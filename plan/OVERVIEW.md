# Overview

Build a development RKE2 environment, plus all of its external dependencies, inside a
virtual Proxmox environment — fully reproducible from infrastructure as code and
configuration as code.

## Document Map

| Document | Purpose |
| --- | --- |
| `OVERVIEW.md` | Architecture, ground rules, and cross-phase constraints (this file) |
| `PHASES.md` | The six phases and the work flow inside each one |
| `TARGETS.md` | Per-VM specifications: CPU, RAM, disk, NICs, DNS, roles |
| `SECRETS.md` | Where secrets live and how automation reads them |
| `CONTROLLER.md` | The automation controller's dependency manifest and cold start |
| `PROXMOX.md` | Hypervisor and template preparation notes |
| `CLUSTER_COMPONENTS.md` | Cluster software stack selections |
| `ANSIBLE_STANDARDS.md` | Role/playbook conventions all automation must follow |
| `PHASE<N>_IMPLEMENTATION.md` | Detailed execution plan for a single phase |
| `../docs/` | The sysadmin guide — how the built environment is operated |

`PROXMOX.md` also records the hypervisor's storage characteristics, which
constrain etcd and are the reason the Phase 4 control plane needs tuning.

## Operating System

Ubuntu 24.04 for every system in the Proxmox environment.

## Automation Controller

The system this repository lives on. It sits **outside** the Proxmox virtual
environment and runs all Pulumi and Ansible automation.

Every controller dependency must be documented and scripted so the automation
environment can be rebuilt from scratch without tribal knowledge.

**Met as of 2026-08-16, in both halves.** The gap this rule guards is narrower
than it looks and worse than it sounds: the plan could rebuild the whole
Proxmox environment, but only from a controller that was built by hand and
whose build existed nowhere. Lose the controller and the ability to rebuild
everything else goes with it.

`CONTROLLER.md` paid the documentation half in Phase 6a — a version-pinned
dependency manifest and a cold-start order that assumes nothing but a bare
Ubuntu host — and named what writing it exposed: drifted SSH host keys, a
FreeIPA CA missing from the controller's trust store, an Ansible installed into
Homebrew's Python with no virtual environment, and a `controller_setup.sh` that
configured dnsmasq and nothing else despite its name.

The scripted half is now:

- `bootstrap/controller-bootstrap.sh` — the **only** hand-run step, and its
  scope is the one thing automation cannot do for itself: Ansible cannot
  install Ansible. System packages, a virtual environment built from pinned
  requirements, and the pinned collections.
- `playbooks/controller_bootstrap.yml` — split DNS, the `controller_runtime`
  role (packages, both virtual environments, the checksummed Pulumi CLI, the
  shell environment), and the WireGuard tunnel. It is also the first play
  `site.yml` imports, so building the lab from a fresh clone builds the
  controller first.
- `playbooks/controller.yml` — the cluster tooling, which can only run once a
  cluster exists: `kubectl` taken from a cluster node so the versions cannot
  drift, plus `k9s`, the Flux CLI and `kubeseal`, the domain CA in the trust
  store, and every managed host's SSH key in `known_hosts`.

What remains is not automation but state that only a backup can supply:
`~/.config/rke2lab/`. `bootstrap/env.sh.example` reduces that to values rather
than knowledge — a rebuilt controller knows exactly which names it needs.

### Controller-to-Internal Network Access

Automation reaches the internal network (`192.168.2.0/24`) through a point-to-point
WireGuard tunnel terminated on `repo01`.

Design goals:

- Minimal setup and operational overhead.
- No change to the internal network's default gateway for initial rollout.
- A stable L3 path for Ansible SSH and service validation traffic.

Implementation model:

- Point-to-point WireGuard between the controller and `repo01` (`10.66.66.0/30`).
- `repo01` acts as the tunnel gateway into the internal network.
- SNAT on `repo01` for tunnel traffic entering `192.168.2.0/24`, so internal hosts
  need no return route toward the controller.

Both ends are Ansible and have been since Phase 1: `playbooks/tunnel_controller_access.yml`
runs the `controller_tunnel` role here and the `wireguard_gateway` role there, and
`playbooks/controller_bootstrap.yml` imports it as its last play. Since 2026-08-17
neither peer's public key is in inventory — both are derived from the private keys
in `env.sh` — and the playbook proves the path carries traffic before it exits.

## Network and Routing Model

| Network | CIDR | Internet Access |
| --- | --- | --- |
| External / lab | `192.168.1.0/24` | Yes, via `192.168.1.1` |
| Internal | `192.168.2.0/24` | **None** |
| Controller tunnel | `10.66.66.0/30` | n/a (management only) |

`repo01` is the only dual-homed host: `192.168.1.20` externally and `192.168.2.99`
internally. It is deliberately **not** a default gateway to the internet for internal
hosts. Internal nodes have no route off `192.168.2.0/24` other than to `repo01`'s
published services.

This constraint is the reason for everything in the next section.

### DNS

`core01` runs FreeIPA as the authority for `dev.lo`, installed with **no forwarders**:
there is no upstream resolver reachable from the internal network, so forwarding would
only make every non-`dev.lo` lookup hang until it times out. Internal hosts resolve
`dev.lo` and nothing else, which is the intended isolation.

`repo01` is the exception, because it needs both: upstream names to download artifacts
and `dev.lo` names to run GitLab. It resolves upstream through `192.168.1.1` and routes
`dev.lo` queries to `192.168.2.4` over the internal link, configured as a netplan
drop-in by the `base_host` role. Listing both resolvers in one flat list would not work
— a resolver that returns NXDOMAIN is treated as authoritative, so the second entry
would never be consulted.

## Artifact Distribution Model

**Rule: every artifact used in every phase is downloaded to `repo01` first, then
served to internal nodes from `repo01`. No internal node ever reaches the internet.**

Three rules govern how long a copy lives. They were written in Phase 6, after
`repo01`'s root filesystem reached 79% carrying images already published to
GitLab — but they are **Phase 1 rules**, because Phase 1 owns the manifest
(`group_vars/repo/artifacts.yml`) and the staging role that reads it. A rebuild
applies them from the first `repo01.yml` run, not from Phase 6:

- **Fetch only what is missing.** Staging is idempotent; a re-run downloads
  nothing it already has.
- **Keep on `repo01` only what a cold start needs.** An artifact whose final home
  is GitLab is *transit*: it is staged, published, and the local copy removed.
  An artifact consumed from Apache by a host with no other source is *bootstrap*
  and stays.
- **Check presence at the destination, not on disk.** This is what makes the
  first two rules compatible. A role that deletes after publishing and then
  looks at the local disk to decide whether to re-fetch will download everything
  on every run. Ask GitLab what it already holds. `rke2_publish` has done this
  for the registry since Phase 4 and is the pattern to generalise.

**Every artifact is documented, including the controller's own dependencies.**
The manifest is what makes the environment rebuildable, so it must cover the
machine that does the rebuilding — Python, Pulumi, Ansible and its collections,
WireGuard, `kubectl`, `k9s` — not just the machines being built. A manifest that
assumes a working controller cannot recover from losing one.

**There are two manifests, since 2026-08-17.** `group_vars/repo/artifacts.yml`
covers the machines being built and is iterated by `artifact_stage`;
`group_vars/controller/artifacts.yml` covers the machine doing the building and
is looked up by name from three roles. The second one existed only as prose in
`CONTROLLER.md` until then, which met the letter of this rule — every controller
dependency *was* documented and pinned — while leaving the pins scattered across
five files, two of them with the URL in one file and its checksum in another.

The controller downloads from upstream, and that is not an exception to the rule
above. The rule constrains **internal** nodes; the controller sits outside the
Proxmox environment, and a controller that fetched Pulumi from `repo01` could
never build the host serving it. Where a tool is in the *rebuild* path rather
than merely convenient — the Flux CLI, kubeseal — it comes from Apache instead,
so a rebuild does not depend on this host reaching GitHub.

Distribution happens in two tiers. Tier 1 exists from Phase 1 onward; Tier 2 only
exists once GitLab is running, and GitLab itself is delivered by Tier 1.

### Tier 1 — `repo01` (Apache + APT proxy)

The bootstrap tier. Available before any cluster service exists.

| Service | Serves | Consumers |
| --- | --- | --- |
| Apache httpd | Static artifacts over HTTP from `/data1/artifacts` | All internal nodes |
| apt-cacher-ng | Ubuntu `.deb` packages, cached on demand | All internal nodes |

The APT service is a **caching proxy, never a mirror**. It stores only the packages
internal hosts actually request, expires them once no index references them, and does
no scheduled precaching. Cloning whole Ubuntu archives would cost tens of gigabytes of
`/data1` to hold packages the lab will never install. Its CONNECT passthrough is
restricted to the Ubuntu archive hosts, so it cannot be used as general internet egress
by an internal node.

Apache is the artifact host for anything that is not an OS package:

- FreeIPA container images and any files needed to stand up the identity stack.
- GitLab container images and any files needed to stand up GitLab.
- Container runtime packages, Compose binaries, and other host-level prerequisites.
- Any archive, binary, or checksum file needed before GitLab exists.

Because there is no registry in Tier 1, container images are staged as saved image
tarballs (`docker save` / `podman save` output) under the Apache document root and
loaded on the target host. Every artifact is published with a checksum file next to
it, and consumers verify the checksum before use.

### Tier 2 — GitLab (container registry + package registry)

Once GitLab is deployed on `repo01` and `dev.lo` DNS resolves, GitLab becomes the
distribution point for cluster-facing content. As of Phase 5 this is live and
serving the whole cluster: the RKE2 image set is in the container registry under
`rke2/images` — core, Cilium, Traefik and kube-vip — the installer and binary
are in the generic package registry under `rke2/packages`, and all six nodes,
servers and workers alike, pull from both with no route to the internet.

- **All RKE2 container images** — pushed to the GitLab container registry and pulled
  from there by control plane and worker nodes.
- **All RKE2 binaries, tarballs, Helm charts, and packages** — published to the GitLab
  package/generic registry and consumed from there.
- Any GitOps-managed workload images for Phase 6.

The manifests those images are declared by are **not** artifacts and do not
travel this path. They are rendered from `files/gitops_source/
cluster-state` and pushed into GitLab by `playbooks/gitops.yml`, which is the
same rule stated the other way round: what a component *is* comes from this
repository, and what it is *made of* comes from the artifact manifest. Until
2026-08-16 the first half of that had no source at all outside GitLab.

The flow is still `internet → repo01 → GitLab → internal nodes`. `repo01` remains the
only host that downloads from upstream; GitLab is a redistribution point, not a
second internet egress.

### Which tier owns what

| Artifact | Retention on `repo01` | Served from |
| --- | --- | --- |
| Ubuntu packages | apt-cacher-ng cache, on demand | Tier 1 — apt proxy |
| FreeIPA images and install files | **Bootstrap** — kept | Tier 1 — Apache |
| GitLab images and install files | **Bootstrap** — kept | Tier 1 — Apache |
| Container runtime / Compose | **Bootstrap** — kept | Tier 1 — Apache |
| RKE2 images | **Transit** — removed after publish | Tier 2 — GitLab registry |
| RKE2 binaries, charts, packages | **Transit** — removed after publish | Tier 2 — GitLab packages |
| GitOps workload images | **Transit** — removed after publish | Tier 2 — GitLab registry |
| Controller dependencies | Documented in `group_vars/controller/artifacts.yml`; installed on the controller | Upstream at build time, except the Flux CLI and kubeseal — Tier 1 Apache |

Bootstrap artifacts are what a rebuild starts from, before GitLab exists to serve
anything. Transit artifacts pass through.

## Infrastructure as Code (IaC)

Pulumi with Python, in a virtual environment, is the primary IaC interface.
Terraform with the `bpg/proxmox` provider is the fallback.

Rationale:

- Pulumi ProxmoxVE is built on the actively maintained bpg Terraform provider.
- Python-first workflows match the rest of this repo and its Ansible usage.

Guardrails:

- Keep resource definitions modular so they can be ported to Terraform with minimal
  rewrite.
- Treat provider-specific workarounds as explicit, documented exceptions.
- Require a successful Phase 1 dry-run and apply against a non-destructive test target
  before continuing to later phases.

## Configuration as Code

All configuration is done with Ansible in a Python virtual environment, following
`ANSIBLE_STANDARDS.md`. Roles must be idempotent: a second run reports no changes.

## Service Deployment Direction

Both `dev.lo` platform services run as containers, not host-installed packages:

- **FreeIPA** on `core01`, providing LDAP, DNS, NTP, and the certificate authority for
  `dev.lo`.
- **GitLab** on `repo01`, providing Git, container registry, and package registry.

Ordering dependency: FreeIPA DNS must be authoritative before GitLab is deployed, so
`gitlab.dev.lo` and `registry.gitlab.dev.lo` resolve consistently from both the
controller and internal hosts.

### Rules for containerized services

Learned the expensive way while deploying FreeIPA in Phase 2. Every one of these cost
a failed install or a corrupted database, and every one of them applies again to
GitLab in Phase 3 and to anything containerized after it.

- **Read the image's own documentation before writing the role.** Not the general
  pattern for the software — the documentation for *running it in a container*.
  FreeIPA's upstream states that privileged mode "is not supported and will not
  work", how install options must be passed, and which flags a cgroup v2 host needs.
  Every one of those was discovered by failure first and confirmed in the docs after.
  This is the Research step of the phase methodology, and skipping it is not faster.
- **Give stateful containers a stop grace period.** Docker's default is 10 seconds.
  A database that is `SIGKILL`ed mid-write comes back corrupt, and the service
  manager inside the container will often still report it healthy. Every `docker
  compose up` after a config change is a shutdown of that database.
- **Check the mode of the data directory, not just its path.** Containers run their
  services as non-root users that must traverse the bind-mounted directory to reach
  configuration relocated into it. `0750 root:root` silently breaks authentication
  while leaving the service "running". It cost a failed install in Phase 2 with
  FreeIPA and `kinit`, and again in Phase 3 with GitLab and PostgreSQL.
- **Declare the mode the application itself uses.** A role that enforces its own idea
  of a bind-mount's mode re-applies it on every run, so the second run takes down what
  the first one built — the change looks like a mysterious regression rather than the
  role and the application disagreeing. Find out what the service sets the directory to
  and declare that.
- **Gate on a transaction, not on a status command.** `ipactl status`, `systemctl
  is-active`, and a container in state `Up` all report the last known intent, not
  that the service works. Prove readiness with something a consumer actually does:
  authenticate, resolve a name, write a record. The phases are meant to run back to
  back, so a false ready in one phase surfaces as an unexplained failure in the next.
- **Mount the data volume before the service writes to it.** A service role that runs
  before the data disk is mounted quietly fills the OS disk instead.
- **The OS disk is for the OS. Nothing else, ever.** Every host here has a
  separate data volume for exactly this reason, and the rule is not a
  preference — it is what keeps a service's growth from taking down the machine
  that serves it. `repo01` ran three phases with `/var/lib/docker` on its 30 GB
  root disk, reaching 79% while its 98 GB `/data1` sat at 8%. When that disk
  fills, GitLab, the APT proxy, Apache, and the tunnel gateway stop *together*.
  Anything with a data root — container runtimes, databases, caches, logs —
  gets pointed at the data volume when it is installed, not when it runs out.
- **Kill the process, not the unit.** Stopping a service does not stop
  everything it started. RKE2's etcd survives `systemctl stop`, `rke2-killall.sh`,
  and a shim sweep, because it is outside the unit's cgroup once the container
  runtime is gone — and it keeps holding its ports, so the next start attaches to
  a stale datastore and blocks forever. Check that the process is gone, by name,
  before starting anything back up.
- **Match the storage to the write pattern, not to the throughput number.**
  A datastore that fsyncs on every commit is bound by fsync latency, and a disk
  can be fast by every other measure while being unusable for it. Measure the
  thing the service actually does — and measure one host at a time, because a
  synthetic fsync benchmark run everywhere at once is a denial of service
  against storage that is already at its limit.
- **Two faults in the same area are still two faults.** The hypervisor was
  swapping *and* its storage was too slow for etcd. The first was obvious,
  loud, and real, and fixing it changed nothing about the second. Confirm that
  the thing you repaired was the thing that was breaking you.
- **A default is a fact with an expiry date.** `ingress-nginx` was RKE2's
  packaged default, and reasoning from that was correct right up until the
  Kubernetes project retired it in March 2026. Phase 4 recorded the default
  accurately and drew a conclusion that was already stale; Phase 5 only caught
  it by reading the vendor's current documentation rather than trusting a note
  written one phase earlier. Check that a component is still alive, not just
  that it is still the default.
- **Some choices cannot be deferred, only made badly.** Rook Ceph wants a raw
  block device and Longhorn wants a filesystem, so "prepare the CSI disks and
  decide later" prepares them for neither. When two options want opposite
  things from the same resource, deferring is a decision to do the work twice.
- **Clear caches after publishing DNS records.** A name queried before it existed
  stays NXDOMAIN for the zone's negative TTL — an hour here — on every resolver that
  asked early. Verify against the authority with `dig @<server>` before believing a
  resolution failure.

## Secrets

No secret value is stored in this repository. Everything sensitive lives in
`~/.config/rke2lab/env.sh` outside the project tree, and automation reads it from the
environment. See `SECRETS.md` for the full inventory, the rotation procedure, and the
preflight check every playbook runs before it starts.

## The Sysadmin Guide

`docs/` is a Sphinx site written for whoever operates this environment rather
than for whoever built it: service URLs, credential rotation, adding a
GitOps-managed service, Longhorn and PVC capacity, and a symptom-ordered
troubleshooting page. It is the operator's view of what the documents in this
directory decide.

**It is maintained with the same rule the plan documents are: a change to the
environment is not finished until the guide reflects it.** The two go stale in
the same way and for the same reason — they describe the present — and the
guide goes stale faster, because it names URLs, versions and credentials rather
than decisions.

`docs/source/reference/maintaining-this-guide.rst` carries the trigger table:
which page a given kind of change obliges you to update. The one worth naming
here is the troubleshooting page, which only ever grows by someone adding the
thing that cost them an hour. Every fault this plan records under a "what the
run taught" heading belongs there in operator form.

It builds with no network access of its own, deliberately, because the person
reading it is often sitting at a broken environment:

```bash
python3 -m venv ~/.venvs/rke2lab-docs
~/.venvs/rke2lab-docs/bin/pip install -r docs/requirements.txt
make -C docs html
```

## Phase Methodology

Every phase in `PHASES.md` runs the same five-step flow. A phase is not complete until
all five steps are done.

1. **Review** — Re-read the plan for this phase and confirm the previous phase's exit
   criteria still hold. Verify the current state of the environment and the repo
   against what the plan assumes. Record anything that has drifted.
2. **Research** — Resolve the phase's open questions before writing code: version
   selection, artifact sources and checksums, provider or role behavior, and any
   decision the implementation depends on. Write down the decisions and their
   rationale.
3. **Implement** — Build the IaC and Ansible changes in the order the plan specifies.
   Stage every required artifact on `repo01` before the consuming host needs it.
4. **Test** — Run the phase's validation checklist, confirm idempotency on a second
   run, and confirm the exit criteria. Capture evidence.
5. **Document** — Correct every plan document the phase invalidated, including
   earlier phases' "Status" and "Still open" sections, and update the sysadmin
   guide in `docs/` for anything the phase changed about operating the
   environment: a new service, a new credential, a new URL, a new version, or a
   fault worth adding to the troubleshooting page. A phase that changed how the
   environment is run and did not touch `docs/` has skipped a step.
