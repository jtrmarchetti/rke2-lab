===============================
Proxmox and the VM estate
===============================

The hypervisor layer the lab VMs run on, and the per-VM specifications that
Pulumi builds from. This page is the source of truth that
``infra/pulumi/modules/vm_definitions.py`` must stay in sync with.

The hypervisor
==============

Proxmox Virtual Environment **9.2** runs as a **three-node cluster on
dedicated hardware**: ``pve01``, ``pve02``, ``pve03``. The internal LAN the
lab VMs live on is a cluster-wide overlay, not a per-node bridge.

.. list-table::
   :header-rows: 1
   :widths: 14 20 30 36

   * - Node
     - IP
     - CPU / RAM
     - Disk
   * - ``pve01``
     - ``192.168.1.21``
     - i7-9700T (8c / 16t), 62.6 GiB
     - NVMe lvmthin ``local-lvm``
   * - ``pve02``
     - ``192.168.1.22``
     - i7-9700T (8c / 16t), 62.6 GiB
     - NVMe lvmthin ``local-lvm``
   * - ``pve03``
     - ``192.168.1.23``
     - i7-9700T (8c / 16t), 62.6 GiB
     - NVMe lvmthin ``local-lvm``

All three are identical by design. Per-node storage layout (identical names on
every node):

- ``local-lvm`` — lvmthin: VM disks and cloud-init LVs. VM disks use
  ``cache=writeback``.
- ``local`` — dir store: the boot image. PVE 9.2's lvmthin rejects file
  uploads, so the image cannot live there; the Pulumi stack imports each
  node's template from the dir store instead.

Two standing facts about this storage tier:

- A thin pool that fills does not degrade gracefully — every guest with a
  write in flight stops at once, including the control plane. **Watch the
  pool, not the guests' free space.**
- A datastore that fsyncs on every commit is bound by fsync latency, and a
  disk can be fast by every other measure while being unusable for it.
  Match the storage to the write pattern, not to a throughput number.

Internal network: PVE SDN
==========================

The internal ``192.168.2.0/24`` crosses all three nodes via PVE's SDN
subsystem, configured by Pulumi:

- **vxlan zone ``labvx``** spanning ``192.168.1.21-23``, tag **42**.
- **vnet ``vlab``** attached to the zone — its bridge is the attach target
  every VM's internal NIC uses.

There is no per-node ``vmbr1`` and no Pulumi-managed L2 bridge. If SDN state
ever looks wrong, the failure class is the vxlan zone, the vnet, or the
remote-FDB entries — not a bridge that does not exist on a node.

The SDN overlay is not self-programming
---------------------------------------

PVE 9.2 materializes the ``vlab`` bridge and the ``vxlan_vlab`` device from
the generated ``/etc/network/interfaces.d/sdn``, but it has **no runtime
daemon** that programs the kernel's remote-FDB ``dst`` bindings from those
lines. Without a static ``bridge fdb append <mac> dev vxlan_vlab dst
<peer-underlay>`` entry per remote VM MAC, cross-node encap is structurally
impossible and the overlay is dead until the FDB is applied by hand — and a
manual apply is runtime-only, wiped on reboot *and* on every
``pve-sdn-commit`` (which re-runs ``ifreload``).

So the build cannot rely on a one-time manual apply. ``modules/sdn_fdb.py``
makes it repeatable: on every node it intersects the live VMID set with the
estate's SDN NIC map, reads each tap's live MAC, writes a per-node
``/etc/sdn-vlab-fdb/sdn-vlab-fdb.txt``, and installs an ``if-up.d`` hook that
re-applies those ``dst`` entries every time ``vxlan_vlab`` comes up.
Deliberately kept out of ``/etc/pve`` (csync2 replicates that tree
cluster-wide, so a per-node data file there clobbers the others). If a node's
overlay is down after a commit or reboot, that hook is the thing to check.

VM placement
============

One control plane and one worker per node, so a node loss loses at most a
third of each set; the two infra VMs sit on the nodes that do **not** host
``kubecp01`` (the API host's node). This is pinned in the stack config via
``deployment:placementOverrides`` (``infra/pulumi/Pulumi.dev.yaml.example``);
a VM not listed there falls back to best-fit and its choice is written to the
committed placement record (``.placement.json``), which is what makes a
rebuild's placement stable.

.. list-table::
   :header-rows: 1
   :widths: 30 16 54

   * - VMs
     - Node
     - Spec
   * - ``kubecp01``, ``kubewk01``
     - ``pve01``
     - CP: 4 vCPU / 6 GiB; worker: 4 vCPU / 10 GiB
   * - ``kubecp02``, ``kubewk02``
     - ``pve02``
     - as above
   * - ``kubecp03``, ``kubewk03``
     - ``pve03``
     - as above
   * - ``repo01``
     - ``pve02``
     - 4 vCPU / 10 GiB
   * - ``core01``
     - ``pve03``
     - 2 vCPU / 6 GiB

Guest memory headroom
---------------------

The eight VMs allocate **64 GiB** of guest RAM in total. Because they are
pinned one control plane + one worker per host, with the two infra VMs on the
hosts that do **not** hold ``kubecp01``, no node is overcommitted — the
busiest is ``pve02`` at 26 GiB of 62.6 GiB:

.. list-table::
   :header-rows: 1
   :widths: 16 44 40

   * - Host
     - VMs on it
     - Guest RAM
   * - ``pve01``
     - ``kubecp01`` + ``kubewk01``
     - 16 GiB
   * - ``pve02``
     - ``kubecp02`` + ``kubewk02`` + ``repo01``
     - 26 GiB
   * - ``pve03``
     - ``kubecp03`` + ``kubewk03`` + ``core01``
     - 22 GiB

.. warning::

   **Do not enable ballooning or memory reservations on these VMs.** A
   reservation that cannot be satisfied is a VM that will not start.
   **Watch swap on the hosts, not on the guests.** Zero host swap has been
   the health signal through the build so far; if it stops being zero, the
   observability sizing is what will push it there first. A host reporting
   maxed-out memory and swap while its guests were nowhere near their limits
   is a minimum-allocation bug in the backing layer, not a guest problem —
   watch the hypervisor, not the guests.

Per-VM specifications
=====================

All hosts run Ubuntu 24.04. The login user is **``root``**, set by
``deployment:vmUsername`` in the Pulumi stack config and supplied to Ansible
as ``VM_USERNAME``. Every VM is root-only; there is no unprivileged account
anywhere.

.. list-table::
   :header-rows: 1
   :widths: 12 18 8 8 14 32

   * - Host
     - FQDN
     - vCPU
     - RAM
     - Internal IP
     - Role
   * - ``repo01``
     - ``repo01.dev.lo``
     - 4
     - 10 GiB
     - ``192.168.2.99``
     - Gateway, proxy, artifact host, GitLab
   * - ``core01``
     - ``core.dev.lo``
     - 2
     - 6 GiB
     - ``192.168.2.4``
     - FreeIPA identity, DNS, NTP, CA
   * - ``kubecp01``
     - ``kubecp01.dev.lo``
     - 4
     - 6 GiB
     - ``192.168.2.21``
     - RKE2 control plane
   * - ``kubecp02``
     - ``kubecp02.dev.lo``
     - 4
     - 6 GiB
     - ``192.168.2.22``
     - RKE2 control plane
   * - ``kubecp03``
     - ``kubecp03.dev.lo``
     - 4
     - 6 GiB
     - ``192.168.2.23``
     - RKE2 control plane
   * - ``kubewk01``
     - ``kubewk01.dev.lo``
     - 4
     - 10 GiB
     - ``192.168.2.31``
     - RKE2 worker
   * - ``kubewk02``
     - ``kubewk02.dev.lo``
     - 4
     - 10 GiB
     - ``192.168.2.32``
     - RKE2 worker
   * - ``kubewk03``
     - ``kubewk03.dev.lo``
     - 4
     - 10 GiB
     - ``192.168.2.33``
     - RKE2 worker

Disk layout, per host: 32 GB OS disk, plus 100 GB mounted at ``/data1`` for
apps and artifacts. The workers additionally carry a third 100 GB disk, ext4,
mounted at ``/var/lib/longhorn`` for the CSI disks.

Notes on the sizes that are load-bearing:

- **``repo01`` is 10 GiB.** GitLab is the binding constraint on this host;
  8 GiB was the measured floor that runs it beside Apache, apt-cacher-ng,
  dnsmasq and the tunnel without swapping, and 10 GiB is that floor with
  headroom. The VM is created at this size — do not build it small and grow
  it later.
- **The control plane is 4 vCPU.** 2 vCPU starves etcd commit latency under
  read bursts. Because storage is now NVMe, the remaining etcd tuning is
  burst safety, not latency hiding — heartbeat 1000 ms / election timeout
  5000 ms in ``ansible/inventory/group_vars/kubecp/``, so a slow commit is
  not mistaken for a dead leader.

VM CPU model: ``host``
======================

The Pulumi VM factory (``modules/vm_factory.py``) presents ``cpu_type:
"host"`` to every guest, deliberately. The original default was a
feature-limited model chosen so a guest stayed migratable to any future host,
but it was changed because the lab's guests run JVMs — FreeIPA's PKI Tomcat
and friends, on OpenJDK — and that JIT **hard-crashes** (SIGSEGV in
``StringTable`` during the post-config PKI restart) when the presented CPU is
feature-limited to v2 (no AVX). A working CA was worth more than a
theoretical cross-host migration property.

.. important::

   **Changing ``cpu_type`` on an existing VM needs a full power cycle.** A
   reboot from inside the guest keeps the running QEMU process and the CPU it
   is presenting. Plan a cold-boot window for it.

Credentials
===========

- **Proxmox API**: user ``root@pam``, password in ``~/.proxmoxpass``
  (never in git). The stack config uses ``proxmox:insecure: true`` for the
  self-signed API cert.
- **Lab VMs**: log in as ``root``; the VM password is supplied to Ansible as
  ``VM_USER_PASSWORD`` from ``env.sh``. No SSH keys are installed on the lab
  VMs — automation authenticates with the password, via
  ``sshpass -f ~/.proxmoxpass`` for the PVE nodes themselves.

Boot image pinning
==================

The Ubuntu 24.04 cloud image the templates import from is **pinned to a dated
release directory and verified against its published SHA256** in
``infra/pulumi/__main__.py``. Changing the URL means changing the checksum
beside it; the two are deliberately coupled so a half-finished edit fails the
download rather than silently widening what is accepted.

Do not benchmark all guests at once
===================================

A synchronous-write ``dd`` benchmark run against every node at once issues
far more sustained fsync than the storage tier can carry, and the whole
hypervisor has frozen under exactly that load. Measure one guest, once, and
keep it off a live quorum.

Automation notes
================

- Pulumi + Python is the IaC interface; the ``pulumi_proxmoxve`` package
  tracks the bpg provider. Provider behaviour changes with upstream releases
  — pin versions and read release notes before upgrading.
- Any provider operation that needs node-scoped access (SSH, template
  import, the ``pve_cleanup`` orphan-LV sweep) goes through
  ``deployment:managementNode`` (``pve01``); any node's API endpoint reaches
  the cluster for most reads.
- VMs are **only** created by Pulumi; host software and configuration is
  Ansible's. Do not hand-create or hand-edit VMs in the PVE UI — the next
  ``pulumi up`` will reconcile against the program.
