# Phase 5 Implementation Plan — RKE2 Workers

## Scope

Add three worker nodes — `kubewk01-03` — to the control plane Phase 4 built,
installed from the same GitLab-hosted artifact set and with the same absence of
any route to the internet. Phase 5 also settles the three decisions Phase 4
deliberately left to it: the ingress controller, whether the control plane
carries a taint once there is somewhere else to schedule, and how each worker's
third disk is prepared for the CSI layer Phase 6 deploys.

The cluster goes from three nodes to six, and from "healthy but with nothing to
run on" to a cluster that can actually host a workload.

## Decision

| | |
| --- | --- |
| Primary path | RKE2 `v1.35.7+rke2r1` agents joining `https://192.168.2.20:9345`, binary and images from GitLab exactly as in Phase 4, **Traefik** as the ingress controller, control plane tainted `CriticalAddonsOnly=true:NoExecute` once the workers are `Ready`, and each worker's third disk formatted and mounted for Longhorn |
| Fallback path | If a worker cannot join through the VIP, join it against a node address directly and carry the VIP as a defect — the agent syncs the full server list once joined, so the seed address only has to work once |

## Preconditions

- Phase 4 exit criteria hold: three servers `Ready`, etcd healthy, the VIP
  answering, and the registry mirror serving every image the cluster pulls.
- `RKE2_TOKEN` and `GITLAB_ROOT_PASSWORD` are set in `~/.config/rke2lab/env.sh`.
  Phase 5 adds no new secrets.
- The `rke2-nodes` deploy token still exists on `repo01` at
  `/data1/gitlab/rke2-deploy-token.yml`. Workers read the same credential the
  servers do.

---

## Review

Performed 2026-08-14 against the live environment.

| Check | Result |
| --- | --- |
| Tunnel to internal network | `wg0` up, `10.66.66.2/30`, `kubecp01` reachable |
| Control plane | `kubecp01-03` all `Ready`, `v1.35.7+rke2r1` |
| etcd | One `Running` member per server |
| Cilium | Both operators and all three agents `Running` |
| Control plane taints | **None** — as Phase 4 left them |
| `kubectl` from the controller | Works over the tunnel against `kube.dev.lo` |
| GitLab registry contents | `rke2/images/rancher/` holds 26 repositories, `rke2/images/kube-vip/` holds one — verified on `repo01`'s registry storage |
| `/data1` on `repo01` | 87 GB free |
| Pulumi `kubewk01-03` specs | Present in `vm_definitions.py`, match `TARGETS.md` |
| Pulumi `phaseLimit` | **`4`** — Phase 5 VMs are not in the deployment set |
| `kubewk` inventory group | **Absent** |
| `kubewk01-03` DNS records | **Absent** |
| Proxmox pool `local-lvm` | 884.9 GB total, 75.0 GB allocated, thin-provisioned |
| Proxmox memory | 18.4 GB of 62.8 GB used, **zero swap** — the TrueNAS fix is holding |

Nothing had drifted in the sense of something changing underneath the plan.
What the review found instead was three things Phase 5 has to add and one piece
of arithmetic nobody had done.

### The worker inventory does not exist yet

`hosts.yml` has no `kubewk` group, there is no `group_vars/kubewk/`, and
`freeipa_server_dns_records` names the three control plane nodes and the VIP but
no workers. All three are Phase 5's to add, and all three follow the shapes
Phase 4 established rather than inventing new ones.

### `phaseLimit` is 4, not 5

Same as every phase that adds VMs. It goes to `5`.

### The control plane pods restarted 22 minutes ago

Every `cilium`, `kube-controller-manager`, and `kube-scheduler` pod shows a
restart within the same few minutes, and the Proxmox host reports an uptime of
0.4 hours. This is the third host outage the Phase 4 document records, and the
cluster recovered from it unattended again, which is the reassuring reading.

The unhelpful part is that it resets the clock on Phase 4's one open
measurement: a settled fsync reading from a host that has been stable for
hours. It is still not available, and Phase 5 does not block on it — but see
the storage section below, because Phase 5 changes the shape of the question.

### The thin pool is about to be overcommitted, and nobody had checked

`local-lvm` is an 884.9 GB thin pool. The five existing VMs *provision* 660 GB
of it and have actually allocated 75 GB, which is the point of thin
provisioning and has been invisible so far because it did not matter.

Three workers at 232 GB each — a 32 GB OS disk, a 100 GB `/data1`, and a 100 GB
CSI disk — provision another 696 GB. That takes the pool to **1356 GB
provisioned against 885 GB of real capacity, or 153%.**

| Stage | Provisioned | Realistically allocated |
| --- | --- | --- |
| Today, five VMs | 660 GB | 75 GB |
| After Phase 5 | 1356 GB | ~120 GB |
| Phase 6 with CSI disks in use | 1356 GB | 420 GB+ |

This is not a Phase 5 gate: the workers will allocate perhaps 15 GB each, and
the pool ends the phase around 14% full. It becomes a gate in Phase 6, and the
reason is written down in the storage section rather than left for whoever
meets it. A thin pool that fills does not degrade — every guest with a write in
flight stops at once, which on this lab means the control plane too.

---

## Research

### Ingress: Traefik, and `CLUSTER_COMPONENTS.md` was right for the wrong reason

Phase 4 flagged that `CLUSTER_COMPONENTS.md` recorded "Traefik v3 (RKE2
default)" and that this was wrong — RKE2's packaged default is `ingress-nginx`
and Traefik is the K3s default. That correction was accurate when it was
written and the conclusion it pointed at has since inverted.

Per RKE2's own migration reference:

- The Kubernetes project **announced ingress-nginx's retirement in March 2026**.
  It is end-of-life today, five months ago.
- **Traefik becomes RKE2's default ingress controller for new clusters at
  v1.36.** This lab runs `v1.35.7`, deliberately one minor behind.
- Migration to Traefik is supported from `v1.32.11+rke2r1` onward, so `v1.35.7`
  is inside the supported window.

Deploying a retired controller into a cluster being built now, and then owing
Phase 6 a migration off it, is a worse trade than paying for one extra artifact
today.

| Question | Decision | Why |
| --- | --- | --- |
| Ingress controller | **Traefik** | ingress-nginx is EOL; Traefik is the default from the next minor. The document this repo already had was correct, and the Phase 4 note explaining why it was wrong is what is now out of date |
| Version | `v3.7.8-build20260717` | Whatever `rke2-images-traefik` for this release contains. Same reasoning as Cilium: take RKE2's bundled chart and take its tested pairing |
| How it is selected | `ingress-controller: [traefik]` in `config.yaml` | Confirmed against `rke2 server --help` on the running v1.35.7 binary: `Ingress Controllers to deploy, one of none, ingress-nginx, traefik; the first value will be set as the default ingress class (default: "ingress-nginx")`. This replaces Phase 4's `disable: [rke2-ingress-nginx]`, which was the blunt instrument for the same job |
| Migration path | **None needed** | Phase 4 disabled ingress entirely, so there is no nginx to migrate from. The vendor's four-phase dual-controller procedure exists for clusters that already serve traffic; this one does not. Reading it was still worth it — it is where the retirement is stated |

The migration reference also notes that not all nginx annotations have Traefik
equivalents. That is a Phase 6 concern and costs nothing now, precisely because
there are no existing Ingress objects to carry annotations.

#### The artifact cost is one tarball, and its checksum chain already terminates here

`rke2-images-traefik.linux-amd64.tar.zst` is a separate release asset — the
core tarball does not contain it, which the vendor doc states explicitly for
air-gapped installs.

The useful part is that `sha256sum-amd64.txt`, already staged on `repo01` and
already recorded in the manifest with a checksum of its own, contains the
entries for both the Traefik tarball and its image list:

```
6dabff733c90a3c2c7304bfe0c530cb6a2304c5a585b0de4fc5f46e700c5fcee  rke2-images-traefik.linux-amd64.tar.zst
42f401dbd52251dd96e09ed22563a88cb269c62b2e775d0f4910745e90b4d06e  rke2-images-traefik.linux-amd64.txt
```

Verified by downloading the list and hashing it: `42f401db…`, matching. So the
new manifest entries carry Rancher's own checksums, from a file this repository
already pins, exactly as the Phase 4 entries do.

The set contains **one image**:

```
docker.io/rancher/hardened-traefik:v3.7.8-build20260717
```

It is a `docker.io/rancher/*` name, so the existing containerd rewrite
(`^rancher/(.*)` → `rke2/images/rancher/$1`) already covers it. Phase 5 adds no
mirror rule, no registry credential, and no new rewrite — one manifest entry
pair and one line in `rke2_publish_image_sets`.

### CSI disk preparation: Longhorn

`CLUSTER_COMPONENTS.md` lists the CSI choice as Rook Ceph or Longhorn, and the
two want opposite things from a disk — Ceph wants a raw block device with no
filesystem signature, Longhorn wants a filesystem mounted at a path. The disk
cannot be prepared for both, so Phase 5 cannot defer the choice and still claim
to have prepared anything.

| Question | Decision | Why |
| --- | --- | --- |
| CSI layer | **Longhorn** | Lighter on both the storage and the nodes. Ceph runs an OSD plus MON and MGR per node on 4 vCPU / 8 GiB workers that also have to carry Phase 6's observability stack, and its write path amplifies against a disk already at etcd's fsync floor |
| Rook's object storage argument | Does not apply | The usual reason to take Ceph despite the weight is that it brings object storage with it. `CLUSTER_COMPONENTS.md` already selects **Garage** for object storage, so that advantage is redundant here |
| Disk preparation | ext4 on `/dev/sdc`, mounted at `/var/lib/longhorn` | Longhorn's default data path. Preparing it now means Phase 6 declares a Helm release rather than doing host-level disk work from a GitOps phase that should not own it |
| Mechanism | The existing `data_volume` role, included a second time | It already does exactly this — assert the device, create a filesystem only if the device has none, mount it, record it in fstab. It needs no change; it needs a second invocation with different variables |

**Longhorn's default replica count is 3, and that interacts with the thin
pool.** Every gigabyte of PVC becomes three gigabytes across the CSI disks and
therefore three gigabytes of thin pool. 100 GB of provisioned volumes consumes
300 GB of an 885 GB pool that is already 153% overcommitted. Phase 6 either
drops the replica count to 2 or sizes its volumes knowing this. Written here
because the arithmetic belongs with the decision that causes it.

### The control plane taint

| Question | Decision | Why |
| --- | --- | --- |
| Taint the servers | **Yes**, `CriticalAddonsOnly=true:NoExecute` | Phase 4's reason for leaving them untainted was that tainting would have left nothing schedulable. That reason expires the moment the workers are `Ready`. The servers are 2 vCPU / 4 GiB nodes running the etcd that is this lab's most fragile component |
| Effect | `NoExecute`, not `NoSchedule` | `NoSchedule` keeps new pods off but leaves what is already there. The packaged charts Phase 4 deployed are running on the control plane right now, and moving them is the point |
| What does not move | kube-vip, Cilium, the static pods | kube-vip's DaemonSet already carries blanket `NoSchedule`/`NoExecute` tolerations — Phase 4 wrote them in. Cilium's agent tolerates everything by design, and apiserver, scheduler, controller-manager and etcd are static pods that no taint touches |
| What does move | metrics-server, snapshot-controller, CoreDNS if it does not tolerate | Onto the workers, which is the intent |
| Ordering | **After** the workers report `Ready`, never before | A `NoExecute` taint applied while the workers are still joining evicts CoreDNS with nowhere to go, and cluster DNS stops. This is Risk Gate 4 |

**The taint has to be applied twice, in two different ways, and that is not
redundancy.** `node-taint:` in `config.yaml` is passed to the kubelet as
`--register-with-taints`, which applies **only at first registration** — it does
nothing to the three nodes that registered hours ago. So the config entry exists
for correctness on a rebuilt node, and a `kubectl taint` step reconciles the
nodes that already exist. Either one alone leaves a gap: config alone changes
nothing today, `kubectl` alone means a rebuilt node comes back untainted.

### The worker role label has the same shape of problem

Pinning Traefik to the workers wants a label like
`node-role.kubernetes.io/worker=true`, and the obvious way to set it is
`node-label:` in the agent's `config.yaml`. That does not work: the
NodeRestriction admission plugin forbids a kubelet from setting any label under
`node-role.kubernetes.io/`, so the kubelet is refused and the node registers
without it.

The label is therefore applied with `kubectl` from the playbook, alongside the
taint, for the same reason and by the same mechanism.

### Joining a worker

| Question | Decision | Why |
| --- | --- | --- |
| Install path | The existing `rke2_node` role with `rke2_node_type: agent` | It was written for this. Its defaults already say so, and the only difference at that layer is what `INSTALL_RKE2_TYPE` is set to |
| Cluster join | A new `rke2_agent` role | The server role carries etcd tolerances, `tls-san`, CNI selection, kube-vip and a kubeconfig copy, none of which an agent has. Sharing it would mean a role that is half `when:` conditions |
| Join address | `https://192.168.2.20:9345`, the VIP | Same as the servers. Only seeds the agent's client-side load balancer; once joined it syncs the full server list and no longer depends on the address |
| Readiness gate | `kubectl wait`, delegated to the bootstrap server | An agent has no kubeconfig and no kubectl — `/etc/rancher/rke2/rke2.yaml` exists only on servers. The gate has to be asked from somewhere that can see the API, and the node must be looked up by its **fully qualified** name, which is the trap Phase 4 recorded |
| Play serialisation | `serial: 100%` — all three agents join in one batch | Agents joining do not have etcd's quorum problem, so the joiner's host build and image pulls are free to overlap. Three at a time was the original plan all along: with the local GitLab registry and package mirror in place the download path absorbs simultaneous pulls, and the full-rebuild loop confirmed it (single-batch join, `failed=0`, workers `changed=1` on the idempotent re-run). The old `-e kubewk_serial=1` override knob was removed with the `serial: 2` default; if the lab's download path or storage is ever under load, serialise the join manually by limiting the host list. See playbooks/kubewk.yml |

### Capacity for three more nodes

| Resource | Today | After Phase 5 | Verdict |
| --- | --- | --- | --- |
| Memory | 24 GB allocated of 62.8 GB | 48 GB allocated | Fits, with ~15 GB left for the host. Phase 6 is what will test this |
| vCPU | 12 allocated on 12 physical cores | 24 on 12 | 2:1 overcommit. Acceptable for a lab; worth remembering if the control plane starts looking slow |
| Thin pool | 75 GB allocated of 885 GB | ~120 GB allocated, 1356 GB provisioned | Fits now. See the Review section for why Phase 6 has to care |
| fsync latency | 20-31 ms, at etcd's floor | Unchanged by adding agents | Workers run no etcd. This is the one thing Phase 5 does *not* make worse |

That last row is worth stating plainly, because `PHASES.md` asks whether the
storage can carry three more nodes and the honest answer is more specific than
yes or no. Agents add no etcd members and therefore add no fsync pressure to
the thing that is actually constrained. What they add is image pulls and
container writes, which are throughput, and throughput was never the problem —
63 MB/s is ample. **Phase 6 is the phase that changes this**, because
Longhorn's replicated writes and Prometheus's write-ahead log are both
fsync-shaped.

### Operator access to the cluster

Added after the phase was otherwise complete, because the cluster was working
and still could not be *looked at* without knowing two undocumented things.

The gap was precise, and neither half was obvious:

| Where | What was wrong |
| --- | --- |
| The controller | `kubectl` was on `PATH`, but nothing set `KUBECONFIG`. A bare `kubectl get pods` therefore tried the default `~/.kube/config`, which does not exist, and failed for a reason that looks like a broken cluster rather than a missing variable |
| The servers | `kubectl` is inside the RKE2 data directory, `/data1/rancher/rke2/bin`, which is on nobody's `PATH`, and its kubeconfig is `0600` root-owned. Every documented command therefore carried an absolute path and an explicit `--kubeconfig`, which is why the runbooks in this repository are written that way |
| The workers | `crictl` needs `--runtime-endpoint unix:///run/k3s/containerd/containerd.sock` on every invocation, because nothing writes `/etc/crictl.yaml` |

This is the same debt `OVERVIEW.md` names — "every controller dependency must be
documented and scripted" — showing up from the other side. Phase 4 recorded that
`kubectl` had been placed on the controller by hand and that nothing in the
repository remembered it. That is now scripted.

| Question | Decision | Why |
| --- | --- | --- |
| Where `kubectl` on the controller comes from | Copied from `kubecp01`'s own `<data-dir>/bin/kubectl` | Exactly the cluster's version, by construction, and no download. This is what Phase 4 did by hand; the only change is that a role does it now |
| Cluster browser | **k9s `v0.51.0`**, on the **controller only** | It is the direct answer to "what pods are up". Kept off the cluster nodes deliberately: the controller has internet so it needs no staging, and an air-gapped node with fewer binaries on it is the better posture. Pinned and checksummed like every other artifact |
| Shell integration | A generated snippet, sourced from `~/.bashrc` by a marked block | Writing `export` lines directly into a personal dotfile makes them impossible to update or remove cleanly. One marked block sourcing one generated file is reversible |
| `KUBECONFIG` on a server | Exported only if the file is readable | It is `0600` root-owned, so an unprivileged shell that exported it anyway would get a permission error on every command and look like a broken cluster. Such a shell simply gets no `KUBECONFIG`, and `sudo -i` gets a working one. Note that no unprivileged user exists on these hosts today, so this path is currently untested by anything but design |
| Workers | `crictl` configured, no `kubectl` | There is no kubeconfig on an agent, so `kubectl` there would have nothing to point at. `crictl` is the tool the runbook actually sends people to a worker for |

### Still open

- **Node enrollment in FreeIPA.** Unchanged from Phase 4, still not needed, and
  the workers are in the same position as the servers.
- **The controller has no scripted bootstrap.** Unchanged from Phase 4, and
  still a debt rather than a fix.
- **A settled storage measurement.** The host rebooted 24 minutes before this
  review, so it is still unavailable. Phase 6 needs it more than Phase 5 does.

---

## Implement

**1. Add the Traefik artifacts** — `group_vars/repo/artifacts.yml` gains the
tarball and its image list, and `rke2_publish_image_sets` gains a `traefik`
entry so the push happens the same way the core and cilium sets did.

**2. Publish the worker DNS records** — `freeipa_server_dns_records` gains
`kubewk01-03`, applied by re-running `playbooks/core01.yml`.

**3. Raise the Pulumi phase limit to 5 and create `kubewk01-03`.**

**4. Add the worker inventory** — a `kubewk` group in `hosts.yml` and a
`group_vars/kubewk/main.yml` carrying the same shape as `kubecp`, plus the
second data volume for Longhorn.

**5. Prepare and join the workers** — `playbooks/kubewk.yml`: `base_host`,
`data_volume` twice, `time_sync`, `rke2_node` with `agent`, then a new
`rke2_agent` role that renders `config.yaml`, starts `rke2-agent`, and gates on
the node reporting `Ready` as seen from the bootstrap server.

**6. Label the workers**, then **switch the control plane to Traefik**, then
**taint the control plane** — strictly in that order, and only once every
worker is `Ready`.

**7. Gate on a transaction** — six nodes `Ready`, a Deployment that schedules
onto the workers and pulls its image through the mirror, Traefik answering, and
the CSI disks mounted.

---

## Test

**Cluster**

- Six nodes `Ready`; the three workers carry the worker role label.
- A Deployment with three replicas schedules only onto workers once the taint
  is applied.
- Pod-to-pod networking works across nodes, worker to server and worker to
  worker.
- etcd still reports three healthy members — adding agents must not touch it.

**Air gap**

- Every worker image was pulled from `registry.gitlab.dev.lo`, confirmed in
  containerd's logs.
- A direct upstream fetch from a worker fails.
- `<data-dir>/agent/images` is empty on every worker, as it is on the servers.

**Ingress**

- The `rke2-traefik` HelmChart deploys and its pods run on workers.
- `traefik` is the default IngressClass.
- An Ingress object routes to a test Service.

**Storage**

- `/var/lib/longhorn` is mounted from `/dev/sdc` on all three workers and
  survives a reboot.
- `/data1` is mounted before RKE2 starts on every worker.

**Operational**

- A second Ansible run reports no changes across all hosts.
- The cluster survives a reboot of all six nodes.
- `yamllint` and `ansible-lint` clean at the `production` profile.

**Verified on 2026-08-14**

| Check | Evidence |
| --- | --- |
| Traefik published | `rke2/images/rancher/hardened-traefik` in the GitLab registry, pushed from the tarball the manifest staged |
| Six nodes | `kubecp01-03` and `kubewk01-03` all `Ready`, `v1.35.7+rke2r1` |
| Worker role label | All three workers carry `node-role.kubernetes.io/worker=true`, applied with kubectl because NodeRestriction refuses the kubelet |
| Control plane taint | `CriticalAddonsOnly` on all three servers, none on the workers |
| Both disks | `/dev/sdb` → `/data1` and `/dev/sdc` → `/var/lib/longhorn` on all three workers, ~90 GB free each |
| **Ingress class** | `traefik` only; no nginx class exists |
| **Traefik placement** | Three replicas, one per worker, none on a server |
| **Ingress routing** | `HTTP 200` through all three workers for a real backend, and `404` for an unmatched Host — the second is what makes the first mean something |
| Cross-node networking | The routing test crossed nodes by construction: Traefik on each worker reached backends on the other two |
| Air gap | `crictl pull` by upstream name succeeds on every worker; a direct fetch of `registry-1.docker.io` fails; `<data-dir>/agent/images` empty on all three |
| etcd | Still three running members — Phase 5 did not disturb it |
| Idempotency | `kubewk.yml` and `kubecp.yml`: `changed=0` on every host |
| Reboot survival | All three workers rebooted in turn; each returned `Ready` with both volumes mounted |
| Capacity after the phase | Thin pool 108 GB of 885 GB (12.2%), host memory 41.6 GB of 62.8 GB, **zero swap** — both as predicted above |
| Lint | `yamllint` clean; `ansible-lint` clean at `production` apart from one pre-existing Phase 3 violation, noted below |

**A kube-vip failover happened during `pulumi up`, and recovered unattended.**
The provider applied a disk-settings update to all five running VMs, the VIP
stopped answering for a few seconds, and `kubectl` reported connection refused.
The control plane VMs never restarted — their uptime was continuous across it —
and the VIP came back on its own with `kubecp01` holding it exactly once. This
is the deliberate leader-failover test Phase 4 never got round to running,
obtained the same accidental way Phase 4 got its reboot tests.

---

## Risk Gates

| Gate | Condition | Action |
| --- | --- | --- |
| 1. Traefik push | `repo01` cannot publish the Traefik image set into GitLab | Stop before creating any VM. A worker that joins and then cannot pull the ingress controller is a harder thing to diagnose than a push that failed loudly |
| 2. Thin pool | `local-lvm` allocation passes 70% at any point | Stop and reclaim before continuing. A full thin pool stops every guest at once, control plane included — this is not a worker-only failure |
| 3. Worker join | An agent does not reach `Ready` | Read `journalctl -u rke2-agent`, then the containerd log. Do not join the next worker against a cluster that has just failed to accept one |
| 4. Taint ordering | Any worker is not yet `Ready` | Do not apply the `NoExecute` taint. Evicting CoreDNS with nowhere to schedule takes cluster DNS down, and every subsequent symptom will point somewhere else |
| 5. Disk | `/data1` or `/var/lib/longhorn` is not mounted before RKE2 starts | Stop. Same failure Phase 1 warned about and Phase 4 gated on: the data lands on the 32 GB OS disk and the node fills silently |

---

## Runbook

### Things that cost a failed run here

**Traefik does not re-read a Service whose target port changed.** The ingress
test returned `502` with a backend that was demonstrably serving `200` on its
pod IP, correct EndpointSlices, and a correctly matched router. The Service had
originally been created pointing at the wrong container port and then patched;
Traefik kept routing to the old one. A `rollout restart` of the DaemonSet fixed
it immediately. Worth knowing before concluding that ingress is broken, because
every other piece of evidence said it was working.

**`502` and `404` mean different things here, and the difference is the test.**
An unmatched Host returns `404` — no router matched. A matched Host with a
failing backend returns `502`. So a `502` is already proof that the Ingress
object was found and served; only the backend is wrong. Checking both a routed
and an unrouted Host is what separates "ingress works" from "everything returns
404 and I cannot tell why".

**A phase's validation has to survive the next phase.** Phase 4's
`validate_phase4.yml` asserted the cluster had exactly `groups['kubecp'] |
length` nodes and no others. That was correct while the control plane was the
whole cluster and became wrong the moment Phase 5 joined workers, so a healthy
six-node cluster failed a Phase 4 check. It now asserts that every server it
owns is registered and says nothing about nodes it does not own. Any later
phase's validation should be written the same way.

**The packaged chart is `rke2-traefik`, not `traefik`.** Its pods are labelled
`app.kubernetes.io/name=rke2-traefik`, so the upstream chart's usual selector
matches nothing. A check written against the upstream label reports Traefik
running nowhere while three replicas are running perfectly.

### Where to look when a worker does not join

```bash
journalctl -u rke2-agent -n 200 --no-pager
crictl --runtime-endpoint unix:///run/k3s/containerd/containerd.sock images
```

From a server, not from the worker:

```bash
kubectl get nodes -o wide
kubectl describe node kubewk01.dev.lo
```

A worker that never appears in `kubectl get nodes` did not reach the supervisor
— that is the VIP, the token, or the CA, and the answer is in the agent
journal. A worker that appears and stays `NotReady` did reach it, and the
answer is Cilium and whether it could pull.

### Reaching the cluster

**Nothing to export any more.** `playbooks/controller.yml` sets `KUBECONFIG` in
the shell environment, so a new terminal can simply do:

```bash
kubectl get nodes                 # or `k get nodes` — the alias is set up too
kubectl get pods -A               # everything running, every namespace
kubectl get pods -A -o wide       # ...and which node each one is on
kubectl get pods -A --field-selector=status.phase!=Running   # only what is unhappy
```

**To just look at the cluster, run `k9s`.** It opens on the pod list; `0`
shows all namespaces, `:nodes` / `:deploy` / `:svc` switch views, `l` tails a
pod's logs, `d` describes, `Ctrl-C` quits. It is the fastest answer to "what is
actually running", and it is installed on the controller only.

Both of these need the tunnel up, because they reach the API through the VIP.

On a control plane node, `kubectl` is now on `PATH` and needs no `--kubeconfig`,
but only as root — the kubeconfig is `0600` root-owned:

```bash
sudo -i
kubectl get nodes
crictl images          # no --runtime-endpoint needed any more
```

On a worker there is no `kubectl`, deliberately, because an agent has no
kubeconfig for it to use. `crictl` is there and is the reason to be on a worker
at all:

```bash
sudo crictl images
sudo crictl ps
```

Rebuilding this on a fresh controller is `ansible-playbook
playbooks/controller.yml`, which needs the cluster to already be up: it takes
`kubectl` from a node rather than downloading one, then proves the result
reaches the API before reporting success.

## Deliverables at Phase 5 Completion

- A six-node RKE2 cluster — three servers, three workers — installed entirely
  from GitLab-hosted artifacts with no internet access from any node.
- Traefik as the ingress controller, replacing the end-of-life ingress-nginx
  that `CLUSTER_COMPONENTS.md` had already, accidentally, selected against.
- The control plane tainted `CriticalAddonsOnly`, with workloads on the workers.
- Each worker's CSI disk formatted and mounted for Longhorn, so Phase 6
  deploys a Helm release rather than partitioning disks.

## Status

Delivered and verified.

**Complete:** a six-node RKE2 cluster — three servers, three workers — installed
entirely from GitLab-hosted artifacts with no route to the internet from any
node. Traefik is the ingress controller and demonstrably routes traffic, the
control plane carries `CriticalAddonsOnly` so workloads land on the workers, and
each worker's CSI disk is formatted and mounted at `/var/lib/longhorn` ready for
Phase 6. The roles are idempotent, the whole set lints clean, and both the
Phase 4 and Phase 5 validation suites pass.

The one new artifact — the Traefik image set — is published in GitLab and was
pulled from there by nodes that cannot reach the internet, which is the same
property every phase since Phase 3 has had to demonstrate rather than assert.

**Carried into Phase 6 — storage, and now with a number attached.** Phase 5 did
not make the fsync problem worse, and the reason is worth keeping: agents run no
etcd, so they add throughput rather than fsync pressure. Phase 6 does not have
that excuse. Longhorn replicates writes and Prometheus keeps a write-ahead log,
and both are fsync-shaped against a disk that sustains roughly 40 per second.

The thin pool now sits at 108 GB of 885 GB against 1356 GB provisioned. That is
comfortable today and is a real gate in Phase 6, because Longhorn's default
three replicas turn every gigabyte of volume into three gigabytes of pool. A
thin pool that fills does not degrade gracefully; every guest with a write in
flight stops at once, and that includes the control plane.

**Also open:**

- **A settled storage measurement.** Still not taken. The host had been up 24
  minutes at Review and the cluster has been under change ever since. Phase 6
  needs this more than Phase 5 did — it should be the first thing that phase
  does, on a host left alone for a few hours, one guest, one run.
- **A pre-existing lint violation.** `roles/ipa_service_cert/tasks/issue.yml:80`
  fails `risky-shell-pipe`. It is Phase 3 code, untouched by this phase, and
  adding `pipefail` to a `docker exec` heredoc could change the failure
  semantics of certificate issuance — so it is reported rather than changed as a
  side effect of Phase 5. Phase 4's claim that the repository lints clean is
  therefore slightly out of date.
- **The controller bootstrap is only half scripted.** `kubectl` and `k9s` are
  now installed and configured by `playbooks/controller.yml`, which closes the
  part of the Phase 4 debt that this phase tripped over. Pulumi, Ansible, their
  virtual environments, and the WireGuard tunnel are still set up by hand and
  are still owed.
- **There is no unprivileged user on any host.** All eight VMs are root-only,
  confirmed by enumerating their accounts. `TARGETS.md` and
  `PHASE1_IMPLEMENTATION.md` both claimed a `devops` user that has never
  existed; both now say `root`, and `TARGETS.md` carries the decision this
  raises. Nothing depends on it today, but administrative access with no named
  account behind it is normally an audit finding, so it is a decision to make
  rather than a discrepancy to close.
- **Node enrollment in FreeIPA.** Unchanged, and now six nodes rather than
  three. Still not needed until SSO in Phase 6.
- **Ingress TLS.** Traefik serves plain HTTP today. Nothing has issued it a
  certificate, and cert-manager is a Phase 6 component.
