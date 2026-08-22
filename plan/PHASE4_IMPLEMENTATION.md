# Phase 4 Implementation Plan — RKE2 Control Plane

## Scope

Stand up a three-node RKE2 control plane on `kubecp01-03`, installed entirely
from artifacts that `repo01` staged and GitLab redistributes. No node in this
phase reaches the internet, and no node loads an image from a tarball copied
onto its disk — the images come from the GitLab container registry over TLS,
which is the whole point of building Tier 2 in Phase 3.

Phase 4 also closes the carry-over Phase 3 left open: publishing the RKE2
artifact set into GitLab. That work lands here because the versions had to be
chosen first, and choosing them is Phase 4's research.

## Decision

| | |
| --- | --- |
| Primary path | RKE2 `v1.35.7+rke2r1` servers, binary from the GitLab generic package registry, all images pulled from the GitLab container registry through a containerd mirror rewrite, Cilium CNI, kube-vip VIP in front of the API |
| Fallback path | If the registry mirror cannot be made to work from a node, fall back to the RKE2 airgap image tarball in `/var/lib/rancher/rke2/agent/images/` served over Tier 1 HTTP, and treat the registry path as a defect to fix before Phase 5 |

## Preconditions

- Phase 3 exit criteria hold: GitLab serves Git, the container registry, and the
  package registry over `dev.lo` names with FreeIPA-issued TLS.
- The `dev.lo` CA is published at `http://192.168.2.99/certs/dev.lo-ca.crt`.
- `RKE2_TOKEN` is set in `~/.config/rke2lab/env.sh`. New in this phase.
- `GITLAB_ROOT_PASSWORD` is set — Phase 4 authenticates to GitLab as `root` to
  push, and to mint the read-only credential the nodes use to pull.

---

## Review

Performed 2026-08-14 against the live environment.

| Check | Result |
| --- | --- |
| Tunnel to internal network | `repo01` and `core01` reachable |
| GitLab service | Container `Up 11 hours (healthy)` |
| `gitlab.dev.lo` TLS | 200 on `/users/sign_in` |
| `registry.gitlab.dev.lo` | 401 on `/v2/` — serving, unauthenticated |
| `dev.lo` authority | `gitlab` and `registry.gitlab` both `192.168.2.99` from `@192.168.2.4` |
| `/data1` on `repo01` | 91 GB free |
| `repo01` memory | 8 GiB, as resized in Phase 3 |
| Artifact tree | `rke2/` and `charts/` exist and are **empty** |
| Pulumi `kubecp01-03` specs | Present in `vm_definitions.py`, match `TARGETS.md` |
| Pulumi `phaseLimit` | **`2`** — Phase 4 VMs are not in the deployment set |

Four things had drifted or were never settled, and all four are resolved here.

### The RKE2 artifact set is not staged

Expected: `PHASE3_IMPLEMENTATION.md` carried step 5 forward deliberately, and
`group_vars/repo/artifacts.yml` says why. Phase 4 adds the manifest entries and
re-runs `playbooks/repo01.yml`. This is the plan working as designed, not drift.

### `phaseLimit` is 2, not 3

Phase 3 added no VMs, so nobody had to raise it. Phase 4 does, and it goes
straight to `4`.

### The nodes have no DNS records

`TARGETS.md` gives `kubecp01-03` addresses but nothing publishes them in
`dev.lo`, and no name exists for the API endpoint. Phase 4 adds forward records
for the three nodes and for the VIP, through the existing
`freeipa_server_dns_records` inventory list.

### `CLUSTER_COMPONENTS.md` names the wrong RKE2 default ingress

It records "Ingress: Traefik v3 (RKE2 default)". RKE2's packaged default is
`rke2-ingress-nginx`; Traefik is the K3s default. RKE2 does ship a Traefik
image set as a separate airgap tarball, so choosing Traefik remains possible —
it is just not the default and not free. Phase 4 does not resolve this, because
Phase 4 does not deploy ingress. It is flagged here so Phase 5 decides it
knowingly rather than discovering it while a chart fails to appear.

---

## Research

### Version selection

| Question | Decision | Why |
| --- | --- | --- |
| RKE2 version | `v1.35.7+rke2r1` | The `stable` channel on `update.rke2.io` as of 2026-08-14. `latest` is `v1.36.3+rke2r1`; the stable channel exists precisely so a lab does not track the newest minor into its own bug reports |
| Kubernetes / etcd | `v1.35.7`, etcd `v3.6.14-k3s1` | Whatever the release ships. Not independently selectable, and pinning the RKE2 patch pins both |
| CNI | **Cilium**, RKE2's packaged chart (`cni: cilium`) | `CLUSTER_COMPONENTS.md` selects Cilium. A control plane with no CNI never reports `Ready`, so this cannot wait for Phase 5 as `PHASES.md` implies — see the note below |
| Cilium version | `v1.19.6` | Whatever `rke2-images-cilium` for this release contains. Taking RKE2's bundled chart means taking its tested pairing |
| kube-vip | `v1.2.2` | `v1.2.3` was published four days ago. One patch back on the same minor is the same feature set with two more weeks of exposure |

**Cilium moves from Phase 5 to Phase 4.** `PHASES.md` lists the Cilium install
under Phase 5's node-managed core services, but Phase 4's own test is
`kubectl get nodes` healthy, and a node without a CNI stays `NotReady` forever.
The two cannot both be true. Phase 4 therefore brings up Cilium as RKE2's
packaged chart, and Phase 5 inherits a working CNI rather than installing one.

### Air-gapped install method

RKE2 documents two air-gapped paths: image tarballs copied onto each node, or a
private registry. `OVERVIEW.md` mandates the second — "All RKE2 container
images — pushed to the GitLab container registry and pulled from there by
control plane and worker nodes."

The binary still has to arrive somehow, and it comes from the generic package
registry, per the same table. So each node does:

```bash
INSTALL_RKE2_ARTIFACT_PATH=/data1/rke2-artifacts INSTALL_RKE2_TYPE=server sh install.sh
```

with only two files in that directory: `rke2.linux-amd64.tar.gz` and
`sha256sum-amd64.txt`. Verified by reading `install.sh` for this tag:
`stage_local_airgap_tarball` only sets `AIRGAP_CHECKSUM_EXPECTED` when an
`rke2-images.linux-amd64.tar.*` is present in the artifact path, and both
`verify_airgap_tarball` and `install_airgap_tarball` return immediately when it
is empty. Omitting the image tarball is a supported shape, not a hack — the
script installs the binary and the systemd units and imports nothing.

This is deliberate. Seeding the airgap tarball *as well* would look like belt
and braces and is actually the opposite: containerd would find every image
already present locally and never contact the registry, so a broken mirror
would pass every test in this phase and fail in Phase 5 or 6 with no obvious
cause. The registry path is the only path, so it is the path that gets tested.

`install.sh` is pinned to the release tag
(`raw.githubusercontent.com/rancher/rke2/v1.35.7+rke2r1/install.sh`) rather than
fetched from `get.rke2.io`. The manifest's integrity control is a recorded
SHA256, and `get.rke2.io` serves a moving target that no checksum can describe.

### What repo01 stages, and what it does with it

Two different things are being staged, and conflating them is how the image
tarballs end up on a node that was supposed to pull from a registry.

| Artifact | Staged on `repo01` | Served to nodes | Purpose |
| --- | --- | --- | --- |
| `rke2-images-core.linux-amd64.tar.zst` | Yes | **No** | Loaded on `repo01`, retagged, pushed to the GitLab registry |
| `rke2-images-cilium.linux-amd64.tar.zst` | Yes | **No** | Same |
| `rke2-images-{core,cilium}.linux-amd64.txt` | Yes | No | The authoritative list of what to retag and push |
| `rke2.linux-amd64.tar.gz` | Yes | Via GitLab packages | The binary |
| `sha256sum-amd64.txt` | Yes | Via GitLab packages | `install.sh` verifies the binary against it |
| `install.sh` | Yes | Via GitLab packages | The installer |
| `ghcr.io/kube-vip/kube-vip:v1.2.2` | Yes | Via GitLab registry | The VIP |

Pulling 26 images individually would work, but one 629 MB tarball plus one
589 MB tarball is fewer round trips and, more usefully, the checksums come from
Rancher's own `sha256sum-amd64.txt` rather than from digests this repo resolved
itself.

### GitLab layout and the mirror rewrite

Group `rke2`, projects `images` and `packages`. Container images land at
`registry.gitlab.dev.lo/rke2/images/<upstream path>`, preserving the upstream
path under the project so the rewrite rule is a prefix and nothing else:

```yaml
# /etc/rancher/rke2/registries.yaml
mirrors:
  docker.io:
    endpoint:
      - "https://registry.gitlab.dev.lo"
    rewrite:
      "^rancher/(.*)": "rke2/images/rancher/$1"
  ghcr.io:
    endpoint:
      - "https://registry.gitlab.dev.lo"
    rewrite:
      "^kube-vip/(.*)": "rke2/images/kube-vip/$1"
configs:
  "registry.gitlab.dev.lo":
    auth:
      username: "<deploy token username>"
      password: "<deploy token>"
    tls:
      ca_file: /etc/rancher/rke2/dev.lo-ca.crt
```

Images keep their upstream names locally — `crictl image ls` shows
`docker.io/rancher/...` — so nothing downstream has to know the mirror exists.
That matters for the packaged Helm charts, which reference upstream names and
which this phase does not want to patch.

`disable-default-registry-endpoint: true` is set in `config.yaml`. Without it,
containerd falls back to the real `docker.io` when the mirror misses. On this
network that fallback cannot succeed anyway, but it turns a precise
"mirror does not have this image" into a DNS or connect timeout thirty seconds
later, and it hides exactly the failure this phase needs to see.

| Question | Decision | Why |
| --- | --- | --- |
| Push credential | `root` and `GITLAB_ROOT_PASSWORD` | Already in `env.sh`, admin rights needed to create the group and projects anyway. Push happens only from `repo01` |
| Pull credential | Group deploy token `rke2-nodes`, `read_registry` **and** `read_package_registry` | Nodes must not hold admin credentials. Group-scoped so one token covers both projects and every later phase. Both scopes, because they are separate permissions and a node needs both — see the runbook |
| Minting the token | The admin token comes from the shared `gitlab_admin_token` role, which signs in as root and POSTs the web token form in a second or two (the `gitlab-rails` runner is kept only as a fallback for a controller without a root password); the node pull token is a group deploy token the role creates through the stable REST API | The deploy-token API is stable across GitLab versions. The admin token is minted at most once a day and recorded at `0600`, and every later run proves the recorded value still authenticates before reusing it — see `plan/SECRETS.md` |
| Token persistence | `/data1/gitlab/rke2-deploy-token.yml`, `0600` | The API returns a token value once and never again, so a second run cannot re-read it. Overridden by `GITLAB_REGISTRY_USER` / `GITLAB_REGISTRY_TOKEN` when those are set in `env.sh` |
| Package version string | `1.35.7+rke2r1` | GitLab validates generic package versions as semver; `+rke2r1` is valid build metadata, the leading `v` is not. Confirmed by upload before the role was written |

### Control plane HA

RKE2 already solves the half that matters. Every node runs a client-side load
balancer inside the `rke2 agent` process: the `server:` address only seeds it,
and once the node has joined it syncs the full apiserver endpoint list from the
`kubernetes` service in `default` and holds connections to all three servers.
Node-to-apiserver traffic survives a server outage with no VIP at all, and that
covers the Phase 5 agents too.

What is left uncovered is the initial registration address for a node that is
joining, and `kubectl` from the controller. Cilium does not help here: LB-IPAM
and L2 announcements program `type: LoadBalancer` Services, which requires a
working apiserver to program them — it cannot front the thing it depends on.

| Question | Decision | Why |
| --- | --- | --- |
| Fixed registration address | **kube-vip**, VIP `192.168.2.20`, ARP mode, leader-elected | Closes the join and `kubectl` paths properly. Round-robin DNS was the cheaper option and would have left a join or a `kubectl` call landing on a dead node's address |
| Deployment shape | DaemonSet manifest in `<data-dir>/server/manifests/` | RKE2 auto-applies that directory. The VIP therefore exists as soon as `kubecp01`'s apiserver does, which is before `kubecp02` needs it |
| Bootstrap order | `kubecp01` omits `server:` entirely | RKE2's first server needs no `cluster-init` flag; the absence of `server:` is what makes it the initial member. `kubecp02-03` then join `https://192.168.2.20:9345` |
| Play serialisation | `serial: [1, 2]` (default), overridable with `-e kubecp_serial=1` or `=N` | Two batches. Batch 1 is the bootstrap server, alone: it *starts* the cluster, so nothing else may be told to join it until /readyz, node-Ready, and the VIP are all up. Batch 2 is the remaining servers, in parallel: a joiner has none of the bootstrap's constraint — two new etcd members joining a healthy one-member cluster is ordinary, and the quorum argument only bites when *restarting* members of an already-formed, even-sized etcd, not when fresh members register against a live VIP. Running the joiners' host build and image pulls in parallel removes the largest wall-clock cost of a cold Phase 4 (three node-Ready waits that ran back-to-back under a blanket `serial: 1`). `-e kubecp_serial=1` restores fully serial; `-e kubecp_serial=N` (N at least the group size) runs the whole group at once, the only way to recover an even-sized etcd whose quorum can never be reached one member at a time. See playbooks/kubecp.yml |
| API name | `kube.dev.lo` → `192.168.2.20` | One name for the cluster, published in the domain, pointing at the VIP |
| `tls-san` | VIP, `kube.dev.lo`, all three node names and addresses | A certificate that already covers every path the API is reachable by is one restart nobody has to schedule later |

### Storage, etcd, and node layout

| Question | Decision | Why |
| --- | --- | --- |
| RKE2 data directory | `data-dir: /data1/rancher/rke2` | `TARGETS.md` gives each node a 32 GB OS disk and a 100 GB `/data1`. containerd's image store lives under the data directory and grows with every image the cluster pulls; on the OS disk it would eventually take the node down. Same lesson as `/data1/gitlab` in Phase 3 |
| etcd snapshots | Every 6 hours, retain 10, at the default location | RKE2 defaults to 12 hours and 5. Six hours costs nothing on a 100 GB disk and bounds the loss window to something a lab rebuild can tolerate. The snapshot directory is left at its default, which sits under the data directory and is therefore already on `/data1` — overriding it would be a second setting saying the same thing, and one more place for the two to disagree |
| etcd sizing | Default 2 GB quota | Three nodes, no workloads until Phase 6. The default is roughly two orders of magnitude above what this cluster will hold |
| Server taints | **None** in Phase 4 | There are no workers yet, so tainting the servers would leave nothing schedulable and nothing to test against. Phase 5 decides whether to taint once workers exist |
| Ingress | `disable: rke2-ingress-nginx` | Phase 5 owns ingress, and it belongs on the workers. Leaving it enabled would schedule it onto control plane nodes and then have to be moved |

### The lab's storage is below etcd's floor, and it is structural

The single largest finding of this phase, discovered by the control plane
falling over rather than by planning. Measured on an idle cluster node with
`dd if=/dev/zero of=/data1/.ft bs=4k count=500 oflag=dsync`:

| Measure | `cache=none` | `cache=writeback` | After the TrueNAS memory fix | etcd needs |
| --- | --- | --- | --- | --- |
| 4 KB fsync latency | ~26 ms | ~20 ms | ~31 ms | under 10 ms |
| Sequential fsync rate | ~38/s | ~50/s | ~32/s | 50/s floor, 500/s busy |
| Sequential throughput | 63 MB/s | 63 MB/s | not re-measured | not the constraint |

etcd's write path is one serialised fsync per raft commit, so it is bound by
fsync **latency**, not bandwidth. 63 MB/s is ample and irrelevant.

**The memory ballooning was a separate problem, and fixing it did not help.**
The Proxmox VM was configured with 64 GB but TrueNAS held its *minimum* at
8 GB, so the hypervisor ballooned it down and reported maxed-out memory and
swap while the guests together were nowhere near 64 GB. Correcting that fixed
what it was: the host now runs at 14-29% of 67 GB with **zero swap**. Fsync
latency did not improve, which is the useful part of the result — it separates
the two problems and confirms the storage constraint is the nested write path
rather than memory pressure.

The 31 ms figure was taken on a host that had been up for well under an hour,
so ARC would have been cold and post-boot work still settling. Treat it as
"no improvement", not as a regression, and re-measure on a host that has been
stable for a while before drawing any further conclusion.

**Do not benchmark all three nodes in parallel.** Doing so issues 1500
synchronous writes at a layer that sustains roughly 40 per second, and the
whole Proxmox VM became unresponsive during exactly that window, needing a
restart from TrueNAS. It could not be pinned on the benchmark — all three
guests stopped logging within 12 seconds of each other with no I/O errors, no
hung tasks and no filesystem errors, which is what a hypervisor freezing looks
like from inside and is indistinguishable from the hypervisor being halted. But
saturating a storage layer that is already at its limit is not a safe thing to
do on a live cluster. One node, one run, and only when the cluster can afford
it.

The failure chain, in order, because each link looked like a different problem:
a slow fsync misses etcd's 500 ms heartbeat → the leader is declared overloaded
and applies start taking seconds → `rke2-server` fails its own etcd read and
exits → systemd restarts it, but the previous containerd is gone while its
`etcd` process survives as an orphan still holding ports 2379 and 2380 → the
restarted server connects to that orphan, which has stale membership and no
quorum → every read blocks → the API stops answering → kube-vip loses its
leader lease and withdraws the VIP → the third server cannot join, because the
address it registers against is the VIP. The reported symptom was "kubecp03
will not join". Nothing in that sentence is where the fault was.

**`rke2-killall.sh` does not clear this state.** It enumerates what to kill
through containerd's socket, and by then the socket is gone, so the orphaned
`etcd` and `containerd-shim` processes survive it. They must be killed by name.
Until they are, a restarted server keeps finding a dead etcd on the port where
it expects a live one.

**Why the disk is slow is not fixable from inside Proxmox.** The Proxmox host's
only disk is `/dev/vda` — a virtio device. Proxmox is itself a virtual machine,
which `PROXMOX.md` states in its first line, so every guest fsync crosses two
hypervisors before it reaches real hardware. The fact was recorded; its
consequence for etcd was not, and that consequence is now noted there too.

`cache=writeback` was applied and helped less than expected, which is worth
recording precisely because the reasoning was wrong the first time: writeback
lets the host page cache acknowledge *writes*, but an explicit flush is still
propagated to the backing device, and etcd flushes on every commit. Only
`cache=unsafe` discards flushes, and that trades a rebuildable lab for a
corrupt one after any host crash — not worth it for the remaining 10 ms.

What actually holds the cluster together is the etcd tolerance change:
`heartbeat-interval=1000` and `election-timeout=5000`, up from 500 and 2500.
That does not make the storage faster. It stops etcd interpreting a slow commit
as a dead leader, which is the difference between a slow cluster and one that
loses quorum. Phases 5 and 6 add three workers and an observability stack to
this same storage, and that is where this will be tested properly.

### The controller has no scripted dependency install

`OVERVIEW.md` requires that "every controller dependency must be documented and
scripted so the automation environment can be rebuilt from scratch without
tribal knowledge", and there is no such script — Pulumi, Ansible, and their
virtual environments were installed by hand and only the Pulumi half is written
down. Phase 4 hit this because `kubectl` is one of its own exit criteria and was
not present.

`kubectl` was taken from `kubecp01`'s own `<data-dir>/bin/kubectl` into
`~/.local/bin`, which guarantees it matches the cluster version exactly and
needs no download. That is the right source; the missing part is that nothing
in this repository records it.

This is not fixed here, because inventing a controller bootstrap mechanism is a
larger change than Phase 4 should carry, and it would want to cover the Python
environments and the tunnel too. It is written down as a debt rather than left
as tribal knowledge, which is the thing `OVERVIEW.md` was actually guarding
against.

**Update — Phase 5 paid the `kubectl` half.** `playbooks/controller.yml` now
copies `kubectl` from a cluster node by exactly the reasoning above, installs
`k9s`, and configures the shell so `KUBECONFIG` is set. The Python environments
and the tunnel are still by hand. See the operator access section of
`PHASE5_IMPLEMENTATION.md`.

### Still open

- **Node enrollment in FreeIPA.** The cluster nodes are not domain-enrolled, for
  the same reason `repo01` is not: `ipa-client-install` rewrites
  `/etc/resolv.conf`. Unlike `repo01`, these nodes *do* resolve solely through
  `core01`, so enrollment would be far less disruptive here. Deferred because
  nothing in Phases 4–6 needs it; SSO in Phase 6 may change that.
- **Certificate rotation.** RKE2 rotates its own internal PKI on restart within
  90 days of expiry. The `dev.lo` CA trust on each node is a static file and has
  no renewal path, which is the same position Phase 3 left `repo01` in.

---

## Implement

**1. Add the Phase 4 artifacts to the manifest** — `group_vars/repo/artifacts.yml`
gains seven `file` entries and one `image` entry, then `playbooks/repo01.yml`
re-runs to stage them under `rke2/`.

**2. Publish the DNS records** — `freeipa_server_dns_records` in
`group_vars/core/main.yml` gains `kubecp01-03` and `kube`, applied by
re-running `playbooks/core01.yml`.

**3. Push the artifact set into GitLab** — a new `rke2_publish` role on `repo01`:

- Ensure the `rke2` group and the `images` and `packages` projects exist.
- Ensure the `rke2-nodes` group deploy token exists, and record its value.
- Load both image tarballs, retag every entry in the published `.txt` lists to
  the GitLab path, push, and record what was pushed so a second run is a no-op.
- Upload `install.sh`, `rke2.linux-amd64.tar.gz`, and `sha256sum-amd64.txt` to
  the generic package registry.

**4. Raise the Pulumi phase limit to 4 and create `kubecp01-03`.**

**5. Prepare the nodes** — `base_host`, `data_volume`, and `time_sync`, then a
new `rke2_node` role for everything that must be true before RKE2 starts:

- Install the `dev.lo` CA into the system trust store *and* at the path
  `registries.yaml` names. Before anything pulls, per the Phase 3 runbook.
- Render `registries.yaml` with the mirror, the rewrite, and the deploy token.
- Fetch the installer and binary from the GitLab package registry.
- Run `install.sh` against them.

**6. Form the cluster** — a new `rke2_server` role:

- Render `config.yaml`, with `server:` present on every node but the first.
- Render the kube-vip DaemonSet and RBAC into `server/manifests/` on the first
  server only.
- Start `rke2-server` on `kubecp01`, gate on readiness, then start `kubecp02`
  and `kubecp03` in series.
- Publish a kubeconfig to the controller pointing at `https://kube.dev.lo:6443`.

**7. Gate on a transaction** — three nodes `Ready`, three healthy etcd members
with no learner, Cilium and CoreDNS rolled out, and a pod that pulls its image
through the mirror and runs.

---

## Test

**Cluster**

- All three servers join; `kubectl get nodes` shows three `Ready`.
- `etcdctl endpoint health` reports three healthy members, none a learner.
- The VIP answers on `192.168.2.20:6443` and `kube.dev.lo` resolves to it.
- `kubectl` works from the controller over the tunnel with no jump host.

**Air gap**

- Every image in `crictl image ls` was pulled from `registry.gitlab.dev.lo` —
  confirmed in containerd's logs, not inferred.
- No node made an outbound internet request during install.
- A direct upstream fetch from a node fails.

**Operational**

- A second Ansible run reports no changes.
- An etcd snapshot and restore is exercised once.
- The cluster survives a reboot of all three nodes.
- A leader failover moves the VIP.

**Verified on 2026-08-14**

| Check | Evidence |
| --- | --- |
| Three servers joined | `kubecp01-03` all `Ready`, `v1.35.7+rke2r1` |
| etcd | One `Running` member per server, no learner left behind |
| API through the VIP | `kube.dev.lo` → `192.168.2.20`, both `6443` and `9345` answer |
| VIP held exactly once | Only one node carries the address; leader election is doing its job |
| `kubectl` from the controller | `get nodes` over the tunnel against `https://kube.dev.lo:6443`, certificate validates against the FreeIPA CA |
| No tarball on any node | `<data-dir>/agent/images` empty on all three — the registry is the only path |
| **Pull through the mirror** | `crictl pull docker.io/rancher/mirrored-pause:3.10.2` succeeds on every node, served by GitLab via the containerd rewrite |
| **No route upstream** | A direct fetch of `registry-1.docker.io` fails from every node |
| Idempotency | Full `playbooks/kubecp.yml`: `changed=0` on all five hosts, `repo01` included |
| etcd snapshot | `rke2 etcd-snapshot save` wrote a snapshot to the data volume |
| **etcd restore** | Restored from snapshot; a namespace created *after* the snapshot was gone afterwards, and all three nodes returned to `Ready` |
| Reboot survival | Three separate times, all unattended — see below |
| Lint | `yamllint` clean, `ansible-lint` clean at the `production` profile |

Reboot survival was verified three times, none of them deliberately, and each
was harder than the test the plan asked for:

1. All three VMs stopped and started to apply `cache=writeback`. Harder than a
   reboot, because it replaces the qemu process rather than restarting the
   guest.
2. A full Proxmox host reboot after the TrueNAS memory fix.
3. A second full host outage, hard-stopped rather than shut down cleanly.

The cluster returned to three `Ready` nodes on its own every time, including
`kubecp03`, whose `rke2-server` had been stopped by hand and came back purely
because the unit was left enabled. Validation was re-run after the last outage
and passed on all four hosts with no failures.

The one test not run is a deliberate kube-vip leader failover. The VIP moved
several times during the storage incident and always landed on exactly one
node, which is the same evidence obtained less tidily.

---

## Risk Gates

| Gate | Condition | Action |
| --- | --- | --- |
| 1. Registry push | `repo01` cannot push the image set into GitLab | Stop. Everything below depends on it, and the fallback tarball path would hide the failure rather than fix it |
| 2. Mirror pull | `kubecp01` cannot pull through the mirror | Stop and fix the mirror. Do not seed the airgap tarball to get past it — see the Research note on why that is worse than failing |
| 3. VIP | kube-vip does not take the address, or ARP does not propagate on `vmbr1` | Fall back to `kube.dev.lo` as three A records for the node addresses, which RKE2 supports directly, and carry the VIP as a defect |
| 4. First server | `rke2-server` on `kubecp01` does not reach readiness | Read `journalctl -u rke2-server` and the containerd log before touching config. Do not join `kubecp02` against a control plane that is not ready |
| 5. Disk | `/data1` is not mounted before RKE2 starts | Stop. The data directory would land on the 32 GB OS disk and the node would fill silently, exactly as Phase 1 warned |

---

## Runbook

### Things that cost a failed run here

Recorded because the next phase meets the same shapes, and because each of them
presented as a different problem than it was.

**Deriving the bootstrap server from play order silently builds a second
cluster.** `ansible_play_hosts_all[0]` is `kubecp01` when the play runs against
the whole group and `kubecp02` under `--limit kubecp02:kubecp03`. The host that
comes out of that expression is the one given no `server:` address, which is
precisely what makes a server start its own cluster rather than join one. So a
routine limited re-run would have produced two one-node clusters, both healthy,
both wrong, with etcd on each perfectly happy. Caught before RKE2 started on
the node; the fix is that `kubecp_bootstrap_host` is stated in inventory and
asserted, because which host started the cluster is a fact about the
environment and not a property of how the playbook was invoked.

**A deploy token that can pull images cannot necessarily pull packages.**
`read_registry` and `read_package_registry` are separate scopes. A token with
only the first authenticates fine against `jwt/auth` for the container
registry — so every registry check passes — and then gets a bare request
failure on the generic package download, which is where the RKE2 binary comes
from. The node fails at `install.sh` having already proven it can reach the
registry, which points the investigation at exactly the wrong place. The role
now checks an existing token's scopes rather than only its name, so widening
the scope list replaces the token instead of leaving one that half works.

**Ansible's `form-urlencoded` body renders a list as repeated keys.**
`scopes: [read_registry, read_package_registry]` becomes
`scopes=read_registry&scopes=read_package_registry`, and GitLab's API wants
`scopes[]=`. It does not reject the request — it returns `201` with a token
carrying one scope. The symptom is a brand new token that 401s against the
registry it was just created for. `body_format: json` sends a real array.

**`docker load` drops the `docker.io/` prefix and keeps every other one.**
`docker.io/rancher/rke2-runtime:v1.35.7-rke2r1` is called
`rancher/rke2-runtime:v1.35.7-rke2r1` once loaded, while
`ghcr.io/kube-vip/kube-vip:v1.2.2` keeps its host. A literal comparison against
the release's own image list therefore reports every core image as missing from
a tarball that contains all of them. Note that this is *not* the same
transformation as stripping the host to build the mirror path, so the role does
both, separately.

**RKE2 registers a node under its fully qualified name.** The node is
`kubecp01.dev.lo`, not `kubecp01`. A readiness gate that looks the node up by
its inventory name finds nothing, and then spends its full retry budget waiting
for a node that was `Ready` the whole time. Read the name from the host.

**A folded YAML scalar puts spaces inside a URL.** `>-` turns every newline
into a space, so wrapping a long `url:` across lines silently corrupts the
query string. Wrapping *inside* a `{{ ... }}` is safe, because the space lands
in a Jinja expression; wrapping outside it is not. Long URLs get assembled in a
task-scoped variable.

### Restoring etcd from a snapshot

Exercised once, and it rolls the whole cluster back — this is not a per-node
operation. Verified by creating a namespace *after* taking the snapshot and
confirming it was gone afterwards, because a restore that leaves recent state
in place has not restored anything.

```bash
# 1. On any server: take the snapshot (or use a scheduled one).
rke2 etcd-snapshot save --name <name>

# 2. Stop every server, and make sure etcd is actually dead on each — see below.
systemctl stop rke2-server
pkill -9 -x etcd

# 3. On the node holding the snapshot only:
rke2 server --cluster-reset \
  --cluster-reset-restore-path=<data-dir>/server/db/snapshots/<snapshot>
# It exits deliberately, telling you to restart without the flag.

# 4. On every other server, move the datastore aside. They rejoin empty.
mv <data-dir>/server/db <data-dir>/server/db.pre-restore-$(date +%s)

# 5. Start the restored server first, wait for it to answer /readyz, then run
#    playbooks/kubecp.yml against the others to rejoin them.
```

The restored server keeps its certificates; `--cluster-reset` backs the old ones
up to `<data-dir>/server/tls-<epoch>` before reissuing.

**Orphaned etcd survives every gentle stop.** `systemctl stop rke2-server`,
`rke2-killall.sh`, and even `pkill -9 -f containerd-shim` all leave the `etcd`
process running, because it is not in the unit's cgroup once containerd is gone.
It keeps holding ports 2379 and 2380, so the next server to start connects to a
stale datastore and blocks on every read. `pkill -9 -x etcd` is what actually
clears it, and it is worth checking `pgrep -x etcd` returns nothing before
starting anything back up. This one cost the most time in this phase, twice.

### Where to look when a node does not come up

```bash
journalctl -u rke2-server -n 200 --no-pager
/data1/rancher/rke2/bin/kubectl --kubeconfig /etc/rancher/rke2/rke2.yaml \
  get pods -A --field-selector=status.phase!=Running
crictl --runtime-endpoint unix:///run/k3s/containerd/containerd.sock images
```

A node that registers and stays `NotReady` is almost always the CNI: check
whether the `cilium` pods could pull. A node that never registers at all did
not get that far — read the journal, not the cluster.

`cilium-operator` sitting `Pending` on a one-node cluster is expected and not a
fault: it wants two replicas on separate nodes and there is only one until the
second server joins.

### Reaching the cluster

```bash
export KUBECONFIG=~/.kube/dev-lo.config     # written by the phase 4 playbook
kubectl get nodes
```

The kubeconfig points at `https://kube.dev.lo:6443`, which resolves to the VIP
and is covered by the API certificate because `tls-san` lists it.

## Deliverables at Phase 4 Completion

- A three-node RKE2 control plane on `v1.35.7+rke2r1`, installed entirely from
  GitLab-hosted artifacts, with no internet access from any node.
- The RKE2 image and package set published in GitLab, closing Phase 3's
  carry-over.
- Cilium CNI running, ahead of Phase 5's schedule and for a stated reason.
- A VIP-fronted API reachable as `kube.dev.lo` from the controller and from
  inside the cluster.
- etcd snapshots on `/data1`, with a restore exercised once.

## Status

Delivered and verified, with one environmental constraint carried forward.

**Complete:** a three-node RKE2 control plane on `v1.35.7+rke2r1`, installed
entirely from GitLab-hosted artifacts with no internet access from any node.
Cilium is the CNI, the API is fronted by a kube-vip VIP reachable as
`kube.dev.lo` from inside the cluster and from the controller, etcd takes
scheduled snapshots to the data volume, and a restore has been exercised and
proven. The roles are idempotent and the whole set lints clean.

Phase 3's carry-over is closed: the RKE2 image and package set is published in
GitLab and demonstrably pulled from there by nodes that cannot reach the
internet. That satisfies the second Phase 3 exit criterion in content as well
as in mechanism.

**Carried into Phase 5 — storage.** The lab's fsync latency is at etcd's
documented floor and the cause is structural: Proxmox is itself a virtual
machine. The control plane is stable now because etcd's heartbeat and election
timeouts were raised to tolerate it, not because the storage improved.

Two things were ruled out along the way, and both are worth not re-testing.
`cache=writeback` helps only marginally, because explicit flushes are still
passed through. And the TrueNAS memory ballooning — the Proxmox VM's minimum
allocation left at 8 GB against a 64 GB maximum — was a genuine and separate
fault, now fixed, that turned out to have no bearing on fsync latency at all.

Phase 5 adds three workers and Phase 6 an observability stack to the same
disks, which is where this will be tested properly. If anything in a later
phase presents as random control plane instability, read the etcd section of
this document before looking anywhere else.

**Also open:**

Four of the six items below were closed by Phase 5 and are kept, marked, rather
than deleted — what a phase handed forward is part of its record, and a reader
arriving here from `PHASES.md` should not have to discover elsewhere that the
question was answered.

- **A settled storage measurement.** *Still open.* Every fsync figure recorded
  here was taken on a host that had recently rebooted or was under change. The
  numbers agree well enough to act on, but one clean reading from a host that
  has been stable for some hours is still worth having. Phase 5 did not obtain
  it either, and needs it less than Phase 6 will.
- **Ingress.** **Closed in Phase 5 — and the conclusion inverted.** The
  observation below was accurate: RKE2's packaged default was `ingress-nginx`
  and `CLUSTER_COMPONENTS.md` was wrong to call Traefik the default. What it
  missed is that the Kubernetes project retired `ingress-nginx` in March 2026
  and RKE2 makes Traefik the default at v1.36. Phase 5 deploys **Traefik**, so
  the document was right and its stated reason was wrong.
- **Server taints.** **Closed in Phase 5.** The servers now carry
  `CriticalAddonsOnly=true:NoExecute`, applied once the workers were `Ready`.
- **The controller has no scripted bootstrap.** **Half closed in Phase 5.**
  `kubectl` is no longer placed by hand: `playbooks/controller.yml` copies it
  from a cluster node and configures the shell around it, alongside `k9s`.
  Pulumi, Ansible, their virtual environments and the tunnel are still by hand.
- **Node enrollment in FreeIPA.** *Still open.* Still not done, still not needed
  until SSO in Phase 6, and now six nodes rather than three.
- **`ansible-lint` at the `production` profile.** The evidence table above
  records a clean run, which was true when it was written. It is not true today:
  `roles/ipa_service_cert/tasks/issue.yml:80` fails `risky-shell-pipe`. Nothing
  in that file changed, so this is either a lint version moving underneath it or
  a claim that was too broad. Recorded rather than silently fixed, because
  adding `pipefail` to a `docker exec` heredoc can change the failure semantics
  of certificate issuance and that is a Phase 3 decision, not a Phase 5 one.
