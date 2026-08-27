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
`requirements.txt` — see `plan/CONTROLLER.md`.

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
every name and no values; `plan/SECRETS.md` is the inventory. Do not put
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
service passwords. See `plan/SECRETS.md` for the full inventory and rotation steps.

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

## Notes

- VM specs are defined in modules/vm_definitions.py from current plan/TARGETS values.
- Bridge names are environment-specific and must match your Proxmox node configuration.
- The image bootstrap uses the non-legacy download resource (`proxmox_download_file` path).
- Static IPs are assigned through cloud-init initialization.
