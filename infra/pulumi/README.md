# Pulumi Proxmox VM Deployment

This stack handles VM creation for all planned phases using a shared VM module.
Default behavior deploys only Phase 1 VMs (repo01).

## 1. Prerequisites

- A Proxmox node reachable over its API, and credentials for it
- An SSH public key for cloud-init
- The Pulumi CLI and this project's virtual environment

The last one is not a manual step any more, and neither is anything else on the
controller. `bootstrap/controller-bootstrap.sh` followed by
`ansible-playbook playbooks/controller_bootstrap.yml` installs the pinned CLI
into `~/.pulumi/bin` and builds `infra/pulumi/.venv` from
`requirements.txt` — see `spec/CONTROLLER.md`.

**No prepared Ubuntu template is required.** If `deployment:templateVmId` is
unset, the stack downloads the Ubuntu 24.04 cloud image and imports it for the
boot disk itself, which is what makes a rebuild from nothing possible. Setting
a template ID is the optimization, not the prerequisite.

## 2. Setup

```bash
source ~/.venvs/rke2lab/bin/activate   # or the project venv, for pulumi itself
source ~/.config/rke2lab/env.sh        # PULUMI_CONFIG_PASSPHRASE and PROXMOX_VE_*
cd infra/pulumi
pulumi stack init dev                  # first time only; `pulumi stack select dev` after
```

## 3. Configuration

Copy values from Pulumi.dev.yaml.example into stack config. Secrets come from
the environment, and there is exactly one place they come from:

```bash
source ~/.config/rke2lab/env.sh
```

That file holds `PROXMOX_VE_ENDPOINT`, `PROXMOX_VE_USERNAME`,
`PROXMOX_VE_PASSWORD`, `PROXMOX_VE_INSECURE`, `VM_SSH_PUBLIC_KEY` and
`PULUMI_CONFIG_PASSPHRASE`. `bootstrap/env.sh.example` is the template with
every name and no values; `spec/SECRETS.md` is the inventory. Do not put
credentials in a shell history or a file beside this one.

To set password login credentials via cloud-init (optional):

```bash
pulumi config set deployment:vmUsername root
pulumi config set --secret deployment:vmUserPassword '<your-root-password>'
```

Note: in SSH public keys, the trailing email is only a key comment label.
It is not used as the login username.

Minimum required stack config:

```bash
pulumi config set deployment:templateNodeName proxmox-rke2
```

Template usage is optional:

```bash
# If you already have a prepared template
pulumi config set deployment:templateVmId 9000
```

If deployment:templateVmId is not set, the stack will download Ubuntu 24.04 cloud image
and import it for the VM boot disk automatically.

When using this no-template mode, set a file-based datastore for the image download:

```bash
pulumi config set deployment:imageDatastoreId local
```

If your bridge names differ from defaults:

```bash
pulumi config set deployment:externalBridge vmbr0
pulumi config set deployment:internalBridge vmbr1
```

By default, the stack enforces separate bridges for external and internal networks
to match the target design. If you temporarily need single-bridge bootstrap:

```bash
pulumi config set deployment:allowSharedBridge true
```

Remove this override once the internal-only bridge exists.

Internal bridge lifecycle is managed by Pulumi by default:

```bash
pulumi config set deployment:manageInternalBridge true
pulumi config set deployment:internalBridge vmbr1
```

For an isolated internal bridge, leave `internalBridgePorts` empty. If you need
to attach physical NICs or other ports, set the list explicitly.

## 4. Local automation bootstrap for future sessions

Keep project secrets outside the repository and source them from a local shell config that is not committed. This project expects the Pulumi passphrase to exist in the user environment before running a stack command.

```bash
cd infra/pulumi
python3 -m venv .venv
source .venv/bin/activate
export PATH="$HOME/.pulumi/bin:$PATH"
source "$HOME/.config/rke2lab/env.sh"

pulumi stack ls
pulumi config get deployment:phaseLimit
pulumi preview --non-interactive
```

The environment file is intentionally not stored in the repo. It lives at
`~/.config/rke2lab/env.sh` (mode 0600) and holds every secret this project needs —
Proxmox credentials, the Pulumi passphrase, VM access, WireGuard private keys, and
service passwords. See `spec/SECRETS.md` for the full inventory and rotation steps.

If you need to restore it in a fresh shell, add this to your local bash profile:

```bash
source "$HOME/.config/rke2lab/env.sh"
```

`~/.config/proxmox-lab.env` still works; it is now a shim that sources the file above.

## 5. Deploy only Phase 1 VM(s)

```bash
pulumi config set deployment:phaseLimit 1
pulumi preview
pulumi up
```

## 6. Deploy additional phases later

```bash
pulumi config set deployment:phaseLimit 2  # adds core01
# 3 = GitLab on repo01 (no new VM), 4 = control plane, 5 = workers
pulumi up
```

or explicit VM selection:

```bash
pulumi config set --path 'deployment:selectedVmKeys[0]' repo01
pulumi config set --path 'deployment:selectedVmKeys[1]' core01
pulumi up
```

## SDN vxlan overlay: durable remote-FDB `dst` bindings

The estate's internal network is a PVE SDN vxlan zone (`labvx`) whose vnet
bridge is `vlab` / `vxlan_vlab`. PVE 9.2 materializes that bridge from the
generated `/etc/network/interfaces.d/sdn` but has **no runtime daemon** that
programs the kernel's remote-FDB `dst` bindings from the `vxlan_remoteip`
lines. Without a static `bridge fdb append <mac> dev vxlan_vlab dst
<peer-underlay>` entry for each remote VM MAC, cross-node encapsulation is
impossible and the overlay is dead even though both ends' bridges are UP.

`modules/sdn_fdb.py` makes this durable and repeatable from scratch:

- For each PVE node it reads each SDN VM's **PVE-assigned MAC** from PVE's
  own config API (`GET /nodes/<n>/qemu/<vmid>/config` → `net<idx> virtio=`).
  That is the MAC a cold-booted VM carries, not the transient live-tap MAC
  that drifts after a VM is recreated — so a fresh build routes correctly.
- It writes a per-node data file to `/etc/sdn-vlab-fdb/sdn-vlab-fdb.txt`
  (one `<mac> <peer-underlay>` line per remote MAC, plus the VRRP +
  broadcast `dst` lines) and installs `/etc/network/if-up.d/sdn-vlab-fdb`,
  an `if-up` hook that re-applies the file on every bring-up of the vxlan.
  The hook is what heals the FDB after a reboot or a `pve-sdn-commit`.

Two gotchas encoded in the module:

- **The data file must not live in `/etc/pve`.** That tree is
  cluster-replicated by csync2 on PVE, so a per-node file written there is
  clobbered to the last node's content cluster-wide. `/etc/sdn-vlab-fdb/` is
  node-local, which is why the data file goes there.
- **Verify reachability with ICMP, not TCP/22.** The RKE2/Ubuntu nodes do
  not expose SSH on the internal overlay; ping an internal IP from an
  overlay member (or dump `bridge fdb show dev vxlan_vlab` on a peer) to
  confirm the `dst` entries are present.

The wiring in `__main__.py` defers both imperative writes — the cluster-wide
`apply_sdn` reload and `ensure_sdn_fdb` — into a post-creation callback over
`pulumi.Output.all(sdn_vnet.id, *[v.id for v in vm_resources]).apply(...)`.
On preview/dry-run the callback still runs but both writers self-gate on
`is_dry_run()`, so the no-op holds. Registering them at resource-creation
time would
make a first-from-scratch up reload an empty SDN config and install FDB
files with no VM MAC lines; the callback runs only once the zone, vnet, and
VMs all exist, so the reload sees the real objects and MAC discovery finds
the PVE-assigned MACs each VM carries at create time. There is also a
standalone entry point for one-off re-application:

```bash
cd infra/pulumi && source .venv/bin/activate && source ~/.config/rke2lab/env.sh
python3 -m modules.sdn_fdb        # discover + log only
python3 -m modules.sdn_fdb --apply  # install the hook and fire the FDB
```

Proven end to end on the 3-node cluster (2026-09-09): a `pulumi destroy` down
to zero VMs followed by a cold `pulumi up` installed the FDB data files and
kernel `dst … self permanent` entries on every node in the first pass (log:
`sdn-fdb: discovered MACs pve01=2, pve02=3, pve03=3` → `pve01: 10 dst
line(s); FDB entries now 32`, etc.) with no manual `bridge fdb append` step,
and the entries survived the subsequent full `site.yml` build and its
idempotent re-run.

## Notes

- VM specs are defined in modules/vm_definitions.py from current spec/TARGETS values.
- Bridge names are environment-specific and must match your Proxmox node configuration.
- The image bootstrap uses the non-legacy download resource (`proxmox_download_file` path).
- Static IPs are assigned through cloud-init initialization.
