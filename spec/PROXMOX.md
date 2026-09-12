# Proxmox Environment

Proxmox Virtual Environment **9.2** runs as a **three-node cluster on dedicated
hardware**: `pve01`, `pve02`, `pve03`. The internal LAN the lab VMs live on is
a cluster-wide overlay, not a per-node bridge.

## Nodes

| Node | IP | CPU | RAM | Disk |
| --- | --- | --- | --- | --- |
| `pve01` | `192.168.1.21` | i7-9700T (8c / 16t) | 62.6 GiB | NVMe lvmthin `local-lvm` |
| `pve02` | `192.168.1.22` | i7-9700T (8c / 16t) | 62.6 GiB | NVMe lvmthin `local-lvm` |
| `pve03` | `192.168.1.23` | i7-9700T (8c / 16t) | 62.6 GiB | NVMe lvmthin `local-lvm` |

All three are identical by design. The dedicated hardware replaces the first
draft's single hypervisor that was itself a VM on a TrueNAS-backed server —
that document recorded an fsync-latency constraint (a virtio disk crossing two
hypervisor layers, 20–31 ms fsync, a thin pool overcommitted to 153%) that
**no longer applies**. Each node's pool is NVMe-backed lvmthin with real
headroom (the `pve/data` thin pool reports ~816 GB at 0% used). What still
carries over from that
era: a thin pool that fills does not degrade gracefully — every guest with a
write in flight stops at once, including the control plane. Watch the pool, not
the guests' free space.

Per-node storage layout (identical names on every node):

- `local-lvm` — lvmthin: VM disks and cloud-init LVs. VM disks use
  `cache=writeback`.
- `local` — dir store: the boot image. **PVE 9.2's lvmthin rejects file
  uploads**, so the image cannot live there; the Pulumi stack imports each
  node's template from the dir store instead.

## Internal network: PVE SDN, not per-node bridges

The internal `192.168.2.0/24` crosses all three nodes via PVE's SDN
subsystem, configured by Pulumi:

- **vxlan zone `labvx`** spanning `192.168.1.21–23`, tag **42**.
- **vnet `vlab`** attached to the zone — its bridge is the attach target every
  VM's internal NIC uses.

There is no per-node `vmbr1` and no Pulumi-managed L2 bridge. If SDN state
ever looks wrong, the failure class is the vxlan zone, the vnet, or the
remote-FDB entries — not a bridge that does not exist on a node.

### The SDN overlay is not self-programming

PVE 9.2 materializes the `vlab` bridge and the `vxlan_vlab` device from the
generated `/etc/network/interfaces.d/sdn`, but it has **no runtime daemon**
that programs the kernel's remote-FDB `dst` bindings from those lines.
Without a static `bridge fdb append <mac> dev vxlan_vlab dst <peer-underlay>`
entry per remote VM MAC, cross-node encap is structurally impossible and the
overlay is dead until the FDB is applied by hand — and a manual apply is
runtime-only, wiped on reboot *and* on every `pve-sdn-commit` (which re-runs
`ifreload`).

So the build cannot rely on a one-time manual apply. `modules/sdn_fdb.py`
makes it repeatable: on every node it intersects the live VMID set with the
estate's SDN NIC map, reads each tap's live MAC, writes a per-node
`/etc/sdn-vlab-fdb/sdn-vlab-fdb.txt`, and installs an `if-up.d` hook that
re-applies those `dst` entries every time `vxlan_vlab` comes up. Deliberately
kept out of `/etc/pve` (csync2 replicates that tree cluster-wide, so a
per-node data file there clobbers the others). If a node's overlay is down
after a commit or reboot, that hook is the thing to check.

## VM placement

One control plane and one worker per node, so a node loss loses at most a
third of each set; the two infra VMs sit on the nodes that do **not** host
`kubecp01` (the API host's node). This is pinned in the stack config via
`deployment:placementOverrides` (`infra/pulumi/Pulumi.dev.yaml.example`); a VM
not listed there falls back to best-fit and its choice is written to the
committed placement record (`.placement.json`), which is what makes a
rebuild's placement stable.

| VMs | Node | Spec (from `vm_definitions.py`) |
| --- | --- | --- |
| `kubecp01`, `kubewk01` | `pve01` | CP: 4 vCPU / 6 GiB; worker: 4 vCPU / 10 GiB |
| `kubecp02`, `kubewk02` | `pve02` | as above |
| `kubecp03`, `kubewk03` | `pve03` | as above |
| `repo01` | `pve02` | 4 vCPU / 10 GiB |
| `core01` | `pve03` | 2 vCPU / 6 GiB |

The control-plane spec is **4 vCPU**: 2 vCPU starves etcd commit latency under
read bursts (the observed `DeadlineExceeded` wedge class). Because storage is
now NVMe, the remaining etcd tuning is burst safety, not latency hiding —
heartbeat 1000 ms / election timeout 5000 ms in
`ansible/inventory/group_vars/kubecp/`, so a slow commit is not mistaken for a
dead leader.

## VM CPU model: `host`, not `x86-64-v2-AES`

The Pulumi VM factory (`modules/vm_factory.py`) presents `cpu_type: "host"` to
every guest, which is a deliberate move **off** `x86-64-v2-AES`. The original
default was `x86-64-v2-AES` (the oldest model that satisfies the x86-64-v2
requirement, chosen so a guest stayed migratable to any future host). It was
changed because the lab's guests run JVMs — FreeIPA's PKI Tomcat and friends,
on OpenJDK 8u502 — and that JIT **hard-crashes** (SIGSEGV in `StringTable`
during the post-config PKI restart) when the presented CPU is feature-limited
to v2 (no AVX). A working CA was worth more than a theoretical cross-host
migration property, so the default is now `host`.

Two facts to carry forward:

- This was met on `core01`'s FreeIPA bootstrap; the crash-loop is the symptom
  and the presented CPU model is the cause.
- **Changing `cpu_type` on an existing VM needs a full power cycle.** A reboot
  from inside the guest keeps the running QEMU process and the CPU it is
  presenting. Plan a cold-boot window for it.

## Credentials

- **Proxmox API**: user `root@pam`, password in `~/.proxmoxpass` (redacted
  here; never in git). The stack config uses `proxmox:insecure: true` for the
  self-signed API cert.
- **Lab VMs**: log in as `root`; the VM password is supplied to Ansible as
  `VM_USER_PASSWORD` from `env.sh`. No SSH keys are installed on the lab VMs
  for this project's access — automation authenticates with the password, via
  `sshpass -f ~/.proxmoxpass` for the PVE nodes themselves.

## Boot image pinning

The Ubuntu 24.04 cloud image the templates import from is **pinned to a dated
release directory and verified against its published SHA256** in
`infra/pulumi/__main__.py`. Changing the URL means changing the checksum
beside it; the two are deliberately coupled so a half-finished edit fails the
download rather than silently widening what is accepted. (The first draft
fetched from `noble/current/` with no checksum, which made rebuilds a
fortnight apart produce different base images from identical source.)

## Do not benchmark all guests at once

A synchronous-write `dd` benchmark run against every node at once issues far
more sustained fsync than the storage tier can carry — in the nested era the
whole hypervisor froze under exactly that load and had to be restarted from
out of band. The rule carries over: measure one guest, once, and keep it off a
live quorum.

## Automation notes

- Pulumi + Python is the IaC interface; the `pulumi_proxmoxve` package tracks
  the bpg provider. Provider behaviour changes with upstream releases — pin
  versions and read release notes before upgrading.
- Any provider operation that needs node-scoped access (SSH, template
  import, the `pve_cleanup` orphan-LV sweep) goes through
  `deployment:managementNode` (`pve01`); any node's API endpoint reaches the
  cluster for most reads.
- VMs are **only** created by Pulumi; host software and configuration is
  Ansible's. Do not hand-create or hand-edit VMs in the PVE UI — the next
  `pulumi up` will reconcile against the program.
