=======================
RKE2 and the node stack
=======================

RKE2 v1.36.3+rke2r1 on six nodes: three servers running the API and etcd, three
workers running everything else. RKE2 packages its own containerd, CNI, ingress
and DNS, so most of this layer is installed and upgraded by the RKE2 release
rather than by Flux.

The units
=========

.. code-block:: console

   # on a server
   $ systemctl status rke2-server
   $ journalctl -u rke2-server -f

   # on a worker
   $ systemctl status rke2-agent
   $ journalctl -u rke2-agent -f

   # the node-local CLI, on any node
   $ export KUBECONFIG=/etc/rancher/rke2/rke2.yaml
   $ /var/lib/rancher/rke2/bin/kubectl get nodes

The data directory is ``/data1/rancher/rke2`` — on the 100 GB data disk, never
on the 32 GB OS disk, because the containerd image store grows with every image
the cluster pulls.

.. danger::

   **Stopping the unit does not stop everything it started.** RKE2's etcd
   survives ``systemctl stop``, ``rke2-killall.sh`` and a shim sweep, because
   it leaves the unit's cgroup once the container runtime is gone — and it
   keeps holding its ports, so the next start attaches to a stale datastore and
   blocks forever. Check the process is gone **by name** before starting
   anything back up:

   .. code-block:: console

      $ systemctl stop rke2-server
      $ /usr/local/bin/rke2-killall.sh
      $ pgrep -a etcd        # must be empty before you start again

etcd
====

Three members, one per server. It is the most storage-sensitive thing in the
environment: it fsyncs on every commit, and the PVE nodes' NVMe storage is
still tuned for etcd's write pattern. The remaining etcd tuning is burst
safety, not latency hiding — a heartbeat / election timeout wide enough that
a slow commit is not mistaken for a dead leader.

.. code-block:: console

   $ kubectl get --raw='/readyz?verbose' | head -20
   $ kubectl -n kube-system get pods -l component=etcd

Symptoms of storage rather than of etcd: leader elections in the server
journal, API latency spikes, ``etcdserver: request timed out``. If you
benchmark, benchmark **one host at a time** — a synthetic fsync test run on all
six at once is a denial of service against storage already at its limit.

Cilium (CNI)
============

Installed as RKE2's packaged chart, sidecarless, and it is why nodes reach
``Ready`` at all.

.. code-block:: console

   $ kubectl -n kube-system get ds cilium
   $ kubectl -n kube-system exec ds/cilium -- cilium status --brief
   $ kubectl -n kube-system exec ds/cilium -- cilium-dbg status

A node stuck ``NotReady`` with ``NetworkPluginNotReady`` is a Cilium pod that
has not started on that node.

Service-to-service mTLS
-----------------------

East-west traffic is encrypted and authenticated: mutual authentication
backed by SPIRE's in-cluster install (every pod gets a SPIFFE SVID, and the
chart's ``cilium-spire`` namespace carries the SPIRE server and per-node
agents), and IPsec on the pod network. Both ride the same packaged chart —
the estate's ``rke2-cilium`` HelmChartConfig turns ``authentication.mutual``
on with the spire integration, and ``encryption`` on with type ``ipsec`` —
so there is no separate mesh to deploy or version against anything.

Encryption keys are not chart material: the ``cilium-ipsec-keys`` Secret in
``kube-system`` carries one PSK line generated on the controller and held
under ``~/.config/rke2lab/`` (recorded in ``env.sh`` as
``CILIUM_IPSEC_KEYS``), written as a static RKE2 manifest like the others.
A rotation is a new generated line, not a re-keyed file.

.. code-block:: console

   $ kubectl -n cilium-spire get sts spire-server
   $ kubectl -n cilium-spire get ds spire-agent
   $ kubectl -n kube-system get secret cilium-ipsec-keys
   $ kubectl -n keycloak get cnp cnp-mutual-auth-keycloak-db

Policies are CiliumNetworkPolicies with ``authentication.mode: required`` on
the path being enforced — the pilot is the SSO path, keycloak to its
database (port 5432). A policy in ``required`` fails closed on a missing
handshake, so a stuck SPIRE server or an agent that never scheduled
surfaces as denied connections, not clear-text traffic. The runbook —
enable, verify, roll back, and the edge-TLS boundary — is
:doc:`../sysadmin/cilium-mtls`.

One hard coupling: the estate runs the chart's L7 proxy (``enable-l7-proxy``
is the chart default) together with IPsec, and Cilium refuses to start an
agent that has both without DNS-proxy transparent mode — proxied DNS would
otherwise leave the node unencrypted. The estate therefore ships
``dnsProxy.enableTransparentMode`` in the same HelmChartConfig. If the
agents crash-loop on a fresh cluster and every pod is stuck ``ContainerCreating``
with a CNI error, read the agent log:
``IPSec requires DNS proxy transparent mode`` is the symptom of that block
having been removed.

Hubble
------

The same HelmChartConfig turns Hubble on: ``hubble.enabled`` starts the
Hubble server inside every agent, and ``hubble.relay.enabled`` and
``hubble.ui.enabled`` deploy the relay and the UI into kube-system — the
chart ships the three, the estate carries only the values. The relay and
UI images are in the ``rke2-images-cilium`` mirror set already. The UI is
exposed on the platform Gateway's ``hubble`` listener (the ``hubble``
GitOps tree, next to the UI Service the chart creates) and is fronted
by an oauth2-proxy (``hubble-auth``, the estate's longhorn-auth
pattern) that authenticates against Keycloak and admits the
``hubble-users`` / ``hubble-admins`` tiers. The flow path, edge
exposure, SSO front-end and the ``verify/test_hubble.py`` gate are
documented in :doc:`../components/observability`.

CoreDNS
=======

Two of them, deliberately separate:

``kube-system``
   The cluster's own resolver, for ``*.svc.cluster.local``. Every pod depends
   on it.

``cluster-dns``
   Authoritative for ``k8s.dev.lo``, on LoadBalancer address ``192.168.2.40``,
   which FreeIPA forwards to. An external query storm here cannot take pod DNS
   with it.

.. code-block:: console

   $ kubectl -n kube-system get pods -l k8s-app=kube-dns
   $ kubectl -n cluster-dns get pods,svc
   $ dig @192.168.2.40 grafana.k8s.dev.lo

Traefik (ingress)
=================

Traefik v3 on ``192.168.2.41``, RKE2's packaged chart and the default ingress
class. Every ``*.k8s.dev.lo`` web UI arrives here.

.. code-block:: console

   $ kubectl -n kube-system get pods -l app.kubernetes.io/name=traefik
   $ kubectl -n kube-system logs -l app.kubernetes.io/name=traefik --tail=50
   $ kubectl get gateway,httproute -A

Traefik programs the shared ``platform`` Gateway (see
:doc:`../developer/adding-a-service`). A 404 means no ``HTTPRoute`` matched the
hostname — check a route for that host exists and its ``hostnames`` is exactly
right. A 503 means the route matched and its backend Service has no ready
endpoints, which is a workload problem, not an edge one.

.. note::

   Traefik is here because ingress-nginx reached end of life in March 2026.
   From v1.36 on, RKE2 makes Traefik the default for new clusters, and the
   standalone ``rke2-images-traefik`` airgap tarball was retired — the Traefik
   images ship inside ``rke2-images-core``.

kube-vip
========

Two jobs, and they are separate:

* The **API virtual address** ``192.168.2.20`` (``kube.dev.lo``), floated across
  the three servers as a static pod, so losing a server does not cost the API.
* The **LoadBalancer pool** ``192.168.2.40-52``, as a cloud provider in
  ``kube-system``, which is what gives Services their external addresses.

.. code-block:: console

   $ kubectl -n kube-system get pods -l app.kubernetes.io/name=kube-vip
   $ kubectl -n kube-system logs -l app.kubernetes.io/name=kube-vip-cloud-provider
   $ ping -c1 192.168.2.20

A Service stuck in ``<pending>`` for its external IP is either a pool that is
exhausted or the cloud provider not running.

Registries
==========

No node reaches the internet. ``/etc/rancher/rke2/registries.yaml`` rewrites
every upstream image reference to ``registry.gitlab.dev.lo/rke2/images/...``
and supplies the deploy token that pulls it.

.. code-block:: console

   $ cat /etc/rancher/rke2/registries.yaml
   $ ls /var/lib/rancher/rke2/agent/etc/containerd/certs.d/

.. important::

   RKE2 regenerates containerd's ``hosts.toml`` from ``registries.yaml`` **at
   service start**, so any change to the mirror rules costs a rolling restart of
   all six nodes however correct the file on disk already is. Batch mirror
   additions into one pass.

Kernel tuning
=============

One setting, and it is not optional on a Kubernetes node: the ``fs.inotify``
limits. ``rke2_node`` writes ``/etc/sysctl.d/90-rke2-inotify.conf`` with 8192
instances and 524288 watches, against kernel defaults of 128 and roughly 46000.

The instance limit is the one that bites. It is **per-UID**, nearly every
container here runs as UID 0, and this cluster measured 129 root-owned inotify
instances against the default ceiling of 128 — kubelet, containerd, Flux's four
controllers, cert-manager, Longhorn, Grafana's config sidecars and Alloy all
watch files as a matter of course.

.. code-block:: console

   $ sysctl fs.inotify.max_user_instances fs.inotify.max_user_watches

.. note::

   Over the limit the failure names the wrong resource — ``failed to create
   fsnotify watcher: too many open files`` — because the allocation returns
   ``EMFILE``, which usually does mean file descriptors. ``fs.file-max`` is not
   the problem and raising ``ulimit`` does not help. See
   :doc:`../sysadmin/troubleshooting`.

The values are ceilings rather than allocations; nothing is reserved by raising
them, and the kernel memory a fully used allowance would cost is well under a
gigabyte.
