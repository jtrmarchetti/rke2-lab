# Phase 6 Implementation Plan — GitOps-Managed Cluster Services

## Scope

Turn the six-node cluster Phase 5 delivered into a GitOps-managed cluster: every
component declared in Git, reconciled by Flux, and built from artifacts that
originate on `repo01` and are served by GitLab.

Phase 6 is three phases of work wearing one number. `PHASES.md` lists a Flux
bootstrap, six core services, and a five-component observability stack as a
single deliverable, and that is more than any previous phase attempted. It is
split here into **6a**, **6b**, and **6c**, each with its own exit criteria, so
that a failure in the observability stack cannot leave the cluster with a
half-reconciled CSI layer underneath it.

Phase 6 also pays two debts that earlier phases deferred: the artifact model
never had a story for artifacts that only pass through `repo01`, and the
controller's own dependencies were never written down at all.

A third debt was found during the review and paid in **Phase 1**, where it
belonged — the container daemons had been writing to the OS disk since Phase 3.
Recording it here only because the Review below is where the evidence sits.

## Decision

| | |
| --- | --- |
| GitOps engine | Flux CD, reconciling a GitLab project on `repo01` that Ansible renders and pushes. **Revised 2026-08-16** — it was `flux bootstrap` against that project, which left the manifests existing only in GitLab |
| Secrets | **OpenBao in-cluster + External Secrets Operator**, with **Sealed Secrets holding the unseal keys** so the cluster recovers from Git alone, and an offline break-glass copy in `env.sh`. Settles the `?` that has stood in `CLUSTER_COMPONENTS.md` since planning |
| Seal | Software seal now; **SoftHSM via PKCS#11 the documented next step, hardware HSM an upgrade from that**. Seven implementation requirements keep both migrations open — see the research section. This is not a deferred decision, it is a decision to stay swappable |
| Storage replicas | **Longhorn at 2 replicas**, not the default 3. On three nodes, 3 replicas leaves no spare to rebuild onto; 2 survives the same node loss, yields 147 GB usable against 98 GB, and costs one fewer fsync per write |
| Staging | 6a Flux and repo structure → 6b core services → 6c observability |
| Artifact model | New `retention:` axis. Bootstrap artifacts stay on Apache; transit artifacts are published to GitLab and the local copy is removed. Presence is checked at the destination, not on disk |
| Fallback path | If Flux cannot bootstrap against GitLab CE's API, drive the same Git repository with `flux install` plus a manually applied `GitRepository`/`Kustomization` pair. Bootstrap is a convenience; reconciliation is the product. **This is now the only path**, taken by choice rather than by necessity — see the 2026-08-16 run record |

## Preconditions

- Phase 5 exit criteria hold: six nodes `Ready`, Traefik serving as the default
  ingress class, Longhorn disks mounted on every worker.
- `repo01` root filesystem has headroom for the phase's image traffic. Met as
  of 2026-08-15: 30% used, 22 GB free, with both container daemons writing to
  `/data1`. Fixed in Phase 1, not here.
- No new secret is required to *start* 6a. 6b introduces `OPENBAO_UNSEAL_KEYS`
  and `OPENBAO_ROOT_TOKEN`, both written to `~/.config/rke2lab/env.sh` by the
  initialisation play rather than supplied by hand.

---

## Review

Performed 2026-08-14 against the live environment.

| Check | Result |
| --- | --- |
| Cluster nodes | Six `Ready` — `kubecp01-03`, `kubewk01-03`, all `v1.35.7+rke2r1` |
| Non-running pods | Eight, all `Completed` Helm install jobs. No failures |
| Longhorn disks | `/dev/sdc` mounted at `/var/lib/longhorn` on all three workers, 97.9 GB each, empty |
| Worker `/data1` | ~90 GB free on each |
| Registry mirror | `registries.yaml` present on the workers, rewriting `docker.io` and `ghcr.io` to `registry.gitlab.dev.lo` |
| GitLab | `gitlab-ce:19.2.2-ce.0`, `Up (healthy)`, `gitlab.dev.lo` 302, registry `/v2/` 401 as expected |
| Hypervisor memory | 45.0 of 67.4 GB used, **zero swap**, load ~1.9 |
| Thin pool | 118.4 GB allocated of 950.2 GB, 1456 GB provisioned — 153% |

Four things came out of the review that the plan did not already know, and an
audit of all eight hosts that followed from the first of them.

### `repo01`'s root filesystem is the real Phase 6 storage gate, not the thin pool

`/` on `repo01` is a 30 GB disk with **6.4 GB free (79% used)**, carrying the
container image store the whole artifact pipeline runs through. `/data1` — the
98 GB volume that exists precisely for this — is 8% used with 87 GB free.

Nearly all of the 24 GB used is container image content, and **it is not where
it looks like it is.** `du` reported 15 GB under `/var/lib/containerd` and 4.3
GB under `/var/lib/docker`, and the second figure is an illusion: `du` on a
running daemon follows the live overlay mount at `docker/rootfs/` and counts
containerd's bytes a second time. Stopped, Docker's own tree is a few hundred
megabytes. Docker uses the containerd image store here — `Storage Driver:
overlayfs` — so containerd holds essentially everything.

A fix that moves `/var/lib/docker` alone therefore reclaims almost nothing while
appearing to be the fix.

Every image Phase 6 mirrors is loaded into that store, tagged, and pushed. The
Phase 5 image set was a single Traefik addition; the Phase 6 set is
cert-manager, Longhorn, Garage, OpenBao, ESO, Keycloak, kube-prometheus-stack,
Loki, Alloy, Grafana, Tempo, and an OTel collector. That does not fit in 6.4 GB,
and when the root filesystem fills, GitLab, the APT proxy, Apache, and the
tunnel gateway all stop at once — `repo01` is a single point of failure for
every one of them.

This is the same mistake `OVERVIEW.md` already records as a rule: *mount the
data volume before the service writes to it*. Neither daemon was ever pointed at
`/data1` — there is no `daemon.json` and no containerd root override on either
container host — so both have been filling the OS disk since Phase 3. `core01`
has the same fault at a tenth the scale. The cluster nodes do not: RKE2 was
pointed at `/data1/rancher` in Phase 4 and holds 4.7 GB there.

Phase 6 promotes this from a per-case judgement to a stated rule in
`OVERVIEW.md`: **the OS disk is for the OS**.

**Resolved 2026-08-15, in Phase 1 and Phase 2** rather than here — the fix
belongs to the phases that build these hosts. `repo01` is now at 30% and
`core01` at 9%. See `PHASE1_IMPLEMENTATION.md` for the `container_storage` role
and the two defects its first run exposed. The artifact model rework below
remains Phase 6's own work: it is the structural fix that stops the problem
recurring as the phase mirrors its images.

### The thin pool figures were never wrong — they were in GiB

`PROXMOX.md` records an 884.9 GB pool with 1356 GB provisioned. The API reports
950.2 GB and 1456 GB. These are the same numbers in different units: 884.9 GiB
is 950.2 GB, and 1356 GiB is 1456 GB. Both give 153% provisioned. Nothing has
drifted, and the discrepancy is recorded here so the next person to compare the
two does not spend the afternoon on it.

### Longhorn cannot actually overrun the thin pool

This corrects the gate Phases 4 and 5 both flagged forward, and it is better
news than they expected.

Longhorn writes into `/var/lib/longhorn`, which is a **fixed 107.4 GB virtual
disk** on each worker. Three of them cap Longhorn's total contribution to the
pool at **322 GB**, no matter how many volumes are created or how many replicas
each one has. Added to today's 118.4 GB, a completely full Longhorn puts the
pool at ~440 GB of 950 GB.

So the pool cannot be filled by Longhorn. What the replica count actually costs
is *usable capacity*, and the research below changes the default from three to
**two**, which yields **147 GB usable** rather than 98 GB. That is the number to
size PVCs against. The pool remains overcommittable by the OS and `/data1`
volumes together, which is worth watching, but it is no longer the thing
standing between Phase 6 and a working CSI layer.

### Controller SSH host keys have drifted

`repo01`'s host key has changed since it was last accepted, and `kubewk02` and
`kubewk03` were never added. Ansible is unaffected — `ansible.cfg` sets
`host_key_checking = False` — but raw `ssh` from the controller fails against
all three. Cosmetic today; it will not be cosmetic during a rebuild, and it
belongs in the controller dependency work in 6a.

---

## Research

### Secrets: there is no painless unseal, but there is a painless division of labour

The question this phase had to answer was whether something sits between Sealed
Secrets and Vault that keeps rotation and external-service secret management
without the unseal ritual. The answer has two halves.

**On unsealing: no product removes it in an air-gapped lab.** Every auto-unseal
mechanism either calls a cloud KMS — AWS, Azure, GCP, none of which exist on
`192.168.2.0/24` — or uses a Transit seal backed by a *second* Vault/OpenBao
instance, which must itself be unsealed with Shamir keys by hand. Transit does
not eliminate the ritual; it relocates it to a dedicated instance and makes the
production cluster's restarts automatic. HSM-backed seals are Enterprise-gated.
There is no self-contained Kubernetes-native auto-unseal.

**On everything else: the combined pattern is real, and it is the standard
one.** It is not "Sealed Secrets unseals Vault" — it is a division of labour:

- **Sealed Secrets** holds *bootstrap* material in Git: the credential ESO uses
  to authenticate to OpenBao, and anything else needed before a secret store
  exists. It answers the bootstrap-order problem, which is the one thing an
  external store genuinely cannot solve for itself.
- **OpenBao** holds *runtime* secrets with rotation, leases, dynamic
  credentials, and an audit log — and is reachable by services outside the
  cluster, which was the explicit requirement.
- **External Secrets Operator** syncs OpenBao into native Kubernetes Secrets.
  ESO supports OpenBao as a first-class provider.

**Sealed Secrets also holds the unseal keys.** This is the decision, and it goes
one step further than the pattern above: the OpenBao unseal keys are sealed into
Git, and an in-cluster unsealer loop reads the resulting Secret and unseals
OpenBao after any restart. The cluster then recovers unattended from nothing but
its Git repository and its sealing key, with no controller and no operator in
the loop. Given that Flux already reconciles the cluster from Git, this is the
only option that makes the *whole* cluster self-healing rather than
self-healing-except-the-vault.

Two consequences are worth stating rather than discovering:

- **It collapses two threat models into one.** Anyone who can read the Sealed
  Secrets controller's private key — a cluster-admin, or anyone holding an etcd
  snapshot — can decrypt the unseal keys and therefore everything OpenBao holds.
  Vault's design assumes unseal keys live *away* from the cluster.

  Running OpenBao outside the cluster was considered and rejected, and the
  reasoning is worth keeping. The separation is largely illusory *here*: every
  VM is root-only, Ansible connects as root from a controller that already holds
  every secret in the environment, and Proxmox can read every guest disk. An
  attacker at the level needed to take the cluster steps over a VM boundary. It
  is not nothing — a pod escape and root on another host are different attacks —
  but it is small, and it would cost GitOps management, Flux reconciliation, and
  self-contained recovery to buy it.

  In-cluster also has a concrete technical advantage: ESO authenticates with the
  **Kubernetes auth method**, using pod ServiceAccount JWTs, so there is no
  static ESO credential to store anywhere.
- **The sealing key becomes the single most valuable secret in the lab.** Lose
  it and the SealedSecret cannot be decrypted, the unseal keys are gone, and
  OpenBao's data is unrecoverable. Backing it up is not optional, and it is the
  same obligation as protecting `env.sh` — moved, not removed.

Because of the second point, `env.sh` keeps an offline **break-glass copy** of
the unseal keys, per `SECRETS.md`. The SealedSecret is the automation's copy and
does the day-to-day work; the `env.sh` copy is what recovers the cluster when
the sealing key is lost. Two copies of the same keys is deliberate — they fail
in different ways, and neither one alone covers both failures.

One clarification, so nobody over-trusts the mechanism: **the SealedSecret
protects the copy in Git, not the copy in the cluster.** The controller decrypts
it into an ordinary Secret, so plaintext unseal keys sit in etcd beside the data
they protect. That is correct GitOps mechanics and the right way to get keys
into Git. It is not additional protection at rest.

### Keeping the HSM path open

The software seal is the decision for now, and a software HSM (SoftHSM via
PKCS#11) is the documented next step, with a hardware HSM an upgrade from that.
That only stays true if the implementation does not quietly weld itself to the
current seal. Seven requirements, all cheap now and expensive later:

1. **Isolate the seal configuration.** The seal stanza lives in its own config
   fragment — a dedicated ConfigMap key or HCL file — so changing seal type
   touches one object and nothing else.
2. **Never discard the unseal keys.** Seal migration requires presenting the old
   keys. The Git copy and the `env.sh` copy both serve this; neither is cleanup
   to be tidied away once auto-unseal works.
3. **Keep OpenBao single-node.** Seal migration on an HA cluster is a
   step-down/step-up dance across every node; on a single node it is a restart
   with a migrate flag. The single-node decision was made for fsync reasons and
   happens to make this straightforward too.
4. **Choose an image with PKCS#11 support available.** The seal requires an
   HSM-enabled build compiled with cgo. If the image cannot do PKCS#11, the
   migration is an image swap as well as a config change — verify this when the
   image is selected, not when the migration is attempted.
5. **Track the v2.7.0 plugin transition in the artifact manifest.** PKCS#11 is
   built in through v2.6.x and becomes an external plugin from v2.7.0. That
   plugin is another artifact to mirror through `repo01` under the retention
   rules, and an air-gapped cluster cannot fetch it at migration time.
6. **Make the unsealer loop removable.** Deploy it as its own Flux
   kustomization, so a real auto-unseal seal retires it by deleting one
   declaration rather than by unpicking it from the OpenBao release.
7. **Do not reuse the unseal keys for anything else.** Nothing but the unsealer
   reads them, so removing them later has no side effects.

Write the migration procedure down during 6b while the reasoning is fresh,
including that Shamir keys become *recovery* keys once an auto-unseal seal is
adopted. A migration path that exists only in someone's head is not a path.

**OpenBao rather than Vault.** OpenBao is the Linux Foundation's MPL-2.0 fork of
Vault's last open-licensed release; Vault itself is now BUSL. It is
API-compatible, so ESO and the Terraform/Ansible ecosystem work against it
largely unchanged, and the Enterprise-gated features it lacks — namespaces,
replication, HSM seals — are not used here. This is the same reasoning that put
GitLab CE in this lab rather than EE, and it matters more in a regulated
environment, not less.

**The storage caveat, which is the real cost.** OpenBao's Raft backend fsyncs
per commit — the identical write pattern that forced etcd's heartbeat and
election timeouts to be raised in Phase 4. Running it on a default Longhorn
StorageClass compounds the problem: Longhorn replicates synchronously, so one
OpenBao commit becomes three fsyncs on storage measured at 32-50 fsync/s.

The mitigation is the two-replica default below — OpenBao runs on it like
everything else, at two fsyncs per commit rather than three — plus a
**single-node OpenBao** rather than a three-node Raft cluster. HA OpenBao is
deliberately out of scope: it multiplies write amplification again to protect a
dev lab against a failure mode a snapshot restore covers. This is the same trade
Phase 5 made when it chose Longhorn over Ceph.

OpenBao deliberately does **not** go on the one-replica class. It holds every
runtime secret in the cluster, and a single replica means one node loss destroys
it. The one-replica class is for reconstructible data only.

### Longhorn replica count: two, and not as a compromise

Raw capacity is 3 × 97.9 = 293.7 GB across the workers' third disks.

| Replicas | Usable | fsync per write | Survives 1 node loss | Spare node to rebuild onto |
| --- | --- | --- | --- | --- |
| 1 | 293.7 GB | 1 | no | n/a |
| **2** | **146.9 GB** | **2** | **yes** | **yes, 1 node** |
| 3 | 97.9 GB | 3 | yes | **none — degraded until repair** |

The default of three is the wrong setting on a three-node cluster, and the
reason is not capacity. Three replicas puts a copy on every node, so when one
fails there is nowhere left to rebuild: the volume stays degraded until that
node comes back. Two replicas tolerates the same single node loss *and* keeps a
spare node, so Longhorn rebuilds to full redundancy automatically without an
operator.

It also yields 50% more usable capacity and one fewer fsync per write on storage
measured at 32-50 fsync/s. Three replicas buys nothing here that two does not
already provide.

Two StorageClasses:

- `longhorn` — 2 replicas, the default for everything.
- `longhorn-single` — 1 replica, for genuinely reconstructible data such as
  caches and scratch. Not for OpenBao, and not for anything whose loss matters.

### The artifact model needs a retention axis

The current manifest has `type` (`file` or `image`) and `dest`, and every
artifact is staged on Apache and left there permanently. That was correct while
every artifact was consumed *from* Apache. It stopped being correct in Phase 4,
when RKE2 images began being staged on `repo01` only to be pushed into GitLab —
and it is what filled the root disk.

Artifacts divide into two kinds, and the manifest should say which:

- **Bootstrap** — consumed from Apache by a host that has no other source.
  FreeIPA's image, GitLab's own image, container runtime packages. These must
  stay: they are what a rebuild starts from, before GitLab exists to serve
  anything.
- **Transit** — staged only to be published into GitLab, then consumed from
  GitLab forever after. Every RKE2 image, every Helm chart, every Phase 6
  workload image. The local copy is dead weight the moment the push succeeds.

Adding `retention: bootstrap|transit` lets the staging role delete transit
artifacts once they are published, and keeps `repo01` holding only what a
cold-start actually needs.

The one thing this must not break is idempotency, and there is a trap in it: a
role that deletes a file after publishing and then checks disk to decide whether
to re-fetch will download the entire set on every run. **The presence check has
to move to the destination.** `rke2_publish/tasks/images.yml` already does this
— it reads the registry's existing tags and never opens a tarball whose images
are all present — so the pattern is proven in this repo and needs generalising
rather than inventing.

### The rebuild has never been tested from zero, because the controller is undocumented

`OVERVIEW.md` states that every controller dependency must be documented and
scripted, and then concedes that Pulumi, Ansible, their virtual environments,
and the WireGuard tunnel "remain the unpaid half of this rule".

That makes the current recovery story incomplete in a specific way: the plan can
rebuild the Proxmox environment, but only from a controller that was set up by
hand and whose setup exists nowhere. If the controller is lost, so is the
ability to rebuild anything else.

Phase 6 closes this because Phase 6 is the phase that doubles the artifact count.
The deliverable is a manifest covering controller dependencies alongside
environment artifacts — pinned versions for Pulumi, Ansible and its collections,
Python, WireGuard, `kubectl`, `k9s`, and the SSH known-hosts state noted in the
Review — plus a documented cold-start order. `controller_setup.sh` and
`requirements.yml` are the beginnings of this; neither is complete and neither
is version-pinned.

### Sizing 6c against 30 GiB of worker memory

The three workers total 30 GiB, and the control plane is tainted
`CriticalAddonsOnly`, so the entire observability stack competes with every
other workload for that 30 GiB. kube-prometheus-stack, Loki, Tempo, Grafana,
Alloy, and an OTel collector at their chart defaults will not fit comfortably
even so.

**The escape hatch this section used to name is gone.** It previously read that
the hypervisor had ~22 GB unused and the workers could therefore be grown. That
uplift has since been taken — every host in `TARGETS.md` gained 2 GiB, workers
included, which is where the 30 GiB comes from — and it consumed the headroom
rather than reserving it. The eight VMs now allocate 64 GiB against a 62.8 GiB
host, so **6c cannot buy its way out of a sizing problem with more RAM.**
Explicit requests, limits, and retention windows are the only lever left.

6c therefore begins by setting them for each component against the 30 GiB
total, and treats chart defaults as a starting point to be overridden rather
than accepted.

### Still open

- ~~Whether Garage or Longhorn-backed PVCs back Loki and Tempo.~~ **Decided in
  6b: Garage.** The concern was that it adds a service to the logging stack's
  critical path, and it does. Two things outweigh it. Loki's supported shape is
  object storage — filesystem mode is single-writer and steers every non-trivial
  deployment back to S3 — so choosing PVCs would mean fighting the component all
  the way through 6c. And the durability story is identical either way, because
  Garage is itself a single node on a two-replica Longhorn volume: the same
  blocks, the same replication, one more process in front of them. What is
  actually risked by the extra hop is *log ingestion*, and losing log ingestion
  for the duration of a restart is not the kind of loss the redundancy is for.
- ~~Whether the workers need more memory for 6c.~~ **Decided, and not by 6c.**
  Every host took a uniform 2 GiB, so the workers are at 10 GiB each and 30 GiB
  in total. It also closed the option: the estate now allocates 64 GiB against
  a 62.8 GiB hypervisor, so there is no further memory to give. What remains
  open is whether 30 GiB is *enough*, which is a measurement to take at the end
  of 6b and a sizing exercise if it is not.
- Whether an unprivileged user should exist, still open from `TARGETS.md`. Phase
  6 is where OpenBao and Keycloak make it answerable, since both introduce real
  identity to the environment.

---

## Implement

### 6a — Foundations: artifact model, controller documentation, Flux

Nothing here deploys a cluster service. 6a exists to make 6b and 6c possible
without filling a disk or losing the ability to rebuild.

1. **Confirm the container storage remediation holds.** `repo01`'s root
   filesystem must be well clear of full before any image mirroring starts.

   This work has **moved to Phase 1 and Phase 2**, where the hosts it applies to
   are built, rather than living in the phase that happened to find it. The
   `container_storage` role was written and applied on 2026-08-15: `repo01` went
   from 79% to 30% of its root disk, `core01` from 13% to 9%, and both are
   idempotent. See `PHASE1_IMPLEMENTATION.md` for the role, the two defects the
   run exposed, and the measurement trap that hid the problem.

   Nothing remains to do here. The check is a check.

2. **Add `retention:` to the artifact manifest schema** and classify every
   existing entry. Update the schema comment block in `artifacts.yml`, which is
   the documentation of record for the manifest.
3. **Teach `rke2_publish` the retention model** — take any manifest subset,
   check presence at the destination, publish what is missing, and remove
   transit artifacts once published. Its existing registry-presence logic is
   the part that already works and is kept.

   Delivered as `roles/rke2_publish/tasks/retention.yml` rather than as the
   new `gitlab_publish` role this step originally called for. The rename was
   not worth a role-wide churn for behaviour that fitted inside the existing
   one; the name is now slightly narrower than what the role does.
4. **Write the controller dependency manifest** and a cold-start order that
   assumes nothing but a bare Ubuntu host: Python, Pulumi, Ansible and pinned
   collections, WireGuard, `kubectl`, `k9s`, and the SSH host key state. Fold in
   `controller_setup.sh` and `requirements.yml`.
5. **Mirror the Flux image set and manifests** into GitLab through the new path.
6. **Create the cluster state project in GitLab** and bootstrap Flux against it,
   with the repository structure below.
7. **Deploy Sealed Secrets as the first Flux-managed component**, because it is
   what every later secret depends on and it proves reconciliation end to end
   with something small.

Repository structure for cluster state:

```text
clusters/dev-lo/          # Flux entry point: the cluster's own kustomization
infrastructure/
  controllers/            # cert-manager, Longhorn, ESO, OpenBao, Sealed Secrets
  configs/                # Issuers, StorageClasses, IPPools — things that need
                          # their controller's CRDs to exist first
apps/                     # Keycloak, Garage, observability
```

The `controllers`/`configs` split is not decoration: a `ClusterIssuer` applied
in the same kustomization as cert-manager fails on the first reconcile because
its CRD does not exist yet, and Flux will retry the whole set rather than the
one object. Separating them makes the dependency expressible with `dependsOn`.

### 6b — Core services

Ordered by what depends on what, not by importance.

1. **cert-manager**, with a `ClusterIssuer` chained to the FreeIPA CA so
   certificates issue from the authority Phase 2 built.
2. **Longhorn**, with `longhorn` at **2 replicas** as the default StorageClass
   and `longhorn-single` at 1 for reconstructible data only. Size against the
   147 GB usable figure, not the 322 GB the disks provision.
3. **Cilium LB-IPAM**, with the IP pool drawn from the ~13 addresses
   `OVERVIEW.md` budgets.
4. **OpenBao**, single-node, Raft storage on the 2-replica `longhorn` class,
   initialised by Ansible, with the seal configuration in its own swappable
   fragment and an image verified to support PKCS#11. The unseal keys are
   written to `env.sh` as the offline break-glass copy **and** sealed into Git
   as a SealedSecret.
5. **The unsealer control loop**, as its own Flux kustomization so a future
   auto-unseal seal retires it by deletion. It reads the sealed unseal keys and
   unseals OpenBao after any restart. Nothing after this should be deployed
   until it survives a restart unattended.
6. **External Secrets Operator**, authenticating to OpenBao with the
   **Kubernetes auth method** — pod ServiceAccount JWTs, so there is no static
   ESO credential to store or rotate. Sealed Secrets covers bootstrap material
   that genuinely predates OpenBao, which with k8s auth is less than expected.
7. **Garage** for object storage.
8. **Keycloak** in FIPS mode: the IdP is inside the FIPS boundary.

### 6c — Observability

Sized before it is deployed, per the research.

1. Set resource requests, limits, and retention windows for every component
   against the 30 GiB worker total.
2. kube-prometheus-stack.
3. Loki plus Alloy, storage backend per the decision left open in 6b.
4. Grafana, Tempo, and the OpenTelemetry collector.
5. Dashboards and datasources declared in Git, not clicked in.

---

## Test

Per stage, and a stage is not done until its own tests pass.

**6a**

- `repo01` root filesystem below 50% used, with both container data roots on
  `/data1`. This is a **precondition check on Phase 1's work**, not a test of
  anything 6a does — see the Preconditions above.
- A transit artifact publishes to GitLab and its local copy is gone.
- A second run of the publish role reports no changes and re-downloads nothing —
  the check that proves presence-at-destination works.
- `flux check` passes; the Sealed Secrets kustomization reconciles clean.
- The controller manifest is complete enough that a reviewer can name every
  dependency without reading a shell history.

**6b**

- A certificate issues from the FreeIPA chain and validates against it.
- A PVC binds, mounts, and survives a node reboot.
- A `LoadBalancer` service receives an IP from the Cilium pool.
- OpenBao unseals automatically after a pod restart, with no human step.
- ESO syncs an OpenBao secret into a Kubernetes Secret.
- SSO login against Keycloak succeeds, in the `dev-lo` realm rather than
  `master`, and a FreeIPA group grants access to a service without anything
  being clicked in Keycloak. Full test list in the single sign-on section
  below.
- Every image pull in the phase resolves to `registry.gitlab.dev.lo`.

**6c**

- Metrics, logs, and traces all visible in Grafana.
- No component is `OOMKilled` after an hour under its own load.
- Retention windows are set explicitly, and the disk cost of each is measured
  against the ~147 GB Longhorn budget.

**All stages**

- Flux reports no failing kustomizations or Helm releases.
- No node makes an outbound internet request — verified from logs, as in Phase 4.

---

## Risk Gates

| Gate | Condition | Action if unmet |
| --- | --- | --- |
| `repo01` root disk | Below 50% before any 6b image mirroring | Stop, and fix it in Phase 1 by running `container_storage` — not from here. 6a's step 1 is the check; the remediation belongs to the phase that builds the host |
| Thin pool | Allocated below 60% of 950 GB | Reduce Longhorn replica counts; the disks cap the exposure at 322 GB but the OS volumes do not |
| Longhorn usable | PVC total below ~147 GB at two replicas | Move reconstructible data to `longhorn-single`; do not drop OpenBao to one replica |
| Worker memory | No sustained `OOMKilled` in 6c | **Cut requests and retention windows — do not resize.** The hypervisor headroom this gate used to point at has been spent by the uniform 2 GiB uplift; 64 GiB is allocated against 62.8 GiB of host. Growing a worker now takes memory from another guest |
| OpenBao commit latency | Unseal and write complete in reasonable time | Move off Longhorn to a local volume — not to one replica, which trades the vault's durability for latency |
| Unseal automation | Survives a pod restart unattended, from Git alone | Do not proceed past step 5 of 6b — every later service depends on it |
| Sealing key backup | Sealed Secrets key backed up, and unseal keys present in `env.sh` | Stop. Losing the sealing key without the break-glass copy makes OpenBao unrecoverable |
| Seal swappability | Seal config isolated, image supports PKCS#11, unseal keys retained, unsealer separately deployed | Fix before 6c. Each one is cheap now and a rebuild later |

---

## Run Record — 2026-08-15

### 6a: delivered, except Sealed Secrets

| Step | Result |
| --- | --- |
| Container storage off the OS disk | Done, and moved to Phase 1/2 where the hosts are built. `repo01` 79% -> 30%, `core01` 13% -> 9% |
| `retention:` on the manifest | 17 entries classified. `/data1/artifacts` 2.9 GB -> 1.6 GB, `rke2/` 1.3 GB -> 32 KB |
| Publish removes transit copies | Done. Re-running staging afterwards reports `changed=0` and downloads nothing |
| Flux mirrored into GitLab | v2.9.4, four controllers, pinned by digest |
| Flux CLI on the controller | Installed by `kube_cli_controller` from Apache, checksummed |
| Flux bootstrapped | `flux check` all passed; controllers on workers only |
| Repository structure | `infra-controllers` -> `infra-configs` -> `apps`, all four kustomizations `True` |
| Sealed Secrets | Done — see the second run record below |
| Controller manifest | Done — see the second run record below |

### Three things the run taught

**`registries.yaml.j2` had a latent bug that only a second entry could expose.**
`mirrors` is a map keyed on the upstream registry, and the template emitted one
block per configured rewrite. Adding Flux beside kube-vip produced two `ghcr.io`
keys; containerd kept the first and dropped the second, so the image resolved to
an unrewritten path and failed with `insufficient_scope`. **An authorization
error for what was really a routing bug** — and it would have looked like a
broken deploy token to anyone who met it cold. The template now groups by
upstream.

**RKE2 regenerates containerd's `hosts.toml` from `registries.yaml` at service
start.** The config was correct on disk while every running node still had the
old rewrites, which is the exact failure `playbooks/repo01.yml` already warns
about for handlers. Fixing it needed a rolling restart of all six nodes — agents
first, servers one at a time. Six `Ready`, three etcd members `Running`, no
failing pods afterwards.

**The rewrite made `--registry` unnecessary.** Flux's manifests keep their
upstream `ghcr.io/fluxcd` names and the nodes resolve them to the mirror, so
nothing committed to Git is airgap-specific and an upgrade is a version bump
rather than a re-rewrite. This is the same property that let RKE2's packaged
charts work unmodified in Phase 4, reused rather than rediscovered.

### The pipeline every remaining component follows

6a proved the path end to end, and 6b and 6c are the same cycle repeated:

1. Choose the version against the vendor's **current** compatibility matrix.
2. Pin every image by digest read from its registry.
3. Add manifest entries with `retention: transit`.
4. Stage on `repo01`, publish into GitLab, local copies retire themselves.
5. Add a mirror entry **only if the image's upstream host has no catch-all
   rule yet**. The rewrites are host-level (see
   `inventory_rke2_node_registry_mirrors` in `group_vars/all/main.yml`), so a
   new namespace under an already-listed host costs nothing; a brand-new host
   costs one rolling restart, paid once.
6. Declare it under `infrastructure/controllers`, its objects under
   `infrastructure/configs`, workloads under `apps`.
7. Verify with a transaction, not a status.

## Run Record — 2026-08-15, second pass

Closes the two items 6a left open: Sealed Secrets (step 7) and the controller
dependency manifest (step 4). The first run record listed only the first of
these as outstanding; the second was outstanding too, and unlisted — the
artifact manifest and `kube_cli_controller` had both been referring to a
`plan/CONTROLLER.md` that did not exist.

| Step | Result |
| --- | --- |
| Sealed Secrets mirrored | v0.38.4, pinned by digest, `docker.io/bitnami` rewrite added |
| Rolling restart | Six nodes, agents first then servers; all `Ready`, three etcd members `Running` |
| Sealed Secrets reconciled | Controller `Running` on `kubewk03`, pulled through the mirror |
| Transaction test | A sealed secret committed to GitLab decrypted in-cluster; removing it pruned both objects |
| Sealing key backed up | `~/.config/rke2lab/sealed-secrets-key.yaml`, mode 0600 |
| kubeseal on the controller | Installed by `kube_cli_controller` from Apache, checksummed |
| `plan/CONTROLLER.md` | Written — dependency manifest, cold-start order, and the gaps |

### The version choice was a trap in both directions

Sealed Secrets v0.38.2, v0.38.3 and v0.38.4 were cut within three hours of each
other on 2026-07-03, and the vendor's release notes label the last two
**"Incomplete release for credentials problems"**. Read cold, that says to take
v0.38.2 — the newest release not carrying a warning.

The registry says the opposite. Docker Hub has **no `0.38.2` or `0.38.3` tag at
all**: the credentials that failed were the *image push* credentials, so those
two releases have complete GitHub assets and no container image. v0.38.4 is the
release that finally pushed, twenty seconds after being cut.

So the careful-looking move — distrust the release the vendor flagged, fall
back one — was the broken one, and it would have failed as an
`ImagePullBackOff` pointing at the mirror rather than at an upstream image that
was never published. **The release notes and the registry disagreed, and the
registry was right.** Checking the destination rather than the announcement is
the same principle the retention model already runs on.

### Three smaller things

**The template fix from the first pass held.** Sealed Secrets is a
`docker.io/bitnami/*` image, so it needed a second rule under the `docker.io`
key — the exact shape that broke `ghcr.io` in the first pass. The grouped
template emitted one `docker.io` block with two rewrites, and the generated
`hosts.toml` on the nodes confirms it. The bug found in the first pass was
fixed properly rather than worked around, and this is the proof.

The restart was needed again, for the same reason: RKE2 regenerates
containerd's `hosts.toml` from `registries.yaml` at service start, so the
config was correct on disk and stale in memory until all six nodes restarted.
This is now a known cost of adding any registry prefix, and it is written into
the component pipeline above.

**The test was a transaction in both directions.** A secret was sealed on the
controller, committed to GitLab, and decrypted in-cluster into a `Secret` owned
by the `SealedSecret` — plaintext never left the controller, and the controller
never saw the plaintext. Then the declaration was removed, and `prune: true`
took the derived `Secret` with it. The second half matters as much as the
first: a GitOps layer that creates but does not clean up is one that drifts.

**The sealing key existed a phase earlier than the plan assumed.**
`SECRETS.md` said the sealing key "joins this list in Phase 6b". It does not —
the controller generates its key pair the first time it starts, so the key
existed the moment Flux reconciled Sealed Secrets, before anything had been
sealed with it. A plan that waits for 6b to think about the backup leaves the
most valuable secret in the lab unprotected for all of 6a. Backed up now, with
the label selector rather than the Secret name, because the controller adds a
new key every 30 days and keeps the old ones. `SECRETS.md` is corrected.

The backup is **written but not yet exercised by a restore**, which makes it a
backup on paper. Proving it belongs in 6b, *before* the OpenBao unseal keys are
sealed with it.

### What writing down the controller found

Two pieces of trust state that no playbook owns, neither of which had failed
loudly:

- **SSH host keys have drifted** — `repo01`'s changed, `kubewk02` and
  `kubewk03` were never accepted. Invisible to Ansible, which sets
  `host_key_checking = False`, and blocking for a human.
- **The FreeIPA CA is not in the controller's trust store.** `git clone`
  against `https://gitlab.dev.lo` fails until `GIT_SSL_CAINFO` is passed by
  hand. Flux is unaffected because its `GitRepository` carries the CA in the
  `flux-system` Secret — which is precisely why nobody noticed: the cluster
  trusts the CA and the machine that publishes to the repository does not.

Also recorded there: Ansible is `pip`-installed into Homebrew's Python with no
virtual environment, the collections are installed in two places at once, and
`kubernetes.core` is installed, undeclared, and unused. `controller_setup.sh`
configures dnsmasq and nothing else, despite its name.

## Run Record — 2026-08-16, 6b

Six of the eight 6b components are delivered, and two decisions changed in
flight. Both changes came from the environment rather than from preference,
and both are recorded here as decisions rather than as adjustments.

| Step | Result |
| --- | --- |
| Sealing-key restore test | **Passed, destructively.** The live key was deleted, a sealed probe was proven undecryptable by the replacement key, the backup was applied, and the probe decrypted again |
| cert-manager | v1.21.1, issuing from an intermediate CA that FreeIPA signed |
| Longhorn | v1.11.3, two replicas, `longhorn-single` beside it. A PVC binds, mounts, and reports `healthy` with two replicas |
| LoadBalancer addresses | kube-vip, pool `192.168.2.40-52`. **Not Cilium LB-IPAM** — see below |
| Cluster subdomain | `k8s.dev.lo`, delegated by FreeIPA to the cluster's own CoreDNS at `192.168.2.40`. Ingress at `192.168.2.41` |
| OpenBao | 2.6.1, single node, Raft on the two-replica class, initialised and unsealed |
| Unsealer loop | Its own Flux Kustomization. **Survives a pod restart with no human step** — Risk Gate closed |
| External Secrets Operator | v2.9.0, Kubernetes auth, no stored credential. A secret written to OpenBao appeared in a Kubernetes Secret |
| Controller trust | Both carry-forward items paid: the domain CA is in the trust store and every managed host's key is in `known_hosts` |
| Garage | Deployed, layout applied, buckets created. An object written through the ingress over the cluster's own TLS returns 200 |
| Keycloak | Running, federated to FreeIPA over LDAPS. FIPS is a component production enables. **Applied and verified on the wire 2026-08-17** |
| Single sign-on | Realm `dev-lo`; groups federated; Grafana, OpenBao, GitLab and the Longhorn proxy federated. **Applied and verified 2026-08-17**, second run reports `changed=0` |

### The artifact model gained a third type, because the second one is broken

Images the cluster pulls are now `retention: transit, type: mirror` — copied
registry to registry by skopeo and never staged on `repo01` at all. The tarball
path stays for the two cases that need it: an image a host must load from a
file because it has no registry, and RKE2's own airgap release sets.

This was not a tidy-up. Docker's containerd image store discards a layer's
compressed blob once the layer is unpacked, so an image that pulls and *runs*
cannot always be exported: `docker save` writes a manifest with no layers, and
the push fails with **"does not provide any platform"** — an error naming the
platform when the fault is missing content. It stopped the External Secrets
image dead after eighteen others had gone through the same code path
successfully, which is the worst shape a bug can have. moby/moby#52897 and
docker/cli#5476.

skopeo streams blobs by digest and touches neither the image store nor the
disk, so the fix also removed a staging tree measured in gigabytes and three of
the four copies each image used to make.

### kube-vip, not Cilium LB-IPAM

The plan named Cilium LB-IPAM. It is not used, and the reasoning is worth
keeping because the alternative still looks attractive on paper.

Cilium's L2 announcements require **kube-proxy replacement**, which this cluster
does not run: enabling it replaces the service datapath on a live cluster, and
service networking is down until it is right. L2 announcements also hold a
**lease per service with a two-second renew deadline** — a steady stream of etcd
writes on storage measured at 32-50 fsync/s, which is the same constraint that
forced etcd's timeouts up in Phase 4.

kube-vip was already here, already in ARP mode, already trusted with the API
VIP. The LoadBalancer half is a second DaemonSet of the same image, on the
workers, plus the cloud provider that hands out addresses.

What is given up: Cilium would announce from the node actually running the
backend, and its pools are CRDs with selectors rather than a ConfigMap of
ranges. Neither the addresses nor the DNS design changes if this migrates
later.

### The cluster owns a subdomain, and getting there took two non-obvious steps

`k8s.dev.lo` is the cluster's. FreeIPA holds no records inside it; the
cluster's own CoreDNS answers every name under it, with `k8s_external` for
LoadBalancer Services and a single-label wildcard for ingress hostnames.

**A forward zone alone does nothing.** BIND answers from an authoritative zone
before it consults a forwarder, so `dev.lo` returned an authoritative NXDOMAIN
for every `k8s.dev.lo` name while the forward zone sat unused. The zone needs a
real delegation — an NS record and its glue A record. Those two records are the
only ones FreeIPA holds for the subdomain, and they are what a delegation *is*.

**Then it needs DNSSEC validation off.** With the delegation in place, answers
began failing validation instead. `dev.lo` lives under a TLD the public root
says does not exist, so anything this server *resolves* rather than serves from
its own zone data cannot build a chain of trust — invisible until a subdomain
was delegated away. BIND 9.11, which the FreeIPA image ships, has no per-zone
escape: `validate-except` arrived in 9.13, and 9.11's negative trust anchors
are `rndc nta` only, dynamic and expiring in a week. So the choice is between
validating nothing and delegating nothing. The role now manages the BIND
options file, defaults validation **on**, and `core01` turns it off with the
reasoning beside it.

### An intermediate CA, not ACME and not an exported key

cert-manager signs with an intermediate that FreeIPA signed, and the three
options are worth recording because two of them fail differently:

- **Ask FreeIPA per certificate**, over its ACME service. FreeIPA issues only
  for names it holds as principals, so every ingress hostname would need a host
  entry in the domain — the exact coupling the subdomain exists to remove.
- **Export the domain CA's key.** FreeIPA will not, its sub-CA keys never leave
  Dogtag, and a lab that copied a root key into etcd would have a chain in name
  only.
- **Have FreeIPA sign an intermediate the cluster generates.** The chain is
  real, the root key never moves, and cert-manager issues for any name under
  the subdomain without a round trip.

FreeIPA ships no subordinate-CA profile, so `ipa_sub_ca` imports one: a copy of
`caIPAserviceCert` with basic constraints asserting CA:TRUE at **path length
zero**, key usage changed to certificate and CRL signing, and the leaf-only
extensions removed. The role asserts what came back is actually a CA, because
a profile that failed to apply returns a perfectly valid leaf certificate and
cert-manager would accept it as an issuer and fail on the first certificate it
signed.

### Keycloak found a fault in the VM definitions, not in Keycloak

`keycloak:26.7.1` starts and dies immediately with:

```text
Fatal glibc error: CPU does not support x86-64-v2
```

The VMs present **`QEMU Virtual CPU version 2.5+`** — Proxmox's default
`kvm64` model, which advertises a Pentium 4-era feature set with no SSE4.2 and
no POPCNT, and therefore not x86-64-v2. Every RHEL 9 and UBI 9 image requires
x86-64-v2 and refuses to run without it, and Keycloak's image is UBI 9. There
is no Keycloak version that avoids this: the requirement comes from the base
image, and falling back far enough to escape it would mean running an identity
provider years out of support.

The message names the CPU, and the thing to change is the VM definition. The
fix is one field — `cpu_type` on `VmSpec`, set to `x86-64-v2-AES`, which is the
oldest model that satisfies the requirement and so keeps a guest migratable to
any host rather than pinning it to this machine's silicon. It is written and
committed in `infra/pulumi/modules/vm_factory.py`.

**It is deliberately not applied.** Changing the CPU model needs a full power
cycle of every VM — a reboot from inside the guest keeps the running QEMU
process and the CPU it presents — so this is a `pulumi up` followed by stopping
and starting the whole estate, including all three etcd members, on storage
already at its floor. That is an operator's decision to schedule, not a step to
take at the end of a build.

Everything else in 6b runs on the current CPU, and so does everything 6c
mirrors today: the observability stack is Go binaries and Alpine images, which
have no such floor. Keycloak is the first thing in this lab to need a
hypervisor change, and it will not be the last.

### Four smaller things the run taught

**Chart values are not where you assume.** RKE2's Traefik takes
`service.spec.type`; a value at `service.type` is accepted, ignored, and leaves
the Service ClusterIP. The OpenBao chart builds its image reference from a
separate `registry` value, so a fully qualified `repository` became
`quay.io/quay.io/openbao/openbao` and failed as `insufficient_scope` — an
authorization error for a path built twice. Both read as the wrong thing being
broken.

**`kubectl exec` does not carry the caller's environment.** A vault token
passed as a task `environment:` arrives nowhere and the vault answers 403,
which reads as a policy problem. `openbao_config` now drives the HTTPS API
through the ingress, which is better than what it replaced: it exercises the
DNS delegation, the ingress address and the certificate rather than bypassing
all three.

**Homebrew's Python does not read the system trust store.** After installing
the domain CA, `curl` and `git` verified immediately and every Ansible `uri`
task did not — Ansible runs on Homebrew's Python, which verifies against
`$(brew --prefix)/etc/openssl@3/cert.pem`. Recorded in `CONTROLLER.md`.

**The restore test generated a second sealing key.** The controller creates a
new key when it starts without one, and it seals *new* secrets with the newest
key. Restoring the backup therefore left two keys in play, and the backup was
re-taken to hold both — a backup that covered only the original would have made
everything sealed after the test unrecoverable.

## Run Record — 2026-08-16, 6c

Sized first, then deployed, which is the order the research section demanded.
Requests come to roughly 2 GiB across the four releases, against 30 GiB of
worker memory shared with everything 6b put there.

| Step | Result |
| --- | --- |
| Sizing | Every component states requests and limits. Prometheus scrapes at 60s, keeps 5 days **or** 6 GB — only the size limit is a promise the disk can keep |
| kube-prometheus-stack 88.3.0 | Running. 45 scrape targets up |
| Loki 7.3.0 | Running, objects in Garage. A query returns pod logs labelled by namespace, pod and node |
| Alloy 1.11.1 | DaemonSet on every node, shipping logs and receiving OTLP |
| Tempo 1.24.4 | Running. A span pushed through Alloy reads back through Grafana |
| Grafana | On `grafana.k8s.dev.lo`, cluster-issued certificate, datasources declared in Git |
| Garage credentials | Reached Loki through OpenBao and External Secrets — the first workload to use the secrets model end to end |

Two components the plan listed are deliberately absent. There is no separate
**OpenTelemetry collector**, because Alloy is one; running both would mean two
collectors with overlapping receivers and no rule about which owns a pipeline.
And the admission webhook's **certgen Job** is gone, because cert-manager
issues that certificate from the same authority as everything else.

### The mirror was wrong in a way that only digests could reveal

`skopeo copy` copies **one platform** by default. The digest an artifact
manifest pins is an image *index*, so a copy without `--all` lands a
single-platform manifest whose digest necessarily differs from the one pinned.

Nothing notices while images are pulled by tag. The Alloy chart pins its
config-reloader as `tag@sha256:...`, and the node then asked the mirror for a
digest the mirror did not have — a 404 for an image sitting right there under
the tag, from a mirror working perfectly for every other image.

`--all --preserve-digests` is now in the role. The cost is real: an index
carries every architecture, so a mirrored image is several times the bytes the
cluster runs. That is the right trade for an air-gapped mirror, where an image
that cannot be addressed the way its chart addresses it is not mirrored at all.

### Versions come from three places and they disagree

Three separate version faults in one phase, all of the same shape — the thing
that pulls an image is not the thing that named it:

- **A chart's `appVersion` is not its image tag.** Loki chart 7.3.0 declares
  appVersion 3.6.12 and defaults its image tag to 3.6.11.
- **An operator overrides its chart's pins.** kube-prometheus-stack pins
  config-reloader v0.91.0 in values; the operator injects the version matching
  *itself* into every Prometheus it creates, so mirroring what the chart named
  left Prometheus stuck in Init.
- **Grafana refuses two default datasources.** The chart provisions Prometheus
  as the organisation's default; a second datasource marked `isDefault` makes
  Grafana exit at startup, which Flux reported as a stalled Deployment rather
  than as a configuration error.

The general rule, and it is the one this phase keeps relearning: **render the
chart and read what it actually asks for.** The manifest is authoritative about
what is mirrored, and it is not authoritative about what will be requested.

## Identity is federated, not duplicated

Keycloak reads users from FreeIPA over LDAPS rather than holding its own. The
bind account is a system account under `cn=sysaccounts` — no POSIX identity, no
Kerberos principal, no way to become a user — and Keycloak's provider is
`READ_ONLY`, because the domain is the authority and a Keycloak that can write
back is a Keycloak that can disagree with it.

The FreeIPA half is applied and proven: the account binds and reads the user
tree. The Keycloak half is written and gated behind `keycloak_ready`, waiting
on the same power cycle Keycloak itself is waiting on.

## Run Record — 2026-08-16, the GitOps source

Closes a hole that had been open since 6a and was not on any list: **the
cluster-state repository in GitLab held the only copy of every manifest the
cluster runs on.**

`OVERVIEW.md` claims the whole internal environment can be rebuilt from this
repository alone. `repo01` is one of the things a rebuild rebuilds, and GitLab
goes with it — so the claim was true of every layer except the one that
describes what the cluster actually does. A rebuild would have reached a healthy
control plane and then stopped, with nothing to reconcile.

Four things existed nowhere in code: the `platform/cluster-state` project, the
53 manifests in it, the `flux bootstrap` invocation, and seven SealedSecrets
whose plaintext was recoverable only by decrypting the thing it was supposed to
be the source of.

| Step | Result |
| --- | --- |
| Manifest tree vendored | 47 templates under `files/gitops_source/cluster-state` + the pinned `gotk-components.yaml` under `files/gitops_source/cluster-state-static` |
| Environment identity templated | Cluster subdomain, LB pool, DNS and ingress addresses, registry host, repository URL. Versions and sizing stay literal — those are decisions about the cluster, not facts about where it runs |
| Secrets regenerated, not carried | Six SealedSecrets sealed at play time from `env.sh` and from `ipa_sub_ca`'s output |
| `flux bootstrap` dropped | `gitops_bootstrap` installs the vendored components and applies the sync objects; Flux self-manages from Git afterwards |
| `GITLAB_FLUX_TOKEN` retired | Replaced by a `read_repository` project deploy token the role creates and records |
| Sealing key restore automated | Applied before Flux installs the controller, and only when the cluster has none |
| Round trip proven | Render, push, reconcile — five Kustomizations and eight HelmReleases ready against the Ansible-pushed revision |
| Second run | No commit, no push, no change: `6 sealed secrets: 6 unchanged` |

### Why the secrets are regenerated rather than committed

Committing the sealed files would have been a two-line change and it is the
wrong one. Ciphertext is only decryptable by the sealing key that produced it,
so a cluster rebuilt with a fresh Sealed Secrets key finds every secret in its
own Git history useless — and the rebuild stops at the first component that
needs a credential. The property being restored here is *rebuild from this
repository*, and ciphertext does not have it.

Sealing at play time makes the plaintext the source and the ciphertext a build
product, which is the relationship every other file in the rendered tree
already has with its template.

The cost is that sealing is randomised — a fresh session key per invocation —
so a naive implementation would commit a different ciphertext on every run, and
every commit is a revision Flux reconciles the whole cluster against. The fix
is available only because the controller holds the sealing key backup:
`kubeseal --recovery-unseal` decrypts what is already committed, and the role
re-seals only what actually differs.

**Three plaintexts had to be recovered before any of this could work.**
`KEYCLOAK_ADMIN_PASSWORD`, `KEYCLOAK_DB_PASSWORD` and `GRAFANA_ADMIN_PASSWORD`
were in `env.sh` nowhere and in GitLab only as ciphertext. They were recovered
with the backed-up sealing key, verified byte for byte against the live
Secrets, and written to `env.sh`. `cluster_services.yml` had been reading
`KEYCLOAK_ADMIN_PASSWORD` from an environment that never held it.

That recovery is also the second restore test of the sealing key backup, and
this one was non-destructive: all thirteen decrypted values matched the live
cluster's Secrets exactly.

### Four faults the render found before Flux did

The role has a `gitops_source_push=false` gate that renders, seals, and reports
the diff without committing. Every one of these was caught by reading that diff
rather than by watching a reconcile fail:

- **A templated dict key is not templated.** Ansible does not template mapping
  keys in a vars file, so the registry pull credential was built with an
  `auths` entry literally named `{{ gitops_source_registry_host }}` — a valid
  docker config matching no registry. Flux would have pulled anonymously and
  failed with a 401 that reads as a broken deploy token. Building the mapping
  as a Jinja expression fixes it.
- **`lookup('file')` strips the trailing newline.** Every PEM lost its last
  byte, which re-sealed the CA on every run and would have put a subtly
  different key in the cluster. `rstrip=false`.
- **An indented Jinja comment leaves its indentation behind.** A note inside
  the CoreDNS Corefile pushed the `answer` line 24 columns right. At column
  zero `trim_blocks` removes the block entirely.
- **`stringData` never settles under `kubectl apply`.** It is write-only — the
  API server consumes it into `data` and the stored object never has it back —
  so the three-way merge patches it on every run and reports `configured` on a
  Secret that has not changed. Send `data`.

Two more of the same family showed up in the bootstrap role: applying the
`flux-system` Namespace separately fights Flux for ownership of an object that
is already the first entry in the component set, and applying the component set
on every run makes Ansible a second manager of everything Flux manages. Both
are now gated on Flux being absent, which is what a bootstrap should have meant
in the first place.

### What is now true that was not

The direction is one-way. A file edited in GitLab survives until the next run
of `playbooks/gitops.yml` and is then overwritten, because the render wipes the
working tree before writing it. That is the point rather than a side effect: a
repository that absorbs edits from both ends has two sources of truth and
therefore none.

`site.yml` runs the sequence a rebuild needs: artifacts and node rewrites,
then the GitOps push and Flux, then the initialisation of what Flux deployed,
then the GitOps push a second time. The second pass exists for one reason —
OpenBao's unseal keys cannot predate the vault, and the vault is deployed from
the repository the first pass creates. `cluster_services.yml` was split at that
seam, with the initialisation plays moving to `cluster_init.yml`.

That second pass does not do what this paragraph originally claimed, and the
correction is below under *The vault was built, wired, and left empty*: it reads
the unseal keys through `lookup('env')`, which sees the environment the process
started with rather than the `env.sh` the first pass wrote. Sealing them takes a
re-source and a second invocation, not a second play in the same run.

## Status

**6a delivered.** Flux reconciles the cluster from GitLab with no route to the
internet, Sealed Secrets is the first component it manages, and the controller's
own dependencies are documented in `plan/CONTROLLER.md`.

**Phase 6 delivered.** Flux reconciles the whole cluster from GitLab with no
route to the internet: five Kustomizations and eight HelmReleases all ready,
and no pod outside Running or Completed.

Metrics, logs and traces are all verified end to end — 45 scrape targets, a
Loki query returning labelled pod logs, and an OTLP span pushed through Alloy
and read back out of Tempo through Grafana.
cert-manager, Longhorn, the LoadBalancer layer, the cluster subdomain, OpenBao,
the unsealer loop, ESO and Garage are running and tested. Both items 6a carried
forward are closed: the sealing key has been restore-tested destructively, and
the controller's trust state is scripted.

Keycloak's manifests are committed and its images are mirrored. It starts and
dies on a CPU model that predates x86-64-v2, and the one-field fix in the VM
definitions is committed but not applied, because applying it power-cycles
every VM in the estate.

Outstanding, and both are 6b's own work rather than carried debt:

**Nothing is outstanding.** The VM CPU change was applied on 2026-08-16 and the
estate power-cycled; Keycloak and Tempo both start, and the cluster came back
from a cold stop of every node with OpenBao unsealing itself and all eight
Longhorn volumes attaching healthy — a stronger proof of the unsealer than the
pod restart it was tested against.

### What the power cycle proved on the way past

The CPU change is invisible in `/proc/cpuinfo`'s model name, which still reads
`QEMU Virtual CPU version 2.5+`. The flags are what changed, and they are what
matters: `sse4_2`, `popcnt` and `ssse3` are present, which is x86-64-v2.

### FIPS is the environment's property, not Keycloak's

Dev is outside the FIPS boundary and production is inside it, and both use the
same manifest — so FIPS is a Kustomize **component** a cluster opts into, left
commented in `dev-lo`'s kustomization so the choice is visible in the
environment that made it.

Enabling it takes two settings, and needing both is the trap. `KC_FIPS_MODE`
alone fails at build time with *"FIPS mode cannot be enabled without enabling
the FIPS feature --features=fips"* — after a Java stack trace long enough to
bury the sentence that names the fix. The feature flag loads the BouncyCastle
FIPS provider; the mode decides how strictly it is enforced, and asking for
enforcement of something never loaded is what dev was doing.

The component uses `strict`, because a boundary that permits the algorithms it
exists to exclude is not a boundary. Three consequences production should meet
on purpose rather than by surprise: the admin password must satisfy the minimum
length FIPS requires, any keystore Keycloak reads has to be BCFKS rather than
PKCS12, and password hashing moves inside the validated set — so hashes created
outside FIPS mode may not verify inside it.

### Identity is federated, and the role had to learn to reconcile

Keycloak reads users from FreeIPA over LDAP. The bind account is a system
account under `cn=sysaccounts` — no POSIX identity, no Kerberos principal — and
the provider is `READ_ONLY`, because the domain is the authority.

Three faults, and the second is the general one:

- **The domain calls the identity host `core.dev.lo`.** Inventory calls it
  `core01`, and only the domain's name resolves. Keycloak reported
  `UnknownHost`, which is a DNS failure wearing an LDAP error's clothes.
- **The role created the provider and never updated it**, so the corrected host
  name was committed, applied, and not in effect — every run found a provider
  by the right name and left its wrong configuration alone. A create-only role
  is one that can only ever be right the first time.
- **Keycloak's generated mapper takes the first name from `cn`**, which is
  correct for the vendor default and wrong for FreeIPA, where `cn` is the full
  name. Pointed at `givenName` instead.

Proven with a real user: created in FreeIPA, it appeared in Keycloak with
`source=FreeIPA` and its name split correctly. The IPA `admin` account
deliberately does not federate — it predates `inetOrgPerson` and lacks it,
which is also why it cannot collide with Keycloak's own admin.

### Tempo needed three fixes, and two were the chart's shape

- `extraEnv` belongs under `tempo`, not at the top level. The chart accepts the
  top-level key silently and renders a pod with no environment at all, so Tempo
  started, could not authenticate to Garage, and exited 2 having logged one
  line saying it started.
- `memBallastSizeMbs` defaults to 1024 — a one-gigabyte ballast inside a 512Mi
  limit, which is a pod that cannot start on arithmetic alone.
- `extraPorts` belongs to `alloy`, not `controller`. Accepted in the wrong
  place, ignored, and reported as a successful upgrade — so Alloy's OTLP
  receivers listened on every pod and were reachable from nowhere.

That is now five faults in this phase of exactly one shape: **a value accepted
where it means nothing.** The rule that catches all of them is to render the
chart and read what it actually produces, rather than trusting that a key was
understood because it was not rejected.

### The vault was built, wired, and left empty

Found on 2026-08-16, after 6c was recorded as delivered. The store was correct
in every part — KV v2 mounted, a read-only policy, Kubernetes auth bound to one
ServiceAccount in one namespace, and a `ClusterSecretStore` pointing at it — and
**nothing in this repository ever wrote a value into it.** Six runtime secrets
were sealed into the cluster-state repository instead, which is the arrangement
the research section above explicitly rejected.

It survived review because the parts that were built were the parts that get
tested. `openbao_config` asserts its role binding is narrow, the unsealer loop
was proven against a cold stop of the estate, and one workload did read a secret
through the vault end to end. But that one entry, Garage's S3 key, had been
written **by hand** during the 6c run — the run record calls it "the first
workload to use the secrets model end to end", and it was, once, for as long as
the vault kept the value nobody had automated putting there. A rebuild would
have produced a Loki that could not reach its object store, from a repository
that looked complete.

The fix is the `openbao_secrets` role, which is the writing half the design
always implied: it runs on the controller in `cluster_init.yml` directly after
`openbao_config`, reads each entry before writing it so a no-op run creates no
new KV version, and confirms every entry reads back through the same path a
workload uses. Keycloak, Garage and Grafana now read their credentials from an
`ExternalSecret` in the directory their SealedSecret used to occupy, and what
remains sealed is the three-item set the research section named: the registry
credential Flux pulls charts with, the CA key pair cert-manager issues from, and
the vault's own unseal keys.

**On the cluster that already exists, the order of the two playbooks matters
once.** Flux prunes, so the push that replaces each `SealedSecret` with an
`ExternalSecret` deletes the Secret the workload is using and creates an object
that syncs a value from an empty vault. Fill the vault first:

```bash
source ~/.config/rke2lab/env.sh
ansible-playbook playbooks/cluster_init.yml --tags openbao_secrets
ansible-playbook playbooks/gitops.yml
```

A cold rebuild has no such ordering problem, because there is nothing running to
take the Secret away from. After it, re-source `env.sh` and re-run
`cluster_init.yml` for the entry below.

Two things follow that are worth stating rather than discovering:

- **Garage now waits on External Secrets to start**, where before its
  credentials were already in Git by the time Flux created it. `garage_init`
  gained an explicit readiness wait, with retries as well as a timeout, because
  `kubectl wait` fails immediately rather than after the timeout when the pod
  does not exist yet.
- **`kv/garage` is written one run late, and no ordering fixes it.**
  `lookup('env')` reads the environment the `ansible-playbook` process started
  with, so the S3 credentials `garage_init` appends to `env.sh` are invisible to
  the run that created them. The same is true of `OPENBAO_UNSEAL_KEYS` in
  `gitops.yml` — which means `site.yml`'s second `gitops.yml` pass does not
  actually seal them on a cold rebuild, and the note above claiming it does is
  wrong about the mechanism. Both need `env.sh` re-sourced and the playbook run
  again.

The decisions Phase 6 inherited are settled: OpenBao in-cluster with Sealed
Secrets holding the unseal keys, the seal kept swappable toward PKCS#11,
Longhorn at two replicas, and the storage gate that turned out to be `repo01`'s
root disk rather than the thin pool everyone was watching.

---

## Single sign-on across the platform — 2026-08-17

Every service with a login now authenticates against Keycloak, and every one
that could keep a local way in kept one. **Applied to the live cluster on
2026-08-17**, and a second run of `cluster_init.yml` reports `changed=0`.

Keycloak was already running by then — the `cpu_type` fix had landed — so
everything gated on `keycloak_ready` executed. Four faults surfaced in the
process, all of them in this repository's own code rather than in any product,
and all four are recorded at the end of this section because each was invisible
until it ran.

### The model

Two FreeIPA groups per application, `<app>-admins` and `<app>-users`, mapped to
two Keycloak client roles, `admin` and `user`. Granting access is
`ipa group-add-member grafana-users --users alice` and nothing else.

Two tiers rather than a per-service role model, because an access model that
cannot be stated in a sentence is not one. Each service decides what the two
mean in its own terms: Grafana turns them into Admin and Viewer, OpenBao into
two policies, oauth2-proxy into permission to reach Longhorn at all.

Where each half lives is the part worth defending. FreeIPA holds who someone is
and who is in which group, because the federation is `READ_ONLY` and the domain
is the authority. Keycloak holds only the sentence FreeIPA cannot express —
that members of `grafana-admins` are administrators of Grafana — because
FreeIPA has no concept of a Keycloak client role. Nobody is ever added to a
group in the Keycloak console.

### Applications moved out of `master`

`keycloak_ldap` originally federated the directory into `master`. That worked
and was the wrong realm: master administers every other realm and holds
Keycloak's own administrator, so every federated end user in it was a user in
the administrative realm. A new `keycloak_realm` role creates `dev-lo` and the
federation moved into it — done now, before the first client existed, because a
realm with clients in it is a migration rather than a rename.

`keycloak_ldap_realm` lost its default rather than gaining a new one. A default
of `master` is worse than none: a caller that forgot to pass the realm would
federate the domain into precisely the place it must not go, and would do it
silently.

**On the cluster that already exists**, the provider left behind in `master`
is harmless and should be deleted by hand once `dev-lo` is proven — nothing
removes it, because a role that pruned realms it did not create is a role that
deletes someone else's work.

### Four services, three cases

- **Grafana, OpenBao and GitLab** speak OIDC natively and were configured.
- **Longhorn** has no authentication of any kind. Not a weak default, not a
  password to change — the UI served its whole management API to anyone who
  could resolve `longhorn.k8s.dev.lo`. This was the largest single gap in the
  cluster and the only one that could not be closed by configuring the service,
  because there is nothing in Longhorn to configure. An **oauth2-proxy** now
  fronts it, in full reverse-proxy mode.
- **Garage** does not federate and is not a gap. It speaks S3 and an admin
  bearer token; there is no OIDC in it. Its keys stay in the vault.

Reverse proxy rather than a Traefik `forwardAuth` middleware, which is what
most guides describe. `forwardAuth` answers 401 for an unauthenticated request
and Traefik cannot turn that into a redirect, so a first-time visitor gets a
blank error page rather than a login screen — fixable with a second Ingress
path and an errors middleware, none of which reads as one object in Git. And a
`Middleware` can only be referenced by an Ingress in its own namespace unless
Traefik is started with cross-namespace references enabled, which is a
cluster-wide loosening for one UI. Reverse proxy mode is one Deployment, one
Service, one Ingress, and every byte of UI traffic through a pod nobody keeps
open.

### The client secrets are authored, and that is the whole of the ordering story

The obvious design has Keycloak generate each client secret and Ansible read it
back. It creates an ordering problem that ordering cannot fix: Grafana and the
Longhorn proxy read their secret from a Kubernetes Secret that External Secrets
syncs out of OpenBao, so the vault entry could not be written until Keycloak was
running — and Keycloak is deployed by Flux, from a repository GitLab serves,
after OpenBao. On a cold rebuild the dependents come up first and wait on a
Secret that cannot exist. Every federated service would need a second pass, the
way `kv/garage` does, with nothing forcing it.

Authoring the secret removes the cycle instead of sequencing around it. The
value is in `env.sh` before either end exists; `openbao_secrets` writes it on
the first pass, so each workload starts holding a credential for a client that
does not exist yet; `keycloak_clients` sets that same value on the client when
Keycloak is reachable. Both ends are configured independently and meet when
both are up. Nothing waits on anything, and **single sign-on adds no new
two-pass step to a cold rebuild** — which, given this phase already has two, was
the bar worth clearing.

It is the argument `GARAGE_RPC_SECRET` already carried: a credential named by
two configurations has to exist before either of them does.

### What happens when Keycloak is down

The table is in `SECRETS.md` and the reasoning is here. The dependency that had
to be avoided is OpenBao's, and it inverts the usual direction: **the vault
holds Keycloak's own database password**, delivered by External Secrets. A vault
openable only through Keycloak could not be opened on the day Keycloak will not
start — which is precisely the day someone needs that password.

So `openbao_oidc` is additive by construction, and three things keep it that
way. The root token is never replaced. Neither policy it writes can seal,
unseal, rekey, or change an auth method or a policy — an administrator who
signed in through Keycloak can read and rewrite secrets, and cannot lock the
vault or grant themselves a way back in. And the unsealer loop reads a
SealedSecret and does not authenticate at all, so restart recovery is
untouched. The role asserts before it finishes that the root token still carries
the `root` policy.

GitLab's federation is additive for the same shape of reason one layer out:
Flux reads the cluster-state repository from GitLab and every image the cluster
pulls comes out of its registry, so a GitLab administrable only through Keycloak
could not be administered on the day the cluster is what is broken. Root keeps
its password and the local form stays on the login page —
`omniauth_auto_sign_in_with_provider` is deliberately not set, because it leaves
the form reachable only by appending `?auto_sign_in=false`, and a break-glass
path nobody can remember is not one.

**Longhorn is the one accepted regression**: its UI is unreachable when Keycloak
is down, because the proxy is the only front door. Nothing that matters depends
on it — the data plane, the CSI driver and every volume keep working, `kubectl`
is unaffected, and the UI comes back by pointing the Ingress at
`longhorn-frontend`. It is a straight trade of "no authentication, always
reachable" for "authenticated, occasionally not", and only the first of those
was ever a real position.

### Two things that will not be found by reading Keycloak

**Groups import on synchronisation, not on login.** A group created in FreeIPA a
minute ago is not in the realm, so `keycloak_clients` — which reads the realm's
groups to bind them to client roles — would fail on a group that plainly exists
in the directory. `keycloak_ldap` therefore forces a `fedToKeycloak` sync after
configuring the mapper rather than waiting for a periodic one.

**A role the service cannot see is not an authorisation model.** Keycloak issues
client roles inside `resource_access.<client>`, which oauth2-proxy reads
natively and Grafana and OpenBao do not. Each client gets a protocol mapper
writing its own roles into a flat `roles` claim, so all three consumers read the
same shape. Leaving it out produces a realm that looks correct in the console
and a service that authenticates everyone and authorises nobody.

### One limitation, recorded rather than worked around

**GitLab CE cannot map an OIDC claim to administrator.** Group-to-admin sync is
an Enterprise feature, and this is Community Edition for the same licensing
reason OpenBao is here rather than Vault. So `gitlab-admins` is real in FreeIPA,
real in Keycloak, and ignored by the one service it names: making somebody a
GitLab administrator is still done in GitLab, by root.

The group is kept anyway, and not as decoration. It keeps one naming convention
across every service, it is the list to consult when deciding who should be an
administrator there, and it is what an upgrade to EE would start consuming with
no change to the directory.

### Four faults, found by running it

None of these could have been found by reading. Each is recorded with what it
looked like, because in every case the symptom pointed somewhere other than the
cause.

**1. The admin token expires mid-run.** `keycloak_clients` obtained one token
and used it for every call. **Keycloak's `master` realm issues 60 second access
tokens by default**, and the role makes about a dozen calls per application — so
the fourth application's last call came back `401 Unauthorized`, after
seventeen successful changes. The realm was left half configured, and the error
reads as a permissions problem rather than an expiry. The fix is a `token.yml`
included at each point where a slow step might just have happened: per
application, and again before the group grants. `keycloak_ldap` has the same
exposure around its group sync — the one step there that can take real time —
and got the same treatment.

Raising `accessTokenLifespan` on `master` would also have worked and is the
wrong fix: that realm's token lifetime is a security property of the
administrative realm, not a knob for a play's convenience.

**2. OpenBao could not verify Keycloak's certificate.** The OIDC config write
failed with `400: error checking oidc discovery URL` — an error that names the
URL and says nothing about certificates, and which looks exactly like a wrong
realm name or an unreachable Keycloak. The vault fetches the discovery document
itself and **the OpenBao container's trust store does not hold the domain CA**.
Fixed with `oidc_discovery_ca_pem`, read from the controller's trust store at
run time; no change to the StatefulSet.

The general lesson is worth stating because it was missed once already: *every*
workload that talks to Keycloak over the ingress needs the domain CA, and all
three take it differently — Grafana as `tls_client_ca`, oauth2-proxy as
`--provider-ca-file`, OpenBao as a PEM in its own API. Grafana's and the
proxy's were written in from the start; OpenBao's was not, and nothing about
the other two suggested it would be needed a third time.

**3. An assertion that checked prose instead of policy.** The guard proving no
OIDC policy can seal or rekey the vault searched the policy text for `"root"` —
and matched the word in the policy's own comment, *"sealing... stay with the
root token"*. A correct policy failed its own safety check. It now strips
comment lines before looking, and checks the paths and the `sudo` capability
rather than words. The narrower point: a check that reads a file as a string
must read what the machine reads, not what the author wrote.

**4. A mapper that reported changed forever.** `groups.ldap.filter` was set to
an empty string, and **Keycloak discards config keys whose value is empty** — so
it never read back, the comparison saw a difference on every run, and the group
mapper was rewritten each time with nothing actually changing. Removing the key
fixed it; an absent filter is the default and means the same thing. This is
what the `changed=0` requirement is for: the run was otherwise correct, and
without that requirement the role would have been shipped rewriting a mapper on
every play for the rest of its life.

Two of the four — the token expiry and the empty-string key — also dumped a
credential into the failure output, because the affected tasks lacked `no_log`.
Every `uri` task in `keycloak_clients` now carries it, and the assertion in
`openbao_oidc` loops over a `(name, policy)` pair rather than the registered
result, so a failure cannot print the request that fetched it.

### Verified on the live cluster

- Realm `dev-lo` created; all eight FreeIPA groups imported; four confidential
  clients, each with `admin` and `user` roles, a `roles` claim mapper, and both
  group grants — sixteen objects, all confirmed by reading them back.
- `longhorn.k8s.dev.lo` returns `302` to the `dev-lo` realm for an anonymous
  request; `grafana.k8s.dev.lo/login/generic_oauth` does the same, with PKCE.
- **The oauth2-proxy recovered on its own.** It was `CrashLoopBackOff` for the
  window between the GitOps push and the realm existing — `Realm does not
  exist` — and came up with no restart and no second pass once
  `keycloak_clients` ran. That is the authored-secret design working: its
  ExternalSecret had synced long before, holding a credential for a client that
  did not yet exist.
- A real user proves the whole chain. `ssotest` was put in `grafana-admins` and
  `openbao-users` in FreeIPA only; Keycloak showed exactly those groups, and a
  generated example token carried `roles: ["admin"]` for the grafana client,
  `roles: ["user"]` for openbao, and **no roles claim at all for longhorn** —
  which is the proxy refusing someone who authenticated perfectly well. The
  user was removed afterwards.
- Break-glass, all four: the OpenBao root token still carries the `root` policy
  and `token/` sits beside `oidc/` in the auth methods; Grafana's local admin
  authenticates against the API; GitLab's sign-in page shows the SSO button
  *and* the password form; Keycloak's own `admin` remains in `master`.
- GitLab's container reaches the discovery document over TLS, proving the
  domain CA landed in `/etc/gitlab/trusted-certs`.
- `cluster_init.yml` reports `changed=0` on a second run.

### Cost

One image — `quay.io/oauth2-proxy/oauth2-proxy:v7.15.3` — which is a new quay.io
namespace in `registries.yaml` and therefore **one rolling restart of all six
nodes**, on the same terms every registry rewrite change has carried since 6b.
Everything else federates by configuration.

### Test

- A member of `grafana-admins` signs in to Grafana and lands as Admin; a member
  of `grafana-users` lands as Viewer; someone in neither is refused rather than
  admitted as a Viewer.
- `longhorn.k8s.dev.lo` redirects an anonymous browser to Keycloak, and returns
  the UI after sign-in. A user in neither Longhorn group is refused by the proxy
  after authenticating successfully.
- `bao login -method=oidc` works from a terminal, not only from the UI — the
  `localhost:8250` callback is what makes the difference.
- **The root token still works after `openbao_oidc` has run.** The role asserts
  it; check it by hand once anyway.
- GitLab shows the sign-in button, and `root` still signs in with its password
  beside it.
- Removing a user from a FreeIPA group removes their access at their next
  sign-in, with nothing done in Keycloak or in any service.
- A second run of `cluster_init.yml` reports no change.

## The directory connection is encrypted — 2026-08-17

**Applied to the live cluster and proven on the wire.** `tcpdump` on `core01`
during a forced full sync counted 55 packets on 636 and zero on 389.

Keycloak reached FreeIPA over `ldap://core.dev.lo:389`. That was a decision on
record, not an oversight, and the reasoning it was defended with is worth
keeping visible now that it has been reversed:

> the connection crosses the internal network between two hosts in the same
> domain, and terminating it in FIPS strict mode would require Keycloak to hold
> a BCFKS truststore — a second keystore format to generate and rotate for a
> hop that never leaves 192.168.2.0/24.

Three things are wrong with it, and only the third is about cryptography.

**The bind is `authType: simple`.** So the exposure was never really the
confidentiality of user attributes, which is what "internal network" is an
argument about. It is the bind account's password, sent in the clear on every
connection, for an account that can read the entire user tree. The thing on the
wire was a credential, not a name.

**The hop is not host-to-host.** Keycloak runs in a pod. The traffic crosses
the pod network, the node's bridge, and the LAN between two VMs on a hypervisor
that is itself a VM. "Two hosts in the same domain" describes a topology this
lab has not had since Keycloak moved into the cluster — the sentence was true
when it was written and quietly stopped being true.

**The BCFKS cost was priced for the wrong cluster.** It applies under
`KC_FIPS_MODE=strict`, which is a Kustomize component `dev-lo` does not enable.
Outside strict mode Keycloak reads PEM from `conf/truststores` and builds the
truststore itself: no keystore file, no format, nothing to rotate beyond the CA
that was already being distributed to three other namespaces.

### What it took

Less than the comment claimed, which is the general lesson.

- `ldaps://core.dev.lo:636`. FreeIPA has served 636 since Phase 2 — the
  container runs with host networking precisely so it can — so the directory
  side needed nothing at all.
- A `keycloak-domain-ca` Secret mounted at `/opt/keycloak/conf/truststores`.
  Keycloak scans that directory at startup with no `KC_TRUSTSTORE_PATHS` set,
  and adds to the JRE's roots rather than replacing them. The bytes are read
  from the controller's trust store at render time, the same arrangement the
  registry and Grafana CA Secrets already use.
- `useTruststoreSpi: always` was already on the provider, and only now means
  anything.

The host name earns its comment twice over. `core.dev.lo` rather than
`core01.dev.lo` was a DNS fix; under LDAPS the same name also has to match the
certificate 389-ds presents, which is issued for the host as the domain knows
it. The inventory name would now fail twice for two different reasons.

### What production still owes

Upstream is explicit that in strict mode the default truststore type is BCFKS
and that `jks` and `pkcs12` are unsupported. It says nothing either way about
the PEM material `conf/truststores` loads, and that gap is not something to
resolve by reading the source and hoping. The first cluster to enable the FIPS
component has to prove the federation still connects and convert the CA to
BCFKS if it does not. The note is in the component itself, where whoever
enables it will be looking.

Reverting to plain LDAP is not the fallback. A directory-wide bind credential
in the clear is a worse answer inside the FIPS boundary than outside it.

### Applied, in the order the ordering mattered

The manifest first, the provider second. Reversed, the provider would point at
a TLS endpoint before Keycloak held the CA to verify it, and the failure would
be a live single sign-on outage between the two steps.

1. `gitops.yml --tags gitops_source` pushed the CA Secret and the mount; Flux
   reconciled and the Deployment rolled. Startup logged the CA loaded from
   `conf/truststores` before anything depended on it.
2. `cluster_init.yml --tags keycloak_ldap` moved the provider to
   `ldaps://core.dev.lo:636`.

### Verified

- **On the wire.** `tcpdump` on `core01` during a forced full sync: 55 packets
  on 636, **zero on 389**, from `192.168.2.32`. This is the only check that
  proves the property the change exists for; everything else proves it still
  works.
- `testConnection` and `testAuthentication` both return 204 against the LDAPS
  URL — the checks that distinguish a provider that is present from one that is
  usable, and with TLS in the path they now also prove the truststore mount.
- The realm still holds 2 federated users and 12 groups, and a triggered full
  sync reports `0 imported users, 2 updated users` — the group mapper survived
  the transport change.
- A second `gitops_source` run reports *"cluster-state is already in sync with
  the template tree"*, and `cluster_init.yml` reports `changed=0`.

### One thing the apply exposed, not caused

`cluster_init.yml` reported `changed=0` on the run that moved the provider from
`ldap://` to `ldaps://`. The federation genuinely changed; the run said it had
not.

The cause is that `federation | Update the existing provider to match this
definition` carries no `changed_when`, so its PUT always reports `ok` — while
the two group-mapper tasks beside it set `changed_when: true` and compare the
existing config first, which is the pattern the provider task never got. The
provider PUT is also unconditional, so simply adding `changed_when: true` would
trade a task that never reports change for one that always does.

It is pre-existing and it is not cosmetic: `changed=0` is the signal this
repository uses to mean *converged*, and a role that can rewrite the identity
federation without disturbing that number can hide the next change the way it
hid this one. **Recorded here rather than fixed**, because the fix is the
config comparison the group mapper already demonstrates and it belongs in a
change of its own, where a second run can prove it.

## Sign-in was never actually tried — 2026-08-17

**Applied to the live cluster and verified.** Everything the section above
recorded as verified was verified *from the outside*: anonymous redirects,
generated example tokens, group-to-role mappings read back through the admin
API. No one had typed a password into any of the four services. The first
interactive login found a defect that none of those checks could reach, and
the shape of the miss is the part worth keeping.

### Longhorn refused every login after Keycloak accepted it

`longhorn.k8s.dev.lo` authenticated correctly at Keycloak and then answered the
callback with **HTTP 500**, for both the administrator and the ordinary user:

```
Error redeeming code during OAuth2 callback:
  email in id_token (jmarchetti.adm@dev.lo) isn't verified
```

FreeIPA has no notion of a verified address — an account's `mail` is set by an
administrator in the domain, which is a stronger assurance than the click-a-link
flow the flag was invented for — so Keycloak imports every federated user with
`emailVerified` false. oauth2-proxy refuses on that alone. `--email-domain=*`
does not cover it: that filters *which* domains are acceptable and is a separate
check from whether the address is verified at all.

Nothing upstream could have caught this. The roles were right, the token
carried `resource_access.longhorn.roles: ["admin"]` exactly as designed, and the
proxy's `--allowed-role` would have matched it — the request never got that far.
The claim that decided the outcome was one no check was looking at, because it
is not part of authorisation.

Fixed at the directory's edge with a `hardcoded-attribute-mapper` on the
federation provider, not with the proxy's
`--insecure-oidc-allow-unverified-email`. The flag is per-service, so the next
service put behind a proxy meets the same wall; and it is named `insecure`
because it tells a proxy to stop checking. The mapper makes the claim true once,
for the whole realm, and it is honest — FreeIPA *is* the authority for these
addresses.

A mapper only shapes a user as they are imported, so adding it changed nothing
about the two users already in the realm. `keycloak_ldap_user_sync` triggers a
`triggerFullSync` after the mappers are written, for the same reason
`keycloak_ldap_group_sync` exists. `triggerChangedUsersSync` would not do:
it selects on the directory's modify timestamp, and correcting a mapper on
Keycloak's side does not touch it — every user would be skipped, which looks
exactly like a successful sync.

### OpenBao granted a path that does not exist

`sso-admins` carried `path "auth"`, which is not a path the vault serves — it is
the prefix each auth *method* is mounted under. The grant matched nothing,
returned 404 to anyone who tried it, and read in a policy review as though the
capability were present. The list of enabled auth methods, which is what the
UI's Access tab asks for and what the policy's own comment means by "diagnose
the vault", lives at `sys/auth`. Corrected, and still read-only: enabling or
tuning a method is `sys/auth/*` with `update`, which would let a Keycloak
session grant itself another way in.

The rest of the report that prompted this — that OpenBao gave no real
administrative access — did not survive testing. A token carrying `sso-admins`
lists `kv/metadata`, reads `kv/data/keycloak` (both `admin-password` and
`db-password` visible), and writes; `sso-users` reads and cannot write; the
UI's engine list resolves. The identity entity created by the administrator's
own past login is a member of `sso-admins`. Policies attach **at login**, so a
session opened before `openbao_oidc` bound the group aliases carries none of
this until the next sign-in — which is the most likely explanation, and is worth
knowing before reaching for the policy.

### The guard that had been reading its own prose

Correcting the policy tripped `policies | Assert no OIDC policy can seal, rekey
or grant access` — on a **comment** mentioning `sys/auth/*`. The assertion
strips comments before checking, precisely so that the explanations do not fail
the check they explain. It had never worked: inside a folded scalar the `'\n'`
given to `.split()` never became a newline, so the policy was one long line, no
line ever matched the comment pattern, and nothing was ever stripped. The guard
had been scanning the prose all along and only passed because no comment had yet
contained a forbidden path.

Rewritten as `regex_replace('(?m)^[ \t]*#.*$', '')`, which anchors per line and
needs no newline literal. A guard that silently checks the wrong thing until the
day it fails on the right one is worse than no guard, because the failure
arrives attached to the wrong cause.

### Still not proven

Grafana and GitLab remain unexercised by an interactive login. Grafana's
configuration is correct — the PKCE redirect resolves, `role_attribute_path`
reads the `roles` claim, `role_attribute_strict` refuses a roleless user — and
Grafana does not check `email_verified`, so the defect above never applied to
it. But **no SSO user has ever signed in**: the org holds only the local admin.
That is the same gap this section is about, and it is recorded rather than
claimed.

GitLab maps no administrator role and never will here: group-to-admin sync is an
Enterprise feature, so `gitlab-admins` is issued in the token and ignored, and
administrator rights are granted in GitLab by `root`. That is by design and is
already recorded in the role's defaults.

A stale comment above Grafana's `allow_sign_up` claimed the setting was off
while the value was `true`. The value is correct and has to be — no Grafana
account exists for a federated user until their first sign-in creates one — and
what keeps it safe is `role_attribute_strict`, not the flag. The comment was
rewritten to say so. It reaches the cluster on the next `gitops_source` render;
nothing behavioural changes with it.

## Run Record — 2026-08-18, 6c follow-up

The observability stack was reported as "performance data only" — Grafana with
numbers in it and nothing else, and alerts that were counts rather than
messages. Every part of that turned out to be true, and none of it was a
collection failure.

| Finding | Reality |
| --- | --- |
| No logs visible | Loki held them all along — 10 namespaces, ~15k lines per 5 minutes. There were **no log dashboards**: all 29 Grafana had came from kube-prometheus-stack and all 29 are metrics |
| Alerts are counts | The counts were the Alertmanager overview panels. 13 alerts were firing with full descriptions, readable only under Alerting → Active notifications with the datasource switched from Grafana to Alertmanager |
| Alerts are wrong | 10 of the 13 were false. RKE2 binds etcd, the scheduler, the controller manager and kube-proxy to `127.0.0.1`; the chart's ServiceMonitors scrape node addresses |

### The alerts described the scrape, not the cluster

`etcdInsufficientMembers` fired — "insufficient members (0)" — while all three
etcd members were `Ready` and serving. The scrape was refused, `up` was 0, and
the rule cannot tell those apart.

`KubeProxyDown` was a different failure wearing the same clothes. The chart's
kube-proxy Service selects `k8s-app: kube-proxy`, which is kubeadm's label;
RKE2's static pods carry `component: kube-proxy` and `tier: control-plane` and
nothing else. The Service matched no pod and had no endpoints, so there was no
`up` series at all — and `KubeProxyDown` alerts on the metric being *absent*,
which fires with no failing target to point at. Fixed with
`kubeProxy.service.selector` rather than by relabelling pods RKE2 recreates.

The rest is `rke2_server_expose_metrics` and `rke2_agent_expose_metrics`, which
add `etcd-expose-metrics` and the three `bind-address` arguments. All four flags
were confirmed against `rke2 server --help` on the running v1.35.7 binary rather
than taken from documentation. They bind to every interface: the scheduler and
controller manager sit behind authn and refuse an unauthenticated scrape, etcd's
2381 and kube-proxy's 10249 do not. Accepted on the lab segment; it would need a
host firewall on a routable network.

### The inotify limit, found by the dashboards it was added to build

While this was in flight, `failed to create fsnotify watcher: too many open
files` appeared in Loki. `fs.inotify.max_user_instances` was at the kernel
default of **128** and UID 0 held **129** — and almost every container on a node
runs as UID 0, so they share one allowance. Kubelet, containerd, Flux's four
controllers, cert-manager, Longhorn, Grafana's sidecars and Alloy all watch
files.

**86 pods** were affected, some thousands of times an hour. Nothing had crashed,
which is the dangerous part: a controller that cannot create a watcher may start
anyway and never notice the change it exists to watch.

The message names the wrong resource. The allocation fails with `EMFILE`, which
usually does mean file descriptors, so it sends you to `ulimit` and `fs.file-max`
where nothing is wrong. Raised to 8192 instances and 524288 watches by
`rke2_node`, written to `/etc/sysctl.d/90-rke2-inotify.conf` **and** applied to
the running kernel — the file alone fixes the next boot and not today. The error
count went to zero within five minutes, across every pod.

Worth recording that this was found by querying Loki, which is exactly the
capability this change existed to expose. It had been happening for as long as
the logs go back.

### Alerting still has nowhere to go

Alertmanager's route to the `null` receiver is left in place, because nothing on
this network reaches a mail server or a webhook. The intended destination is
Splunk or Elasticsearch taking alerts as events, reachable through Alertmanager's
generic `webhook_config` — what is missing is an endpoint and credentials, not a
change of shape.

Noted where it will be needed: declaring `alertmanager.config` **replaces** the
chart's default wholesale, and the default carries the inhibit rules that stop
one critical alert arriving with the three warnings it caused.

## Keycloak had no permanent administrator and no administrators — 2026-08-18

Two gaps, and they were the same gap seen from either end. The account that
could administer Keycloak was the temporary one Keycloak created for itself,
and no person in the domain could administer Keycloak at all.

### The bootstrap account is temporary, and Keycloak means it

`KC_BOOTSTRAP_ADMIN_USERNAME` and `KC_BOOTSTRAP_ADMIN_PASSWORD` create an
administrator on the first start of a Keycloak whose `master` realm does not
exist. Since 26 that account is stamped with the user attribute
`is_temporary_admin`, and the console warns on every session it holds: *create
a permanent admin account and delete the temporary one*. `AdminConsole.java`
reads exactly that attribute to decide whether to warn, so the banner is not
advice about the password — it is a property of the account.

The documented remedy is to create a second administrator and delete the first,
which here would rename the account four Ansible roles, the vault key
`kv/keycloak`, `KEYCLOAK_ADMIN_PASSWORD` and the runbooks all refer to, in
order to arrive at an account with the same name, the same password and the
same role. What makes an account permanent is that its credential has a source
of record. This one has two — `env.sh` and the vault — and reaches the pod
through External Secrets. So `keycloak_break_glass` keeps the account and
removes the marker.

### Removing the marker takes three writes

`is_temporary_admin` is not declared in the realm's user profile, so it is an
**unmanaged** attribute, and a realm whose `unmanagedAttributePolicy` is unset —
Keycloak's default, and this realm's state — silently drops unmanaged
attributes from every write through the admin API. Confirmed against the live
cluster before anything was written: a `PUT` of the user with the attribute
removed answers `204`, and a re-read shows it still there. Nothing in the
response distinguishes that from success.

The role therefore opens the policy, writes the account, and restores the
policy — with the restore in an `always`, because the open window accepts
arbitrary attributes on any account in the administrative realm and a failure
between the two steps would leave it that way silently.

### Nobody in the domain could administer Keycloak

Every other service in this lab is administered by a FreeIPA group. Keycloak
was the exception: the only way in was one shared local password.

`keycloak_realm_admins` grants the FreeIPA group `keycloak-admins` the
`realm-admin` role on the `dev-lo` realm's `realm-management` client — the
client Keycloak creates with every realm except master. Its members sign in at
`/admin/dev-lo/console` as themselves and administer that realm completely.

The grant stops at the realm, deliberately. A `dev-lo` administrator cannot
create a realm, cannot edit `master`, and cannot revoke the local
administrator. The alternative — federating the directory into `master` and
granting its `admin` role — would make the break-glass realm depend on the
directory it is break-glass *for*, and would put every account in the domain
into the realm that administers every other realm in order to give
administrative access to two of them.

`keycloak-admins` is the one group with no `-users` counterpart, which is why
`ipa_sso_groups` grew `ipa_sso_groups_admin_only_applications` rather than
taking `keycloak` in the application list. Every federated account can already
sign in to Keycloak — that is what being federated is — so a `keycloak-users`
group would grant nothing, and a group that grants nothing is worse than no
group: it reads as access in `ipa group-find` and is then found to do nothing.
Keycloak is also not in the application list for a second reason: it is not an
OIDC client of itself, and `keycloak_clients` would have created one.

### The stale provider in `master` was worse than "harmless"

It was carried in `identity.rst` and `common-issues.rst` as stale, harmless,
and to be removed by hand — for as long as removing it by hand did not happen.
What it actually was: a user federation provider still enabled in the realm
that administers every other realm, binding over **plain `ldap://core.dev.lo:389`**
— the unencrypted connection the LDAPS work of 2026-08-17 was supposed to have
ended — and importing domain accounts into `master`. Two were there.

`keycloak_break_glass` now removes any provider it finds in the administrative
realm and then the accounts carrying a `federationLink`, in that order: while
the provider exists, a deleted account is one Keycloak may satisfy from the
directory again. Selection is on `federationLink` and never on a name, so the
task cannot be the reason a local administrator stops existing. The directory
entries themselves are untouched; both accounts keep every access they had,
through `dev-lo`.

### Verified on the live cluster

| Check | Result |
| --- | --- |
| `admin` in master | Present, enabled, holds the `admin` realm role, **no** `is_temporary_admin` attribute |
| master user profile | `unmanagedAttributePolicy` unset — the window closed behind the write |
| master federation providers | 0 |
| master accounts | 1, local, no `federationLink` |
| `keycloak-admins` in FreeIPA | Created, imported into `dev-lo` |
| Its role mapping | `realm-admin` on `realm-management` in `dev-lo` |

### Still not proven

No member of `keycloak-admins` has signed in to `/admin/dev-lo/console`. The
group is empty, for the same reason `ipa_sso_groups` never adds a member: who
administers Keycloak is a decision about people. Add someone and the console
is the test.

### Master was federated after all, deliberately — 2026-08-18

The first version of this stopped at `dev-lo` on the argument that a federated
master makes Keycloak unadministrable when FreeIPA is down. That argument was
overstated, and it was presented as though it were Keycloak's guidance when it
was this repository's convention.

Keycloak's actual guidance is that master holds administrators rather than
application users and business identities. `keycloak-admins` *are* platform
administrators — master is what they are for. What the guidance is against is
putting the domain there, and the answer to that is a filter, not a refusal:

```text
users:  (&(!(nsAccountLock=TRUE))(memberOf=cn=keycloak-admins,cn=groups,cn=accounts,dc=dev,dc=lo))
groups: (cn=keycloak-admins)
```

Verified before building on it: FreeIPA answers a `memberOf` filter for the
Keycloak bind account, returning exactly the group's members — even though the
group mapper cannot rely on the attribute being freely readable, which is why
it still loads groups by member attribute.

Master now holds two accounts and one group: local `admin`, `jmarchetti.adm`,
and `keycloak-admins` carrying the `admin` realm role. The rest of the domain
is not there.

The availability argument survives intact — `admin` is still local and
unfederated, so it still works when the directory does not. What was given up
is the other half, and it is recorded in the role defaults rather than left to
be discovered: **a member of `keycloak-admins` can now disable or delete the
local break-glass account.** Master's administrator is no longer protected
*from* the directory.

Three roles changed shape rather than gaining special cases:

| Role | Change |
| --- | --- |
| `keycloak_ldap` | `keycloak_ldap_user_search_filter` and `keycloak_ldap_groups_filter`; `keycloak_ldap_allow_master_realm` to reach master at all, and a second assertion that refuses master unless *both* filters are set — the flag alone does not distinguish a filtered provider from an unfiltered one. Plus `keycloak_ldap_manage_sysaccount`, so the second invocation does not re-create the bind account |
| `keycloak_realm_admins` | Grants realm roles when no client is named, because master has no `realm-management` client — administering master is not administering an application. Same group, same additive rule, two endpoints |
| `keycloak_break_glass` | `remove_federation` became an allowlist. Accounts are matched to the providers that own them, so allowing a provider no longer means deleting everything it imported |

### The cache, which cost a login

A member of `keycloak-admins` was refused at `/admin/dev-lo/console` with *You
do not have permission to access this resource* — with every mapping in the
realm correct. The group's member list showed them. Their own group list did
not.

Keycloak caches an imported user together with the groups resolved for them,
and **neither sync invalidates it**. The group subtree is re-read, the user
entries are re-read, and the cached association between the two survives both.
`/groups/{id}/members` queries the directory and therefore agreed; the user's
own `/groups` is served from the cache and did not, and every role derived from
the group was missing with it.

`POST /admin/realms/dev-lo/clear-user-cache` fixed it instantly: 0 effective
`realm-management` roles before, 22 after, with nothing else changed.
`keycloak_ldap` now posts it after the syncs, guarded by
`keycloak_ldap_clear_user_cache`. It is reported unchanged, because the cache
repopulates from the database and the directory on the next read.

Worth stating plainly, because the existing note in `managing-services.rst` was
not enough: "a change takes effect at the user's next sign-in" is true and
insufficient. A new *group membership* for an already-imported user is not
visible to any number of sign-ins until the cache is dropped.
