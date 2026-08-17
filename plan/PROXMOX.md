# Proxmox Environment

Proxmox is running as a single node as a VM on a physical linux server using default ports.

## Storage characteristics

Because Proxmox is itself a virtual machine, its only disk is a virtio device
(`/dev/vda`) and every guest write crosses two hypervisors before reaching real
hardware. Throughput is fine; **fsync latency is not**.

Measured from an idle guest on `local-lvm`, with `cache=writeback`, using
`dd if=/dev/zero of=/data1/.ft bs=4k count=500 oflag=dsync`:

| Measure | Value |
| --- | --- |
| Sequential throughput | 63 MB/s |
| 4 KB fsync latency | 20-31 ms, i.e. ~32-50 fsync/s |

The spread is real and depends on how long the host has been up — the lower
figure came from a settled host, the higher from one that had been running for
under an hour with a cold ARC. Neither is good.

### Memory: check the TrueNAS minimum, not just the maximum

The Proxmox VM is configured with 64 GB, but TrueNAS separately holds a
**minimum** allocation, and it was left at 8 GB. The result was a hypervisor
that ballooned itself down and reported maxed-out memory and active swap while
the guests running on it together came nowhere near 64 GB. Raising the minimum
fixed it: the host now sits at 14-29% of 67 GB with zero swap.

Worth knowing for two reasons. The reported memory pressure was real but its
cause was not where it appeared to be, and fixing it did **not** improve fsync
latency — which is how we know the storage constraint and the memory constraint
were independent problems.

This matters for anything whose write path is a serialised fsync per commit —
etcd above all, and to a lesser degree PostgreSQL under GitLab. etcd's own
guidance is a 50 IOPS floor for a light cluster and 500 for a busy one, so this
environment sits exactly at the floor. Phase 4 handles it by raising etcd's
heartbeat and election timeouts so a slow commit is not mistaken for a dead
leader; see `PHASE4_IMPLEMENTATION.md`.

`cache=writeback` is set on every VM disk by the Pulumi VM factory. It helps
less than it appears to: the host page cache acknowledges writes, but explicit
flushes are still passed through, and etcd flushes on every commit. `cache=unsafe`
would discard flushes and be dramatically faster, at the price of a corrupted
filesystem after any host crash — deliberately not used.

Faster backing storage for the Proxmox VM is the only real fix, and it is the
thing to change first if the lab grows beyond the six nodes planned here.

### The thin pool is overcommitted

All eight VMs now exist. `local-lvm` is an 884.9 GiB thin pool holding **1356
GiB of provisioned disk** — 153% — of which about 110 GiB is actually allocated.

**Units, before anyone rechecks this.** The figures above are GiB; the Proxmox
API reports the same pool as 950.2 GB total, 1456 GB provisioned, 118.4 GB
allocated. Both give 153%. Nothing has drifted between them.

That is normal for thin provisioning and is fine at this size. A thin pool that
fills does not degrade gracefully: every guest with a write in flight stops at
once, and that includes the control plane. Watch the pool, not the guests' free
space — a VM can report plenty free while the pool underneath it has none.

#### Longhorn cannot fill it — measured in Phase 6

Phases 4 and 5 both flagged the pool forward as the Phase 6 gate, on the
reasoning that Longhorn's three-replica default turns every gigabyte of volume
into three of pool. That reasoning is correct and the conclusion drawn from it
was wrong, for a reason worth recording.

Longhorn writes into `/var/lib/longhorn`, which is a **fixed 107.4 GB virtual
disk** on each worker. Three of them cap Longhorn's total contribution to the
pool at **322 GB**, however many volumes and replicas are created. Added to
today's allocation the pool reaches ~440 GB of 950 GB at Longhorn's absolute
maximum. The amplification is real; it is bounded by a disk size that was fixed
in Phase 5.

What the replica count actually costs is **usable capacity**, and the default is
the wrong setting on a three-node cluster. Raw capacity is 3 × 97.9 = 293.7 GB:

| Replicas | Usable | fsync per write | Survives 1 node loss | Spare node to rebuild onto |
| --- | --- | --- | --- | --- |
| 1 | 293.7 GB | 1 | no | n/a |
| **2** | **146.9 GB** | **2** | **yes** | **yes** |
| 3 | 97.9 GB | 3 | yes | **none — degraded until repair** |

Three replicas on exactly three nodes puts a copy on every node, which leaves
Longhorn nowhere to rebuild when one fails. Two replicas tolerates the same
single node loss, keeps a spare to rebuild onto automatically, yields 50% more
usable space, and issues one fewer fsync per write on storage measured at 32-50
fsync/s. **Two is the default here**, and it is better than three rather than
cheaper than it.

The pool can still be overrun, but only by the OS and `/data1` volumes filling
together, which is a slower and more visible failure. The acute storage gate for
Phase 6 turned out to be somewhere else entirely: `repo01`'s 30 GB root disk,
which had been quietly absorbing `/var/lib/docker` since Phase 3.

### Do not benchmark all guests at once

A `dd ... oflag=dsync` run against all three cluster nodes simultaneously
issues around 1500 synchronous writes at a layer that sustains roughly 40 per
second. The entire Proxmox VM became unresponsive during exactly that window
and had to be restarted from TrueNAS.

It was not possible to pin the outage on the benchmark: all three guests
stopped logging within 12 seconds of each other with no I/O errors, no hung
tasks and no filesystem errors, which is what a frozen hypervisor looks like
from inside a guest and is indistinguishable from the hypervisor being halted.
The answer would be in TrueNAS's own logs.

Either way, saturating storage that is already at its limit is not a safe thing
to do against a live cluster. Measure one guest, once.

## Nodes
- proxmox-kube
    - datacenter: Datacenter
    - ip: 192.168.1.16
    - cpu: 12
    - ram: 64 GB
  
## Credentials
```
username: root
password: stored in ~/.proxmoxpass
```

## IaC Viability Assessment

### Pulumi (Python) viability
- Viable for this project.
- Pulumi ProxmoxVE package is available for Python and tracks the bpg Proxmox provider.
- Provider supports required capabilities for Phase 1 VM provisioning and configuration handoff.

Constraints to account for:
- Pulumi package behavior follows upstream Terraform provider changes; major upgrades can require state/resource token migration.
- Some provider features requiring nested/advanced configuration may require explicit Provider instance usage.
- For operations that require SSH in upstream provider behavior, provider SSH settings must be explicit.

### Terraform fallback viability
- Strong fallback option with high ecosystem usage and clear operational guidance.
- bpg/proxmox provider is active and broadly used.
- If Pulumi wrapper limitations block progress, fallback is to keep resource model and migrate to Terraform HCL.

### Recommendation
- Proceed with Pulumi + Python for Phase 1.
- Keep a Terraform parity plan for only core resources in Phase 1 (provider config, template image, repo01 VM, network, disk).
- Freeze provider versions during Phase 1 execution to reduce churn.

## Authentication and Security Approach

- Start with username/password auth from local secure environment variables for initial bootstrap.
- Move to API token auth once minimum required privileges are validated.
- Never commit credentials; load from shell environment or local secret files excluded from git.

Suggested environment variables:
- PROXMOX_VE_ENDPOINT
- PROXMOX_VE_USERNAME
- PROXMOX_VE_PASSWORD
- PROXMOX_VE_INSECURE

## Phase 1 Technical Approach

1. Build or import Ubuntu 24.04 cloud image/template in Proxmox. **Pinned to a
   dated release directory and verified against its published SHA256 since
   2026-08-17.** It was fetched from `noble/current/` with no checksum, which
   made two rebuilds a fortnight apart produce different base images from
   identical source — and made the disk every VM here is imported from the only
   artifact in the environment that nothing verified. Changing the URL means
   changing the checksum beside it in `infra/pulumi/__main__.py`; the two are
   deliberately coupled so that a half-finished edit fails the download rather
   than silently widening what is accepted.

   Note for the first `pulumi up` after that change: the image resource is
   **replaced**, so the file is deleted from the datastore and re-downloaded
   with verification. The existing VMs are updated, not replaced — they imported
   their disks at creation and do not re-read the image.
2. Create Pulumi project using Python and pinned provider version.
3. Define explicit Proxmox provider resource and use it for all VM resources.
4. Provision repo01 VM from template with:
     - CPU, memory, disk from target definition
     - dual NIC setup (external + internal)
     - cloud-init user, SSH key, static addressing
5. Configure controller-to-internal tunnel path:
    - deploy WireGuard endpoint on `repo01`
    - configure WireGuard peer on automation controller
    - enable IPv4 forwarding on `repo01`
    - apply SNAT/masquerade on `repo01` for tunnel subnet to `192.168.2.0/24`
    - add controller static route for `192.168.2.0/24` via WireGuard interface
6. Export required connection outputs (IP/FQDN) for Ansible inventory generation.
7. Run Ansible in stages:
    - base host prep
    - WireGuard tunnel gateway
    - SOCKS5
    - apt proxy
    - GitLab
8. Execute smoke tests and capture evidence in runbook.

## Controller Tunnel Reference Design (Phase 1)

Recommended minimal setup: WireGuard point-to-point tunnel.

Proposed tunnel subnet:
- `10.66.66.0/30`
- controller WG IP: `10.66.66.1/30`
- `repo01` WG IP: `10.66.66.2/30`

Routing behavior:
- controller route: `192.168.2.0/24` via `wg0`
- `repo01` forwards between `wg0` and internal NIC
- `repo01` applies SNAT for source `10.66.66.0/30` toward `192.168.2.0/24`

Why SNAT first:
- avoids immediate dependency on adding return routes to internal network gateways
- keeps initial rollout simple and reversible

Future optimization (optional):
- remove SNAT and add explicit return route on internal gateway (`192.168.2.20`) for `10.66.66.0/30` via `192.168.2.99`
- preserves original source IP visibility from controller

## Known Risks and Mitigations

- Risk: Provider upgrade introduces breaking resource token or schema changes.
    - Mitigation: Pin Pulumi and provider versions in Phase 1; defer upgrades until phase completion.

- Risk: Proxmox API auth mode differences block some operations.
    - Mitigation: Keep bootstrap on known-good auth method; test token auth in isolated change set.

- Risk: Multi-step VM customization creates drift between IaC and Ansible.
    - Mitigation: Keep VM creation in IaC only, host software/config only in Ansible.

- Risk: GitLab footprint exceeds VM sizing under load.
    - Mitigation: Begin with conservative single-node settings and capture resource telemetry early.

- Risk: Tunnel or forwarding misconfiguration blocks controller access to internal nodes.
    - Mitigation: Add explicit tunnel validation tasks (`ping`, TCP/22 checks, Ansible ad-hoc command) before running service roles.
    - **Implemented 2026-08-17**, five phases after it was written, by the third
      play of `playbooks/tunnel_controller_access.yml`. Three checks, each a
      different question: the interface is up and the gateway is a peer of it;
      a handshake has completed, which is what a wrong key breaks; and a TCP
      port inside `192.168.2.0/24` answers through the tunnel. Until then the
      failure this mitigation names surfaced as every later playbook timing out
      against an internal host, with an error naming the host and saying
      nothing about the path to it.
