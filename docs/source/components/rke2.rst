=======================
RKE2 and the node stack
=======================

RKE2 v1.35.7+rke2r1 on six nodes: three servers running the API and etcd, three
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
environment: it fsyncs on every commit, and this hypervisor is itself a VM
whose storage sits at etcd's fsync floor.

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
   $ kubectl get ingress -A

A 404 from Traefik means no Ingress matched the hostname — check the Ingress
exists and its ``host`` is exactly right. A 503 means the Ingress matched and
its backend Service has no ready endpoints, which is a workload problem, not an
ingress one.

.. note::

   Traefik is here because ingress-nginx reached end of life in March 2026, not
   because it was always RKE2's default. It was not; RKE2 makes Traefik the
   default for new clusters from v1.36.

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
