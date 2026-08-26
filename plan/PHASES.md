# Phases

Six phases, executed in order. Each one runs the same flow — **Review → Research →
Implement → Test → Document** — as defined in `OVERVIEW.md`. A phase does not start
until the previous phase's exit criteria pass.

The fifth step is not optional and is not only about this directory: every phase
that changes how the environment is *operated or extended* — a service, a
credential, a URL, a version, a design decision, or a fault worth recording —
updates the documentation site in `docs/` (both the sysadmin and the developer
sections) before it is called complete. See `OVERVIEW.md`.

| Phase | Deliverable | Hosts | Artifact tier introduced |
| --- | --- | --- | --- |
| 1 | Repo server, tunnel, artifact distribution | `repo01` | Tier 1 (Apache + apt proxy) |
| 2 | Identity, DNS, CA for `dev.lo` | `core01` | — (consumes Tier 1) |
| 3 | GitLab container platform | `repo01` | Tier 2 (registry + packages) |
| 4 | RKE2 control plane | `kubecp01-03` | — (consumes Tier 2) |
| 5 | RKE2 workers | `kubewk01-03` | — (consumes Tier 2) |
| 6 | GitOps-managed cluster services (6a/6b/6c) | cluster | — (consumes Tier 2) |

---

## Phase 1: Repo Server and Artifact Distribution

`repo01` is the bootstrap platform for everything that follows: the controller's way
into the internal network, and the **only** source of packages and artifacts for hosts
that have no internet access.

Detailed plan: `PHASE1_IMPLEMENTATION.md`

### Review

- Confirm the Proxmox endpoint, credentials, and Ubuntu 24.04 template are ready.
- Confirm the external (`192.168.1.0/24`) and internal (`192.168.2.0/24`) bridges exist
  and match `TARGETS.md`.
- Confirm no internal host has an internet default route today.

### Research

- Pulumi ProxmoxVE dual-NIC and cloud-init behavior for the `repo01` VM spec.
- Apache layout and access policy for the artifact document root.
- apt-cacher-ng tuning: cache location on `/data1`, expiry window, and a narrow
  CONNECT passthrough. Caching only — mirroring whole archives is out of scope.
- Build the artifact manifest: for every later phase, list each required image,
  binary, and archive with its upstream URL, version, and checksum.

### Implement

1. Create the `repo01` VM through Pulumi (dual NIC: `192.168.1.20`,
   `192.168.2.99`), **at the size `TARGETS.md` gives it** — 4 vCPU / 10 GiB.
   That size is set by GitLab in Phase 3, but it is applied here, because this
   is the phase that creates the host. Phase 3 discovered the original 2 vCPU /
   4 GiB was too small and resized in place; a rebuild must not repeat that.
2. Establish the controller-to-internal WireGuard tunnel with `repo01` as gateway.
3. Apply the base host role: hostname, time, baseline packages, firewall posture.
4. Configure the SOCKS5 proxy for external-to-internal web access.
5. Configure apt-cacher-ng as the APT caching proxy for all internal hosts, bound
   to the internal and tunnel addresses only.
6. Configure split DNS on `repo01` so `dev.lo` resolves through `core01` while
   upstream names still resolve through `192.168.1.1`.
7. **Point the container daemons at `/data1` before either one is installed.**
   Docker's `data-root` and containerd's `root` both default to `/var/lib` on
   the 32 GB OS disk. This is the `container_storage` role, and it runs
   immediately after the data volume and before anything that runs a container
   — including GitLab in Phase 3, whose image traffic is what filled the disk.

   Found during the Phase 6 review and fixed here, in the phase that builds
   this host. On a fresh build it writes two config files and moves nothing,
   which is the entire reason it belongs in Phase 1 rather than in the phase
   that noticed. See `PHASE1_IMPLEMENTATION.md` step 7a.
8. **Configure Apache as the internal artifact host**, serving `/data1/artifacts` over
   HTTP to `192.168.2.0/24`, with directory indexes and published checksum files.
9. **Stage the artifact set on `repo01`**, downloaded over the external NIC and
   verified against the manifest:
   - FreeIPA container images and install files (Phase 2).
   - GitLab container images and install files (Phase 3).
   - Container runtime and Compose prerequisites for `repo01` and `core01`.
   - RKE2 images, binaries, and charts (Phases 4–6), held on `repo01` until GitLab
     exists to receive them.

   Every entry carries `retention: bootstrap | transit`. Transit artifacts are
   removed once GitLab holds them, and presence is then checked at the
   destination rather than on local disk. The axis was designed in Phase 6a,
   but it is applied from here: Phase 1 owns the manifest and the staging role,
   so a rebuild uses the model from its first run.
10. Prepare `repo01` to host containerized services — no host-package GitLab path.

### Test

- Pulumi preview is clean; a second apply is a no-op.
- Ansible reports zero changes on a second run.
- The controller can SSH to an internal target over the tunnel with no jump host.
- An internal node completes `apt update` through the `repo01` proxy, and the cache
  grows only by what was actually requested.
- An internal node downloads an artifact from Apache over HTTP and its checksum
  matches.
- Internal nodes still have **no** internet default route; confirm a direct upstream
  fetch from an internal node fails.
- Destroy and recreate `repo01` end to end, then re-run every check above.

### Exit criteria

- `repo01` rebuilds from scratch in under 30 minutes, excluding template preparation.
- Every artifact needed by Phases 2 and 3 is staged, checksummed, and downloadable
  from Apache.
- Rollback and rebuild procedure documented and executed once.

---

## Phase 2: Core Server (Identity and DNS)

`core01` becomes the identity, DNS, NTP, and certificate authority for `dev.lo`.

Detailed plan: `PHASE2_IMPLEMENTATION.md`

### Review

- Confirm Phase 1 exit criteria still hold, especially the tunnel and Apache host.
- Confirm the FreeIPA images and install files are already staged on `repo01` — Phase 2
  pulls them from Apache, never from the internet.
- Confirm `FREEIPA_ADMIN_PASSWORD` and `FREEIPA_DIR_MANAGER_PASSWORD` are set in
  `~/.config/rke2lab/env.sh`; the playbook's preflight check fails without them.
- Reconcile `TARGETS.md` and the Pulumi VM definitions for `core01`
  (`192.168.2.4/24`, gateway `192.168.2.99`, dns `127.0.0.1`).

### Research

- FreeIPA container image version and its deployment requirements on Ubuntu 24.04:
  host networking, cgroup v2 handling, and the persistent data volume.
- The time source for the internal network. FreeIPA installs with `--no-ntp` today
  because there is no upstream NTP pool to sync against; decide whether `core01`
  should serve its own clock to clients instead.
- The record set GitLab will need in Phase 3.

DNS forwarding is already decided: **none**. FreeIPA is installed with
`--no-forwarders` because no upstream resolver is reachable from this network.

### Implement

1. Enable Phase 2 in the Pulumi deployment configuration and create `core01`.
2. Apply the base host role and point package installs at the `repo01` APT proxy.
3. Apply `container_storage` between the data volume and the container runtime,
   so Docker and containerd write to `/data1` before either is installed.
   `core01` carried the same fault as `repo01` at a tenth the scale. Phase 1
   owns the role; Phase 2 owns applying it to this host.
4. Pull the FreeIPA image and install files **from Apache on `repo01`** and load them
   locally.
5. Bootstrap the `dev.lo` domain with LDAP, DNS, NTP, and the CA.
6. Publish the GitLab DNS records: `gitlab.dev.lo`, `registry.gitlab.dev.lo`.
7. Point internal hosts at `core01` for DNS.

### Test

- `core01` is reachable at `192.168.2.4` and survives a clean reboot.
- FreeIPA installs with no manual intervention; a second Ansible run reports no drift.
- `dev.lo` resolves authoritatively from the controller and from internal hosts.
- GitLab hostnames resolve before Phase 3 begins.
- DNS and CA services come back automatically after a restart.

### Exit criteria

- Working FreeIPA identity, DNS, and CA on `core.dev.lo`.
- GitLab DNS records live and resolving from both sides of the tunnel.
- Rebuild and recovery notes captured.

---

## Phase 3: GitLab Container Platform on `repo01`

GitLab becomes Tier 2 of the artifact model: the container registry and package
registry the cluster consumes from.

### Review

- Confirm `dev.lo` DNS is authoritative and GitLab records resolve.
- Confirm the GitLab container images and install files are staged on Apache.
- Confirm `repo01` has the disk headroom on `/data1` for GitLab plus the registry.

### Research

- GitLab container version, persistent volume layout, and upgrade path.
- Registry storage sizing for the full RKE2 image set.
- Backup and restore approach for GitLab data and registry content.
- Certificate approach: certificates issued by the FreeIPA CA versus plain HTTP for the
  dev environment.

### Implement

1. Load the GitLab image from the Apache artifact host and deploy GitLab as a
   containerized workload on `repo01`.
2. Configure persistent storage, backup policy, and the upgrade path.
3. Enable and configure the container registry and the package registry.
4. **Push the staged RKE2 content from `repo01` into GitLab**: container images to the
   registry, binaries/tarballs/charts to the package registry.
5. Document the pull path internal nodes will use for both registries.

### Test

- Git clone and push succeed over `gitlab.dev.lo`.
- Registry push and pull succeed over `registry.gitlab.dev.lo`.
- Package registry upload and download succeed.
- An internal node pulls an RKE2 image from the GitLab registry with no internet
  access.
- Backup and restore exercised once against real data.

### Exit criteria

- GitLab serving Git, registry, and packages over `dev.lo` names.
- The complete RKE2 artifact set published in GitLab and pullable from an internal
  node.

---

## Phase 4: RKE2 Control Plane

Detailed plan: `PHASE4_IMPLEMENTATION.md`

Two things below did not survive contact with the build, and the detailed plan
explains both. **Cilium moved here from Phase 5**, because Phase 4's own test is
`kubectl get nodes` healthy and a node without a CNI never leaves `NotReady`.
And the control plane is stable only because etcd's heartbeat and election
timeouts were raised to tolerate storage that sits at etcd's documented floor;
see `PROXMOX.md`.

### Review

- Confirm the full RKE2 image and package set is in GitLab and pullable.
- Reconcile the control plane node specs in `TARGETS.md` against the Pulumi
  definitions.
- Confirm the nodes resolve through `core01` (`192.168.2.4`) and that `dev.lo` records
  for the cluster exist before the nodes are created.

### Research

- RKE2 version selection and its matching image set.
- Air-gapped RKE2 install method pointed at the GitLab registry and package registry.
- Registry mirror and credential configuration for containerd.
- etcd sizing, backup, and the control plane HA/VIP approach across three nodes.

### Implement

1. Create `kubecp01-03` through Pulumi.
2. Apply the base host role, APT proxy, and DNS settings.
3. Configure containerd registry mirroring to `registry.gitlab.dev.lo`.
4. Install RKE2 server from GitLab-hosted artifacts and form the cluster.
5. Configure the services required only on the control plane.

### Test

- All three control plane nodes join and etcd reports healthy.
- No node made an outbound internet request during install — verify from logs.
- `kubectl get nodes` is healthy from the controller over the tunnel.
- etcd snapshot and restore exercised once.

### Exit criteria

- A three-node RKE2 control plane, installed entirely from GitLab-hosted artifacts.

---

## Phase 5: RKE2 Workers

Detailed plan: `PHASE5_IMPLEMENTATION.md`

Three decisions Phase 4 deferred are settled here, and one of them inverted the
note Phase 4 left behind: **ingress-nginx is end of life**, so Traefik is the
ingress controller after all. The other two are Longhorn for the CSI layer and
`CriticalAddonsOnly` on the control plane.

### Review

- Confirm the control plane is healthy and the join token path is documented.
- Confirm worker node specs, including the extra 100 GB CSI disk on each.
- Confirm the Proxmox thin pool can carry three more VMs — Phase 5 takes it to
  153% provisioned, which is fine now and is a Phase 6 gate.

### Research

- Worker-side services that must be node-managed rather than GitOps-managed.
- CSI disk preparation for the storage choice in `CLUSTER_COMPONENTS.md`.
  Resolved: **Longhorn**, formatted and mounted at `/var/lib/longhorn`. The
  choice could not be deferred, because Ceph wants a raw device and Longhorn
  wants a filesystem, and a disk cannot be prepared for both.
- Cilium is **already installed** — Phase 4 brought it forward as RKE2's
  packaged chart. Phase 5 inherits it rather than installing it.
- Ingress. Resolved: **Traefik**. The Kubernetes project announced
  ingress-nginx's retirement in March 2026 and RKE2 makes Traefik the default
  for new clusters at v1.36, so Phase 4's correction to `CLUSTER_COMPONENTS.md`
  has itself gone out of date. Selected with `ingress-controller: traefik`,
  which also sets the default ingress class.
- Whether the control plane should be tainted `CriticalAddonsOnly` once workers
  exist to schedule onto. Resolved: **yes**, and strictly after the workers are
  `Ready`, because the taint is `NoExecute`.
- Whether the storage can carry three more nodes; see the etcd section of
  `PHASE4_IMPLEMENTATION.md` before assuming it can. Resolved: yes, and for a
  specific reason — agents run no etcd, so they add throughput rather than
  fsync pressure, and throughput was never the constraint.

### Implement

1. Publish the Traefik image set into GitLab — the only new artifact this phase
   needs, and the workers cannot pull what has not been pushed.
2. Create `kubewk01-03` through Pulumi and publish their DNS records.
3. Apply the base host role, APT proxy, DNS, and registry mirror configuration,
   and prepare both data disks.
4. Install the RKE2 agent from GitLab-hosted artifacts and join the cluster.
5. Label the workers, then taint the control plane, then deploy ingress — in
   that order, and only once every worker is `Ready`.
6. Make the cluster operable: `kubectl` on `PATH` with a `KUBECONFIG` set on the
   controller and on the servers, `crictl` configured on every node, and `k9s`
   on the controller. This was not in the original plan for this phase; it was
   added because the cluster was finished and still could not be looked at
   without knowing two undocumented things.

   `playbooks/controller.yml` also installs the **Flux CLI and `kubeseal`**,
   which are Phase 6a tools rather than Phase 5 ones. They live in the same
   role because it is the controller's single tooling entry point, and it runs
   here — after the cluster exists and before Phase 6 needs them — which is the
   only window that works. Both come from Apache, checksummed, so no ordering
   against GitLab is implied.

### Test

- All workers register `Ready` and pods schedule across them.
- Cilium is healthy and pod-to-pod networking works across nodes.
- A test workload pulls its image from the GitLab registry and lands only on
  workers.
- Traefik is the ingress class, and its pods run on workers.
- CSI disks are mounted on every worker and survive a reboot.
- A bare `kubectl get nodes` works from a new shell on the controller, and from
  a root shell on any server, with nothing to export first.

### Exit criteria

- A six-node cluster with node-managed core services running, no internet dependency.
- The cluster is operable from the controller with tooling that is scripted
  rather than placed by hand.

---

## Phase 6: RKE2 Core GitOps Services

Detailed plan: `PHASE6_IMPLEMENTATION.md`

**Split into 6a, 6b, and 6c.** As written below this was three phases of work
under one number — more than any previous phase attempted — and a failure in the
observability stack could leave the cluster with a half-reconciled CSI layer
underneath it. Each stage now carries its own exit criteria.

Phase 6 also settles the secrets question that has been open since planning
(**OpenBao + External Secrets Operator**, with Sealed Secrets for bootstrap
material) and pays two debts: the artifact model had no story for artifacts that
only pass *through* `repo01`, and the controller's own dependencies were never
documented at all.

### Review

- Confirm the cluster is healthy and the registry mirror works from every node.
- Confirm which components are GitOps-managed per `CLUSTER_COMPONENTS.md`.
- Check `repo01`'s **root** filesystem, not just `/data1`. Done 2026-08-14: it
  was at 79% with the container image store on it, and that — not the thin pool
  everybody was watching — is the storage gate for this phase. Remediated in
  Phase 1 and Phase 2, where those hosts are built; this is a check, not work.

### Research

- Flux CD bootstrap against the internal GitLab instance.
- Repository and directory structure for cluster state.
- Secrets management. Resolved: **OpenBao + ESO, Sealed Secrets for bootstrap.**
  No auto-unseal exists without a cloud KMS or a second instance that must
  itself be unsealed by hand, so the ritual is automated from `env.sh` rather
  than designed away. OpenBao over Vault on licence grounds.
- Chart and image sourcing for every GitOps-managed component, all mirrored
  through `repo01` into GitLab first — under the retention rules in
  `OVERVIEW.md`, so transit artifacts do not accumulate.
- Sizing the observability stack against 30 GiB of worker memory before
  deploying it, rather than accepting chart defaults.

### Implement

**6a — Foundations.** Add `retention:` to the artifact manifest and teach
`rke2_publish` to remove transit copies once GitLab holds them; document the
controller's own dependencies and a cold-start order; bootstrap Flux against a
GitLab project; deploy Sealed Secrets as the first Flux-managed component.

Moving the container data roots off the OS disk is **not** in this list, though
Phase 6's review is what found the problem. It is Phase 1 and Phase 2 work —
those phases build `repo01` and `core01`, and the configuration has to exist
before the daemons are installed rather than after they have filled a disk. 6a
verifies it and does not perform it.

**6b — Core services.** cert-manager on the FreeIPA chain, Longhorn plus a
one-replica StorageClass for fsync-bound singletons, Cilium LB-IPAM, OpenBao
with automated unsealing, External Secrets Operator, Garage, Keycloak — and
single sign-on across every service that has a login, with each authorised by a
pair of FreeIPA groups and each keeping a local way in for the day the identity
provider is what is broken.

**6c — Observability.** Resource requests and retention windows first, then
kube-prometheus-stack, Loki + Alloy, Grafana, Tempo, and the OTel collector.

### Test

- Flux reconciles cleanly with no failing kustomizations or Helm releases.
- Certificates issue from the FreeIPA CA chain.
- Storage provisioning, load balancer IP assignment, and SSO login all work.
- A FreeIPA group grants access to a service, and removing someone from it
  removes their access, with nothing clicked in Keycloak or in the service.
- Every break-glass account still works after single sign-on is enabled —
  most importantly OpenBao's root token, since the vault holds Keycloak's
  own database password.
- OpenBao unseals unattended after a pod restart.
- A second publish run re-downloads nothing, proving presence is checked at the
  destination rather than on local disk.
- Metrics, logs, and traces are visible in Grafana.
- Every image pull resolves to the GitLab registry.

### Exit criteria

- A GitOps-managed cluster where every component is declared in Git and every artifact
  originates from `repo01` and is served by GitLab.
- **That declaration is rendered from this repository, not authored in GitLab.**
  Added 2026-08-16, because the original wording was satisfied by manifests
  that existed only in the GitLab this plan also assumes gets rebuilt. "Declared
  in Git" has to mean the Git that survives losing `repo01`.
- `repo01` holds only bootstrap artifacts; everything else lives in GitLab.
- The controller can be rebuilt from documentation alone.
- The documentation site in `docs/` describes what was actually built: every
  service with a URL, every credential with a rotation procedure, the faults the
  phase found in operator form, and — for the developer section — how the built
  design is extended. Added 2026-08-17, with the guide itself.
