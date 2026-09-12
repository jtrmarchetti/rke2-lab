# Targets

VM specifications for the Proxmox environment. These values are the source of truth
for `infra/pulumi/modules/vm_definitions.py`; the two must stay in sync.

All hosts run Ubuntu 24.04. The login user is **`root`**, set by
`deployment:vmUsername` in the Pulumi stack config and supplied to Ansible as
`VM_USERNAME`.

This document previously said `devops` throughout, and no host has ever had such
a user — every VM is root-only, confirmed against all eight. Nothing depends on
an unprivileged user today, but see the note at the end of this file: it is
worth deciding whether that is the intent or an accident.

## Summary

| Host | FQDN | Phase | vCPU | RAM | Internal IP | Role |
| --- | --- | --- | --- | --- | --- | --- |
| `repo01` | `repo01.dev.lo` | 1 | 4 | 10 GiB | `192.168.2.99` | Gateway, proxy, artifact host, GitLab |
| `core01` | `core.dev.lo` | 2 | 2 | 6 GiB | `192.168.2.4` | FreeIPA identity, DNS, NTP, CA |
| `kubecp01` | `kubecp01.dev.lo` | 4 | 4 | 6 GiB | `192.168.2.21` | RKE2 control plane |
| `kubecp02` | `kubecp02.dev.lo` | 4 | 4 | 6 GiB | `192.168.2.22` | RKE2 control plane |
| `kubecp03` | `kubecp03.dev.lo` | 4 | 4 | 6 GiB | `192.168.2.23` | RKE2 control plane |
| `kubewk01` | `kubewk01.dev.lo` | 5 | 4 | 10 GiB | `192.168.2.31` | RKE2 worker |
| `kubewk02` | `kubewk02.dev.lo` | 5 | 4 | 10 GiB | `192.168.2.32` | RKE2 worker |
| `kubewk03` | `kubewk03.dev.lo` | 5 | 4 | 10 GiB | `192.168.2.33` | RKE2 worker |

### Memory total, and the headroom this leaves

The eight VMs allocate **64 GiB** of guest RAM in total. Because they are
pinned one control plane + one worker per host, with the two infra VMs on the
hosts that do **not** hold `kubecp01`, no node is overcommitted — the busiest
is `pve02` at 26 GiB of 62.6 GiB:

| Host | VMs on it | Guest RAM |
| --- | --- | --- |
| `pve01` | `kubecp01` + `kubewk01` | 16 GiB |
| `pve02` | `kubecp02` + `kubewk02` + `repo01` | 26 GiB |
| `pve03` | `kubecp03` + `kubewk03` + `core01` | 22 GiB |

That replaces the first draft's single 62.8 GiB hypervisor, where 64 GiB of
guests was more than the host physically had and survived only because Proxmox
hands memory out on demand.

The memory-misconfiguration lesson from that era stands on its own: a host
reporting maxed-out memory and swap while its guests were nowhere near their
limits was a minimum-allocation bug in the backing layer, not a guest problem.
Watch the hypervisor, not the guests.

Two consequences worth stating rather than discovering:

- **Do not enable ballooning or memory reservations on these VMs.** A
  reservation that cannot be satisfied is a VM that will not start.
- **Watch swap on the hosts, not on the guests.** Zero host swap has been the
  health signal through the build so far; if it stops being zero, the
  observability sizing is what will push it there first.

## DNS

`core01` is the only resolver on the internal network, and it has no forwarders — it
answers for `dev.lo` and nothing else, because there is no upstream resolver to
forward to.

| Host | Resolver | Why |
| --- | --- | --- |
| `repo01` | `192.168.1.1`, plus `192.168.2.4` for `dev.lo` only | Needs upstream names to download artifacts, and `dev.lo` names for GitLab. Split by a netplan drop-in on the internal link, since a resolver that answers NXDOMAIN is treated as authoritative and a second entry would never be consulted. |
| `core01` | `127.0.0.1` | Runs FreeIPA; resolves for itself. |
| Cluster nodes | `192.168.2.4` | `dev.lo` authority. |

---

## repo01

The only dual-homed host, and the only host with internet access.

- **hostname:** `repo01.dev.lo`
- **username:** `root`
- **cpu:** 4
- **ram:** 10 GiB — sized for GitLab, which is the binding constraint on this
  host. GitLab's published requirements are far higher; 8 GiB was the measured
  floor that runs it beside Apache, apt-cacher-ng, dnsmasq, and the tunnel
  without swapping, and the memory-constrained omnibus settings are applied on
  top of it. 10 GiB is that floor with headroom.

  **The VM is created at this size.** An earlier draft sized the GitLab host
  small and resized the running host after the undersizing was discovered;
  that is history, not a rebuild step. A host built from `vm_definitions.py`
  today comes up at 10 GiB before GitLab is ever installed. Do not build this
  host small and grow it later.
- **storage:**
  - 32 GB — OS
  - 100 GB — apps and artifacts, mounted at `/data1`
- **nics:**
  - `192.168.1.20/24` — gateway `192.168.1.1`, dns `192.168.1.1` — external network / internet
  - `192.168.2.99/24` — no gateway — internal network; `dev.lo` queries split to `192.168.2.4`
- **roles:**
  - WireGuard tunnel gateway for controller access to `192.168.2.0/24`
  - SOCKS5 proxy for web access from external into the internal environment
  - APT caching proxy for internal hosts; caches on demand, never mirrors a
    repository; deliberately not an internet default route
  - Apache artifact host serving `/data1/artifacts` over HTTP to internal nodes
  - Staging point for every artifact in every phase, downloaded and checksummed here
    first
  - GitLab server for container image, package, and raw artifact hosting

## core01

- **hostname:** `core.dev.lo`
- **username:** `root`
- **cpu:** 2
- **ram:** 6 GiB
- **storage:**
  - 32 GB — OS
  - 100 GB — apps, mounted at `/data1`
- **nics:**
  - `192.168.2.4/24` — gateway `192.168.2.99`, dns `127.0.0.1` — internal network
- **roles:**
  - FreeIPA server for the `dev.lo` domain, providing LDAP, NTP, DNS, and the
    certificate authority

## kubecp01

- **hostname:** `kubecp01.dev.lo`
- **username:** `root`
- **cpu:** 4
- **ram:** 6 GiB
- **storage:**
  - 32 GB — OS
  - 100 GB — apps, mounted at `/data1`
- **nics:**
  - `192.168.2.21/24` — gateway `192.168.2.99`, dns `192.168.2.4` — internal network
- **role:** RKE2 control plane

## kubecp02

- **hostname:** `kubecp02.dev.lo`
- **username:** `root`
- **cpu:** 4
- **ram:** 6 GiB
- **storage:**
  - 32 GB — OS
  - 100 GB — apps, mounted at `/data1`
- **nics:**
  - `192.168.2.22/24` — gateway `192.168.2.99`, dns `192.168.2.4` — internal network
- **role:** RKE2 control plane

## kubecp03

- **hostname:** `kubecp03.dev.lo`
- **username:** `root`
- **cpu:** 4
- **ram:** 6 GiB
- **storage:**
  - 32 GB — OS
  - 100 GB — apps, mounted at `/data1`
- **nics:**
  - `192.168.2.23/24` — gateway `192.168.2.99`, dns `192.168.2.4` — internal network
- **role:** RKE2 control plane

## kubewk01

- **hostname:** `kubewk01.dev.lo`
- **username:** `root`
- **cpu:** 4
- **ram:** 10 GiB
- **storage:**
  - 32 GB — OS
  - 100 GB — apps, mounted at `/data1`
  - 100 GB — CSI, ext4, mounted at `/var/lib/longhorn`
- **nics:**
  - `192.168.2.31/24` — gateway `192.168.2.99`, dns `192.168.2.4` — internal network
- **role:** RKE2 worker

## kubewk02

- **hostname:** `kubewk02.dev.lo`
- **username:** `root`
- **cpu:** 4
- **ram:** 10 GiB
- **storage:**
  - 32 GB — OS
  - 100 GB — apps, mounted at `/data1`
  - 100 GB — CSI, ext4, mounted at `/var/lib/longhorn`
- **nics:**
  - `192.168.2.32/24` — gateway `192.168.2.99`, dns `192.168.2.4` — internal network
- **role:** RKE2 worker

## kubewk03

- **hostname:** `kubewk03.dev.lo`
- **username:** `root`
- **cpu:** 4
- **ram:** 10 GiB
- **storage:**
  - 32 GB — OS
  - 100 GB — apps, mounted at `/data1`
  - 100 GB — CSI, ext4, mounted at `/var/lib/longhorn`
- **nics:**
  - `192.168.2.33/24` — gateway `192.168.2.99`, dns `192.168.2.4` — internal network
- **role:** RKE2 worker

---

## Open: there is no unprivileged user

Every VM is root-only. Automation connects as `root`, services run as root or as
whatever user their container declares, and no host carries the `devops` account
this document described when it first wrote.

That is worth a decision rather than a correction, and it is deliberately not
made here:

- Nothing currently needs it. Ansible connects as `root` by design, and the
  cluster's own components run under their own accounts inside containers.
- In a regulated environment it is normally an audit finding — administrative
  access with no named account behind it and no separation between logging in
  and being able to change anything.

If an unprivileged user is wanted, it belongs in the Pulumi cloud-init user
definition and in `base_host`, so it exists from first boot rather than being
added to eight running machines afterwards.
