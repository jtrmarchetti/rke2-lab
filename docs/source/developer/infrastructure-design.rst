============================
Infrastructure design
============================

The decisions that shape the estate, and where each one is owned. This page
is the design record for the *why* — the constraints, the four models every
new service must fit, the cross-cutting containerization rules, and which
component was chosen for each job and why. :doc:`../orientation` is the
operator's summary of the same ground: the eight VMs, the three networks, the
two DNS zones, the certificate chain.

The four models a new service must fit
======================================

**The artifact model.** Nothing inside the internal network reaches the
internet; ``repo01`` is the only host that does, and every image, chart and
package enters the estate as an artifact through it — Tier 1 (Apache + APT
proxy) before GitLab exists, Tier 2 (GitLab's registries) after. A service you
add must arrive as a mirrored artifact: :doc:`adding-a-service` step 1.

**The DNS model.** Two zones with a delegation between them. ``dev.lo`` is
FreeIPA's, authoritative, with no forwarders. ``k8s.dev.lo`` is the
cluster's: FreeIPA forwards the whole subdomain to CoreDNS in-cluster and holds
no records inside it, so a single-label service name needs **no second manual
step** — it resolves to the ingress address automatically.

**The identity model.** FreeIPA is the authority for who a user is; Keycloak
holds only the mapping FreeIPA cannot express (``<app>-admins`` ⇒ admin of the
service). Two FreeIPA groups per application, mapped to Keycloak client roles.
A service that federates is an entry in
``inventory_keycloak_applications`` — see :doc:`adding-a-service` step 5.

**The secret model.** One value, one author: ``~/.config/rke2lab/env.sh``,
pushed outward by the playbook that owns it. A service's secret is written to
OpenBao and read by the workload through an ExternalSecret — never a plain
Secret in Git. :doc:`../components/secrets` is the layer map.

Why the estate is shaped this way
=================================

Three constraints do the explaining. A three-node PVE cluster whose per-node
placement keeps every node under commit (do not enable ballooning — a
reservation that cannot be satisfied is a VM that will not start; see
:doc:`../reference/proxmox`). PVE NVMe storage still tuned for etcd's write
pattern (two Longhorn replicas, one disk per worker, nothing more). And the
artifact model, which exists because a lab that can never reach the internet
still has to be rebuildable from a bare checkout.

.. note::

   A default is a fact with an expiry date: when you touch a component, check
   it is still alive upstream, not merely still the default.

Which component was chosen, and why
===================================

The operating procedures for each of these live in
:doc:`../components/index`; this section records the *decision* — what was
picked and the constraint that forced it.

Node managed (installed by RKE2)
---------------------------------

- **CNI: Cilium.** RKE2's packaged chart; a node without a CNI never reaches
  ``Ready``, so the control plane could not have been declared healthy
  without it.
- **DNS: CoreDNS, twice.** RKE2's own for ``*.svc.cluster.local``, and a
  second deployment in ``cluster-dns`` that is authoritative for ``k8s.dev.lo``
  on a LoadBalancer address FreeIPA forwards to. ExternalDNS was never
  adopted: the second CoreDNS answers every single-label name under the
  subdomain with the ingress address, so a GitOps-managed name needs no record
  created anywhere.
- **Ingress: Traefik v3.** RKE2's packaged chart and, from v1.36 on, the
  default ingress class — Traefik was chosen because ingress-nginx reached
  end of life in March 2026. See :doc:`../components/rke2`.

GitOps managed (installed by Flux)
-----------------------------------

- **CSI: Longhorn, not Rook Ceph.** The disks had to be prepared for one or
  the other, and Ceph and Longhorn want opposite things from a disk — a raw
  block device versus a filesystem — so deferring is a decision to do the work
  twice. Longhorn is lighter on workers and on storage already at etcd's fsync
  floor, and Rook's object-storage advantage is redundant because Garage is
  already selected for that. **Two replicas, not the default three:** on a
  three-node cluster three replicas puts a copy on every node and leaves
  Longhorn nowhere to rebuild when one fails; two tolerates the same single
  node loss, keeps a spare to rebuild onto, and gives more usable capacity at
  one fewer fsync per write.
- **Load balancing: kube-vip, not Cilium LB-IPAM.** Cilium's L2
  announcements require kube-proxy replacement, which this cluster does not
  run, and hold a lease per service with a two-second renew deadline — a
  steady stream of etcd writes on storage at etcd's fsync floor. kube-vip was
  already here holding the API VIP; the LoadBalancer half is a second
  DaemonSet of the same image.
- **Object storage: Garage.** S3-compatible, runs outside the cluster, its
  keys stay in the vault. It cannot federate — S3 and an admin bearer token
  have no OIDC to configure.
- **Secrets: OpenBao + External Secrets, Sealed Secrets for bootstrap only.**
  Sealed Secrets holds what must exist *before* a secret store does — the
  credential ESO uses to reach OpenBao. OpenBao holds runtime secrets with
  rotation, leases and an audit trail, and ESO syncs one into the other. It
  runs **in the cluster**, not outside: the trust-domain separation is largely
  illusory here (every VM is root-only, Proxmox can read every guest disk),
  and in-cluster keeps GitOps management and lets ESO authenticate with the
  Kubernetes auth method. It is a single node because its Raft backend
  fsyncs per commit — etcd's write pattern, on storage already at etcd's
  floor — and it runs on the two-replica ``longhorn`` class, never the
  one-replica class, because one node loss must not destroy every runtime
  secret. OpenBao rather than Vault because Vault is now BUSL and OpenBao is
  the Linux Foundation's MPL-2.0 fork, API-compatible and supported by ESO.
- **Service mesh: Cilium (sidecarless).**
- **SSO: Keycloak, federated to FreeIPA.** Two FreeIPA groups per application
  (``<app>-admins`` / ``<app>-users``) map to two Keycloak client roles
  (``admin`` / ``user``); the directory stays the authority for who someone
  is. **GitLab CE cannot map an OIDC claim to administrator** — that is an
  Enterprise feature, so ``gitlab-admins`` is real in FreeIPA and Keycloak
  and GitLab is the one consumer that ignores it; administrator rights there
  are still granted in GitLab, by ``root``. See
  :doc:`../components/identity`.

Both platform services run as containers, not host-installed packages:
**FreeIPA** on ``core01`` (LDAP, DNS, NTP, the CA for ``dev.lo``) and
**GitLab** on ``repo01`` (Git, the container registry, the package
registry). Ordering: FreeIPA's DNS must be authoritative before GitLab is
deployed, so ``gitlab.dev.lo`` and ``registry.gitlab.dev.lo`` resolve
consistently from both the controller and internal hosts.

Cross-cutting containerization rules
====================================

Every one of these was learned the expensive way, and every one of them
applies again to anything containerized. They are rules, not history.

- **Read the image's own documentation before writing the role** — not the
  general pattern for the software, but the documentation for *running it in
  a container*.
- **Give stateful containers a stop grace period.** Docker's default is 10
  seconds. A database ``SIGKILL``\ ed mid-write comes back corrupt, and the
  service manager inside the container will often still report it healthy.
- **Check the mode of the data directory, not just its path.** Containers run
  their services as non-root users that must traverse the bind-mounted
  directory; ``0750 root:root`` silently breaks authentication while leaving
  the service "running".
- **Declare the mode the application itself uses.** A role that enforces its
  own idea of a bind-mount's mode re-applies it on every run, so the second
  run takes down what the first built.
- **Gate on a transaction, not on a status command.** ``ipactl status``,
  ``systemctl is-active`` and a container in state ``Up`` report the last
  known intent, not that the service works. Prove readiness with something a
  consumer actually does.
- **Mount the data volume before the service writes to it.** A service role
  that runs before the data disk is mounted quietly fills the OS disk instead.
- **The OS disk is for the OS. Nothing else, ever.** Anything with a data
  root — container runtimes, databases, caches, logs — gets pointed at the
  data volume when it is installed, not when it runs out.
- **Kill the process, not the unit.** Stopping a service does not stop
  everything it started; check that the process is gone, by name, before
  starting anything back up.
- **Match the storage to the write pattern, not to the throughput number.**
  Measure the thing the service actually does, one host at a time.
- **Clear caches after publishing DNS records.** A name queried before it
  existed stays NXDOMAIN for the zone's negative TTL; verify against the
  authority with ``dig @<server>``.
- **Install the domain CA into the trust store before the container runtime
  starts.** A registry that signs with the domain CA is unreachable to a
  runtime whose trust pool does not yet contain it. Ordering, not content, is
  the failure.
- **Registry mirrors keep their upstream names.** A mirror's ``replacement``
  is the registry host, while every image in the tree keeps its upstream
  name; the mirror must stay a transparent redirect, not a rename.
