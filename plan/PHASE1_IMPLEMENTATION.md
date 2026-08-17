# Phase 1 Implementation Plan — Repo Server and Artifact Distribution

## Scope

Build `repo01` as the bootstrap platform for every later phase:

- Controller tunnel and routing entry point into the internal network.
- SOCKS5 access path for external-to-internal web access.
- APT proxy/cache for Ubuntu packages.
- **Apache artifact host** serving every non-OS artifact to internal nodes.
- **Artifact staging area** holding the downloads for all six phases.
- Container host baseline for the GitLab deployment in Phase 3.

The internal network has no internet access. After Phase 1, `repo01` is the only path
by which any file reaches an internal host.

## Decision

| | |
| --- | --- |
| Primary IaC | Pulumi with Python |
| Fallback IaC | Terraform with the `bpg/proxmox` provider |
| Go/No-Go rule | If Pulumi cannot reliably create, update, and destroy `repo01` after two focused remediation attempts, switch to Terraform for VM lifecycle and keep Ansible unchanged |

## Preconditions

- Proxmox endpoint reachable from the automation controller.
- Ubuntu 24.04 cloud image or template prepared in Proxmox.
- SSH keypair available for cloud-init user access.
- Automation controller can install and run WireGuard tools.
- Local secrets prepared and **not committed**:
  - `PROXMOX_VE_ENDPOINT`
  - `PROXMOX_VE_USERNAME`
  - `PROXMOX_VE_PASSWORD`
  - `PROXMOX_VE_INSECURE`

## Repository Layout

```text
infra/
  pulumi/
    Pulumi.yaml
    Pulumi.dev.yaml
    __main__.py
    requirements.txt
    modules/
      provider.py
      vm_definitions.py
ansible/
  inventory/
    hosts.yml
    group_vars/
      repo/
        main.yml
  playbooks/
    repo01.yml
    tunnel_controller_access.yml
    validate_phase1.yml
  roles/
    base_host/
    wireguard_gateway/
    socks5_proxy/
    apt_proxy/
    data_volume/            # /data1 filesystem and mount
    artifact_host/          # Apache serving /data1/artifacts
    artifact_stage/         # download, verify, publish manifest artifacts
    gitlab/                 # guard/no-op in this phase
plan/
  PHASE1_IMPLEMENTATION.md
```

Staging is a separate role from the artifact host because the two answer to
different things: `artifact_host` is server configuration, `artifact_stage`
consumes the manifest in `inventory/group_vars/repo/artifacts.yml`. Adding an
artifact in a later phase is then an inventory change, not a role change.

---

## Review

Before writing code, confirm the starting state:

- Proxmox endpoint, credentials, and node capacity are available.
- The Ubuntu 24.04 template exists and boots with cloud-init.
- The external (`vmbr` for `192.168.1.0/24`) and internal (`192.168.2.0/24`) bridges
  exist and are attached to the right Proxmox interfaces.
- `TARGETS.md` and `infra/pulumi/modules/vm_definitions.py` agree on the `repo01` spec:
  4 vCPU, 10 GiB RAM, 32 GB OS disk, 100 GB `/data1`, NICs `192.168.1.20/24` and
  `192.168.2.99/24`.

  This line read `2 vCPU, 4 GiB` for four phases and was wrong for three of
  them. Phase 3 found `repo01` undersized for GitLab and resized it to 4 vCPU /
  8 GiB, and the whole estate has since taken a uniform 2 GiB uplift. **The
  sizing belongs here, not in Phase 3** — Phase 1 is the phase that creates
  this VM, so a rebuild must create it at its final size rather than build it
  small and grow it two phases later.
- No internal host currently has an internet default route.
- Record any drift found here before continuing.

Confirm `~/.config/rke2lab/env.sh` exists and exports `WIREGUARD_REPO_PRIVATE_KEY`
and `WIREGUARD_CONTROLLER_PRIVATE_KEY`. The playbooks refuse to start without them.
See `SECRETS.md`; the private keys were previously committed in plaintext and should
be rotated.

---

## Research

Resolve these before implementing. Record each decision and its rationale.

**Provider behavior**

- Pulumi ProxmoxVE dual-NIC ordering and cloud-init static network configuration.
- Disk attachment and mount handling for the 100 GB `/data1` volume.

**Artifact host**

- Apache document root layout under `/data1/artifacts`, directory indexing, and the
  access policy limiting reads to `192.168.2.0/24` and the tunnel.
- Checksum publication convention — one `.sha256` file alongside each artifact.
- Container image transport without a registry: `docker save` / `podman save` tarballs
  served over HTTP and loaded on the target host.

**APT proxy**

- apt-cacher-ng as a caching proxy only. It stores what internal hosts request and
  expires it again; it never mirrors an archive. Settings to confirm against real
  usage: cache location on `/data1`, the expiry window, retained package versions,
  and a CONNECT passthrough narrow enough that the proxy cannot serve as general
  internet egress.
- The client-side `Acquire::http::Proxy` setting delivered by the base host role.
  `repo01` itself does not use the proxy — it has the external NIC, and pointing it at
  its own proxy before that proxy exists breaks bootstrap.

**Split DNS**

- `repo01` needs upstream names to download artifacts and `dev.lo` names to run GitLab,
  while `core01` answers for `dev.lo` with no forwarders. Confirm the netplan drop-in
  written by `base_host` routes only `dev.lo` to `192.168.2.4`.

**Artifact manifest**

Build the manifest that drives the staging step. For each entry: name, version,
upstream URL, SHA256, destination path under the document root, and the phase that
consumes it.

| Phase | Artifacts to manifest |
| --- | --- |
| 2 | FreeIPA container image, Compose file inputs, container runtime packages |
| 3 | GitLab container image, GitLab Runner image if used, persistence tooling |
| 4–5 | RKE2 release tarballs, install script, and the full air-gap image set |
| 6 | Flux CD, Cilium, cert-manager, CSI, observability charts and images |

Phase 4–6 artifacts are downloaded and held on `repo01` now; they move into GitLab in
Phase 3 once Tier 2 exists.

---

## Implement

**1. Bootstrap the IaC environment**

- Create a Python virtual environment for the Pulumi project.
- Install the Pulumi CLI, `pulumi-proxmoxve`, and dependencies; pin versions in
  `requirements.txt`.

**2. Create the provider module**

- Define an explicit provider object with environment-driven credentials.
- Set safety options: TLS handling, and random VM IDs if the environment needs them.

**3. Implement the `repo01` VM module**

- Clone from the Ubuntu 24.04 template.
- Configure CPU, memory, and disks per `TARGETS.md`.
- Configure both NICs — external `192.168.1.20/24` via `192.168.1.1`, internal
  `192.168.2.99/24` with no gateway.
- Configure cloud-init: the login user from `deployment:vmUsername` — which is
  `root`, not the `devops` this line said for four phases — plus the SSH
  authorized key and static networking.

**4. Export outputs for configuration automation**

- Output hostname, IPv4 addresses, and the SSH target for Ansible.
- Feed those outputs into the inventory.

**5. Implement the controller tunnel path**

- Install and configure WireGuard on `repo01` and the controller.
- Create the point-to-point tunnel: `10.66.66.1/30` on `repo01`, `10.66.66.2/30` on the
  controller.
- Enable `net.ipv4.ip_forward=1` on `repo01`.
- Add the SNAT/masquerade rule for `10.66.66.0/30` toward the internal network.
- Add the controller static route for `192.168.2.0/24` via `wg0`.

**6. Apply the Ansible base setup**

- Host updates, baseline packages, timezone and NTP conventions, firewall posture.
- Confirm idempotency and check-mode behavior.

**7. Mount and lay out the artifact volume**

- Mount the 100 GB disk at `/data1`.
- Create `/data1/artifacts/{freeipa,gitlab,runtime,rke2,charts}` with the ownership and
  permissions Apache requires.

**7a. Keep the container daemons off the OS disk**

- `container_storage` — **new role**: set Docker's `data-root` and containerd's
  `root` to `/data1/docker` and `/data1/containerd` before either daemon is
  installed, and relocate them if they are already running from `/var/lib`.
- Runs immediately after `data_volume` and before anything that runs a
  container, for the same reason step 7 comes before step 8: a service that
  writes before its volume is mounted fills the OS disk instead.

This step was added in retrospect, and the cost of its absence is recorded in
the run record below. Both daemons default to `/var/lib`, which is the 32 GB OS
disk. By Phase 5 `repo01` was at **79% of its root filesystem** — 6.4 GB free —
while `/data1` sat at 8% with 87 GB free. Filling that disk stops GitLab, the
apt proxy, Apache, and the tunnel gateway simultaneously, because `repo01` runs
all of them.

The trap is that the space does not appear where it is looked for. With
Docker's containerd image store — `Storage Driver: overlayfs`, the default on
this host — the layer content lives under **containerd's** root, not Docker's.
`/var/lib/containerd` held 15 GB against `/var/lib/docker`'s few hundred
megabytes of real metadata. Moving only the obvious path reclaims almost
nothing and looks like the fix was applied.

`du` on a running daemon compounds it: `du -sh /var/lib/docker` reported 4.3 GB
because it followed the live overlay mount at `rootfs/` and counted containerd's
bytes a second time. Measure with the daemon stopped, or measure containerd.

**8. Implement the service roles**

- `socks5_proxy` — external-to-internal web access.
- `apt_proxy` — apt-cacher-ng bound to the internal and tunnel addresses only,
  configured through a drop-in so the packaged `acng.conf` and its mirror lists stay
  as shipped.
- `artifact_host` — **new role**: install Apache, template the vhost serving
  `/data1/artifacts` to `192.168.2.0/24` and the tunnel, enable directory indexes,
  disable CGI and any write methods, and manage the service.
- `gitlab` — remains a guard/no-op so no host-package install path can run in this
  phase.

**9. Stage the artifacts**

- Download every manifest entry to `repo01` over the external NIC.
- Verify each download against its recorded SHA256.
- Publish each artifact and its `.sha256` file under the document root.
- Save the required container images as tarballs and publish them the same way.
- Record the manifest in the repo so staging is reproducible, not manual.
- Classify every entry with `retention: bootstrap | transit`. Bootstrap
  artifacts are consumed from Apache by a host with no other source and stay
  here forever; transit artifacts exist only to be published into GitLab, and
  the local copy is removed once it lands. Presence is then checked **at the
  destination**, never on local disk — a role that deletes after publishing and
  looks at the disk to decide whether to re-fetch downloads the whole set on
  every run.

  The `retention:` axis was designed in Phase 6a, which is where the disk
  filled. It is documented here because the manifest and `artifact_stage` are
  Phase 1's, and a rebuilt `repo01` has to apply the model from its first run
  rather than acquire it five phases later. See `OVERVIEW.md` for the three
  rules and `PHASE6_IMPLEMENTATION.md` for the reasoning.

---

## Test

**IaC**

- Pulumi preview is clean and readable.
- Pulumi apply completes with no manual drift fixes.
- A second apply is a no-op, or shows only expected minimal updates.

**Configuration**

- The Ansible run completes successfully.
- A second run reports zero changes for steady-state tasks.

**Tunnel**

- The controller SSHes to at least one `192.168.2.0/24` host over the WireGuard path,
  with no jump host.
- Tunnel teardown and restore validated once.

**Artifact distribution**

- `apt update` and a package install succeed from an internal node through the proxy.
- The cache under `/data1` grows only by what was actually requested, and the daily
  expiry pass removes files no index references.
- The proxy is not reachable on the external address, and a CONNECT to a non-Ubuntu
  host through it is refused.
- `repo01` resolves an upstream name and a `dev.lo` name correctly at the same time.
- An internal node fetches an artifact from Apache over HTTP and the SHA256 matches
  the published checksum.
- A container image tarball downloads and loads successfully on an internal node.
- Apache refuses requests from outside `192.168.2.0/24` and the tunnel.
- **Negative test:** a direct upstream fetch from an internal node fails — no internal
  host has an internet path.

**Rebuild**

- Destroy `repo01` through Pulumi, recreate it, re-run Ansible, re-stage artifacts, and
  confirm every check above passes again.

**Operational**

- Log and credential locations documented.
- Recovery steps executed at least once.

---

## Run Record — 2026-08-13

Phase 1 re-run against the existing `repo01` after the plan changed. Pulumi was
not re-applied: `TARGETS.md` and `infra/pulumi/modules/vm_definitions.py` were
compared line by line and already agree on every host, so the drift was entirely
in the Ansible layer.

### Drift found during Review

| Finding | Resolution |
| --- | --- |
| `/dev/sdb` was raw — no filesystem, no `/data1` mount, nothing in fstab | New `data_volume` role. The apt cache and the artifact tree were both configured to live on `/data1` and would have been written to the 32 GB OS disk instead |
| No `artifact_host` role existed | Created |
| No artifact manifest and no staging path existed | Created `artifact_stage` plus `group_vars/repo/artifacts.yml` |
| `apt-cacher-ng` was still caching to `/var/cache` on the OS disk | The rewritten `apt_proxy` drop-in had never been applied; now on `/data1` |
| `validate_phase1.yml` covered two listeners and a ping | Rewritten to the Test section below, including the negative tests |
| `freeipa_server` fetched its image from `/containers/`, a path the document root layout does not have | Repointed at `/freeipa/`, and at the published `.sha256` for verification |

The 208 MB apt cache left behind at `/var/cache/apt-cacher-ng` is orphaned, not
in use. It is a caching proxy, so it refills on demand; deleting it is safe
whenever the OS disk needs the space.

### Defects found by the validation playbook

Three of these passed a check-mode dry run and an apply, and were only caught
because the validation playbook asserts behavior rather than configuration.

- **The apt proxy was listening on `0.0.0.0`, including the external address.**
  The config on disk was correct. An earlier run had written it and then failed
  in a later role, which discarded the queued restart handler; the next run saw
  no change and never restarted the service. The playbook now sets
  `force_handlers: true`, so a late failure cannot leave a service running
  config that no subsequent run will reapply.
- **Apache served the artifact tree to any source address.** The source-address
  rules were in a `<Directory>` block and the method restriction in a
  `<Location>` block. Apache merges authorization sections with `<Location>`
  last, so the `<Location>` — which contributed no `Require` for GET — replaced
  the address rules instead of adding to them. Both rules now live in the
  `<Directory>` block.
- **Write methods were accepted after that first fix.** Two `Require` sets that
  both apply to the same method merge as "satisfy any", so any client passing
  the address check also satisfied `Require all denied`. The address rules and
  the deny are now scoped to disjoint method sets via `<Limit>` and
  `<LimitExcept>`.
- **`ServerTokens Prod` had no effect.** The packaged `security.conf` sorts
  after a conf named `artifact-host.conf` in `conf-enabled` and sets
  `ServerTokens OS`. The role's conf is now `zzz-artifact-host.conf`.

### Decisions

- **Pulumi retained.** No provider problems arose; the fallback was not needed.
- **Access control by `Require ip`, not by binding.** Apache stays bound to all
  interfaces and refuses disallowed sources with 403. Binding to `10.66.66.1`
  would make Apache fail to start whenever it came up before `wg0`, which is
  the normal boot order. apt-cacher-ng does bind, because it has no client ACL
  of its own.
- **Image integrity is pinned at the registry digest, not at a tarball
  checksum.** `docker save` output is not reproducible across runtime versions,
  so no fixed tarball SHA256 could be recorded in advance. The manifest pins
  the digest, the pull is by digest, and the `.sha256` published next to each
  artifact is generated after staging and describes what is actually served —
  which is what consumers verify against.
- **A `.digest` marker sits next to each image tarball.** It records what the
  tarball was built from, so re-pinning the manifest triggers a re-export while
  an unchanged pin does not. Without it, every run would re-export gigabytes.
- **`name[casing]` is skipped in `.ansible-lint`.** It contradicts the
  file-name task prefix that `ANSIBLE_STANDARDS.md` requires of multi-file
  roles.

### Evidence

- `ansible-lint playbooks/ roles/` and `yamllint .` — clean; production profile.
- `repo01.yml --check` — passes, no failures.
- `repo01.yml` applied, then run twice more: `changed=0` both times.
- `validate_phase1.yml` — passes, including the negative tests. `core01` exists,
  so the no-internet-path check ran against a real internal node rather than
  skipping.
- Staged: `/data1` mounted, 98 GB, 1.6 GB used. FreeIPA (331 MB) and GitLab
  (1.4 GB) tarballs published with `.sha256` files that verify.
- Access control, measured: internal source GET/HEAD/OPTIONS 200,
  POST/PUT/DELETE/PATCH 403; external source 403 for both a file and the index.

### Not done

- **Phase 4–6 artifacts are not staged.** Their versions are still open
  research items in the later phase plans — `CLUSTER_COMPONENTS.md` has the CSI
  and secrets choices undecided — and an artifact cannot be downloaded or
  checksummed before its version is chosen. Phase 1 delivers the staging
  mechanism and the phase 2–3 artifacts that gate the next phase. This is Risk
  Gate 3, and it stays open until each later phase adds its manifest entries.
- **Rebuild not exercised.** Destroying and recreating `repo01` through Pulumi
  was not run, because `core01` already depends on this host. The re-apply and
  idempotency evidence above covers the configuration half of that test, not
  the VM lifecycle half.
- **Boot ordering not tested.** apt-cacher-ng binds to `10.66.66.1`, a `wg0`
  address. Whether it survives a reboot in which it starts before `wg0` is up
  has not been verified.
- **`/var/cache/apt` holds 2.2 GB** of downloaded `.deb` archives on `repo01`.
  Legitimate OS cache rather than a misplaced data root, so it is hygiene
  rather than a gate, but it is 2.2 GB of packages that will never be installed
  again. An `apt clean` belongs in `base_host`.

---

## Remediation — 2026-08-15: container storage moved off the OS disk

Found during the Phase 6 review and fixed here, in the phase that owns
`repo01`, rather than in the phase that happened to notice it.

The `container_storage` role was written and applied to `repo01` and `core01`.
It is idempotent — the second and third runs reported `changed=0` — and on a
fresh build it writes two configuration files before either daemon exists, so a
rebuilt host never acquires the fault at all.

| Host | Root before | Root after |
| --- | --- | --- |
| `repo01` | 79% used, 6.4 GB free | **30% used, 22 GB free** |
| `core01` | 13% used, 27 GB free | **9% used, 28 GB free** |

Two things went wrong during the run and both are worth keeping:

- **The graceful stop silently did nothing on `repo01`.** The role stopped
  compose projects with `--project-directory`, which only finds default
  filenames, and GitLab's project is `docker-compose.gitlab.yml`. Compose
  exited "no configuration file provided" — and a `failed_when` written to
  tolerate that error swallowed it. GitLab was therefore killed by the daemon's
  own 15 second shutdown timeout instead of its 5 minute `stop_grace_period`.
  PostgreSQL recovered cleanly (`healthy`, queries served, `gitlab:check`
  passing, zero corruption entries), but that was PostgreSQL's crash safety, not
  the automation's doing. The role now **discovers** the compose file and
  asserts it found one, and the tolerance is gone. A `failed_when` that hides
  the error it was written for is worse than no error handling.
- **`service_facts` does not report socket units**, so `docker.socket` was never
  stopped. A socket-activated daemon restarting partway through a multi-minute
  copy would have written into the root rsync was still filling. Unit detection
  now uses `systemctl list-unit-files`.

The old roots are renamed `.migrated` rather than deleted, and removed only by a
later run given `container_storage_remove_rollback=true` — a rollback deleted in
the run that created it is not a rollback.

## Risk Gates

| Gate | Condition | Action |
| --- | --- | --- |
| 1. Provider health | Repeatable provider errors block VM lifecycle | Decide the Terraform fallback quickly rather than grinding |
| 2. Network correctness | Dual-NIC routing or the WireGuard path is unstable | Stop and fix baseline networking before any service install |
| 3. Artifact completeness | The manifest is missing artifacts a later phase needs | Extend the manifest and re-stage before closing Phase 1 — a gap here blocks an air-gapped phase later |
| 4. Service footprint | `/data1` cannot hold the artifact set plus GitLab and its registry | Resize the volume before the Phase 3 rollout |
| 5. DNS dependency | `dev.lo` DNS is not yet active | Do not deploy GitLab; that is Phase 3, gated on Phase 2 |

## Deliverables at Phase 1 Completion

- Working Pulumi code for the `repo01` lifecycle.
- Working Ansible automation for all `repo01` services, including the new
  `artifact_host` role.
- Working controller-to-internal WireGuard configuration and validation runbook.
- Apache artifact host serving a staged, checksummed artifact set to internal nodes.
- A committed artifact manifest covering every phase.
- Validation playbook and smoke test evidence.
- Documented rollback and rebuild procedure.
- Handoff checklist for Phase 2, confirming the FreeIPA artifacts are staged.
- Decision record: Pulumi retained, or Terraform fallback activated.
