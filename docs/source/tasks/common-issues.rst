==============
Common issues
==============

Ordered by symptom. Every entry here has actually happened in this
environment.

Nothing responds at all
=======================

**Ansible times out against an internal host, and the error names the host.**
Check the tunnel handshake, not the interface:

.. code-block:: console

   $ sudo wg show          # "latest handshake" must not be 0

A WireGuard peer with a stale key brings the interface up, lists the peer and
reports green from systemd. The handshake is the only symptom. Re-run
``playbooks/tunnel_controller_access.yml``.

**A browser gets "Forbidden" from an internal service, but curl over the
tunnel gets the page.** The proxy is sourcing the connection from the wrong
address. ``repo01`` is dual-homed, and dante binds its source address per
destination only when ``/etc/danted.conf`` names both interfaces with
``external.rotation: route``. With one interface named, everything the proxy
reaches leaves as ``192.168.1.20``, which the artifact host's ``Require ip``
list does not include. Confirm from the source address in
``/var/log/apache2/artifact-host-access.log`` on ``repo01``, then re-run
``playbooks/repo01.yml --tags socks5_proxy``.

**Everything on repo01 stops at once — GitLab, APT, artifacts, the tunnel.**
Its 32 GB root filesystem is full. ``df -h /`` on ``repo01``. Everything that
grows belongs on ``/data1``; find what is not.

DNS
===

**A name that should exist returns NXDOMAIN.** Ask the authority before
believing the resolver:

.. code-block:: console

   $ dig @192.168.2.4 gitlab.dev.lo         # FreeIPA, for dev.lo
   $ dig @192.168.2.40 grafana.k8s.dev.lo   # the cluster, for k8s.dev.lo

A name queried before it existed stays NXDOMAIN for the zone's negative TTL —
an hour here — on every resolver that asked early. If the authority answers and
your resolver does not, flush your resolver and wait it out.

**Nothing under ``k8s.dev.lo`` resolves.** FreeIPA forwards that whole zone to
the cluster's CoreDNS on ``192.168.2.40`` and holds no records for it, so this
is a cluster problem wearing a DNS problem's clothes:
``kubectl -n cluster-dns get pods,svc``.

**A bucket name does not resolve but the S3 endpoint does.** The cluster's DNS
answers single-label names under ``k8s.dev.lo``; ``<bucket>.s3.k8s.dev.lo`` is
two labels and is handled by an explicit record. New buckets addressed
virtual-host style need that record to exist.

**An internal host cannot resolve an internet name.** Correct. ``core01`` has
no forwarders, because there is no upstream resolver reachable from the
internal network. Only ``repo01`` and the controller resolve both.

Certificates
============

**``x509: certificate signed by unknown authority``.** The FreeIPA CA is not in
that client's trust store. See :doc:`../access`.

**A browser warns on a ``k8s.dev.lo`` name but not on ``gitlab.dev.lo``.**
Almost always the working name is a stored exception rather than real CA trust
— clicking through a warning once is permanent, and it is listed under the
Servers tab of the browser's certificate dialog. The root CA alone validates
every internal name, ``k8s.dev.lo`` included, because cert-manager serves the
cluster intermediate alongside each leaf. Confirm the chain is intact before
touching the certificates:

.. code-block:: console

   $ curl -fsSL http://192.168.2.99/certs/dev.lo-ca.crt -o /tmp/root.crt
   $ openssl s_client -connect bao.k8s.dev.lo:443 -servername bao.k8s.dev.lo \
       -CAfile /tmp/root.crt </dev/null 2>/dev/null | grep "Verify return code"

``Verify return code: 0 (ok)`` means the server side is correct and the fault
is in that browser's trust store. See :doc:`../access`.

**A ``k8s.dev.lo`` name serves ``CN=TRAEFIK DEFAULT CERT``.** No Ingress claims
that hostname. The cluster's DNS answers every single-label name under
``k8s.dev.lo`` with the ingress address, so a typo resolves and connects, then
gets Traefik's self-signed fallback. No amount of CA installation fixes it —
check the hostname against ``kubectl get ingress -A``.

**``curl`` is happy but an Ansible ``uri`` task fails verification.** Ansible's
Python may verify against a different bundle than
``update-ca-certificates`` writes. Roles pass
``/etc/ssl/certs/ca-certificates.crt`` explicitly for this reason.

**A ``Certificate`` sits ``False``.** ``kubectl describe certificate`` and then
``certificaterequest``. Nearly always the ``k8s-ca`` issuer being unhealthy
rather than the workload.

Images and pulls
================

**``ImagePullBackOff`` on a new service.** In this cluster, three causes, in
order of likelihood:

#. No mirror rule rewrites that upstream namespace — check
   ``/etc/rancher/rke2/registries.yaml`` on the node, and remember a change
   there needs an RKE2 restart to reach containerd.
#. The image was never mirrored into GitLab at all.
#. The ``rke2-nodes`` deploy token is stale — a 401 that reads like a broken
   image name.

**A pull 404s for an image that plainly exists.** The mirror was made
single-platform. Copy with ``--all --preserve-digests``: charts that pin by
digest cannot resolve a rewritten manifest.

**A bare ``postgres:17.7-alpine`` will not pull.** Docker Hub official images
resolve to ``docker.io/library/*``; the ``^library/`` rule is what covers them.

Pods
====

**``Pending`` forever.** ``kubectl describe pod``. Either no node has room
(check ``kubectl top nodes`` — worker memory is the scarce resource here), or
its PVC has not bound (:doc:`storage-pvc`), or it landed on a control plane
node it cannot tolerate.

**``CrashLoopBackOff``.** ``kubectl logs --previous`` is the one that shows
why. If the log is empty, the failure is before the process started and lives
in ``describe``.

**Running but not Ready, and its Ingress returns 503.** The readiness probe is
failing, so the Service has no endpoints. The workload, not Traefik.

**"failed to create fsnotify watcher: too many open files".** Not a file
descriptor limit, despite what it says. The kernel's ``fs.inotify`` limits are
**per-UID**, almost every container on a node runs as UID 0, and the default
ceiling of 128 instances is one a Kubernetes node passes without doing anything
unusual — kubelet, containerd, Flux's controllers, cert-manager, Longhorn,
Grafana's sidecars and Alloy all watch files.

The nodes are configured for this by ``rke2_node``, which sets 8192 instances
and 524288 watches in ``/etc/sysctl.d/90-rke2-inotify.conf``. If the message
appears anyway, check the node:

.. code-block:: console

   $ sysctl fs.inotify.max_user_instances fs.inotify.max_user_watches

   # what is actually holding them, by UID
   $ for f in /proc/*/fd/*; do \
       case $(readlink $f 2>/dev/null) in anon_inode:inotify) stat -c %u $f;; esac; \
     done | sort | uniq -c | sort -rn | head

A value of 128 means the drop-in is missing or the host was rebooted before it
was written — re-run ``playbooks/kubecp.yml`` or ``playbooks/kubewk.yml``.
Watch for the failure being silent: a controller that cannot create a watcher
sometimes starts anyway and simply never notices the change it exists to watch.

GitOps
======

**A change was made and nothing happened.** Was it made in the right place?
GitLab's ``platform/cluster-state`` is *rendered* from
``ansible/files/gitops_source/`` and a commit made in GitLab is overwritten.

.. code-block:: console

   $ flux get kustomizations
   $ flux get helmreleases -A        # check for SUSPENDED
   $ flux reconcile kustomization apps --with-source

**A HelmRelease says "upgrade retries exhausted".** Fix the cause, then
``flux reconcile helmrelease <name> -n <ns> --force``.

**A resource keeps reverting.** That is Flux working. Change the source.

Secrets
=======

**OpenBao is sealed.** Normal after any restart. The unsealer polls every 15
seconds; ``kubectl -n openbao logs deploy/openbao-unsealer`` if it does not
clear.

**An ExternalSecret will not sync.** It names the vault path it could not read.
Usually nothing has written that path yet — ``openbao_secrets`` runs in
``cluster_init.yml``.

**A value written to ``env.sh`` during a run is invisible to that run.**
Structural: ``lookup('env')`` reads the environment the process started with.
Re-source ``env.sh`` and run again. Garage's S3 keys are always a run late for
this reason.

**A password is silently wrong by a few characters.** Double quotes in
``env.sh``. A ``$`` inside the value was expanded as an undefined shell
variable. Single-quote everything.

Sign-in
=======

**oauth2-proxy returns HTTP 500 after Keycloak accepted the login**, saying the
email is not verified. FreeIPA has no notion of a verified address, so Keycloak
imports federated users with ``emailVerified`` false. Fixed realm-wide by a
hardcoded-attribute mapper on the federation provider — not by the proxy's
``--insecure-oidc-allow-unverified-email``, which only moves the wall to the
next service.

**A mapper was added and nothing changed for existing users.** Mappers shape
users at import. Trigger a **full** sync; ``triggerChangedUsersSync`` selects on
the directory's modify timestamp, which a Keycloak-side change does not touch,
so every user is skipped and it looks like success.

**A user has the group but not the access.** Nothing propagates to an open
session: Keycloak maps groups at token issue and OpenBao attaches policies at
login. Sign out and back in.

**Grafana refuses a user with no role.** ``role_attribute_strict``, working as
designed. Put them in ``grafana-users``.

**GitLab ignores ``gitlab-admins``.** Group-to-admin mapping is a GitLab
Enterprise feature. Administrator rights are granted in GitLab by ``root``.

Cluster and storage
===================

**etcd leader elections, API latency, ``request timed out``.** Storage, not
etcd. This hypervisor is itself a VM and its storage is at etcd's fsync floor.
Benchmark one host at a time — a synthetic fsync test on all six at once is a
denial of service against storage already at its limit.

**Two faults in the same area are still two faults.** The hypervisor was
swapping *and* its storage was too slow for etcd; fixing the first changed
nothing about the second. Confirm that what you repaired was what was breaking
you.

**A node will not restart cleanly.** ``pgrep -a etcd`` after
``rke2-killall.sh``. See :doc:`node-maintenance`.

**Volumes degraded across the board.** A worker is down, or its
``/var/lib/longhorn`` did not mount. :doc:`storage-longhorn`.

Things that look broken and are not
===================================

* OpenBao sealed for the first few seconds after a restart.
* An internal host failing to resolve an internet name.
* ``/proc/cpuinfo`` reporting ``QEMU Virtual CPU version 2.5+`` — the model name
  never changed; the **flags** did (``sse4_2``, ``popcnt``, ``ssse3``).
* A ``kubectl edit`` being reverted.
* An LDAP federation provider still sitting in Keycloak's ``master`` realm —
  stale, harmless, and should be removed by hand.
