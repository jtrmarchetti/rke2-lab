# The Automation Controller

`OVERVIEW.md` states that every controller dependency must be documented and
scripted, and then concedes that Pulumi, Ansible, their virtual environments,
and the WireGuard tunnel "remain the unpaid half of this rule". This document
pays it.

The gap it closes is specific and worth naming plainly. The plan can rebuild
the entire Proxmox environment — but only from a controller that was set up by
hand and whose setup existed nowhere. **Lose the controller and you lose the
ability to rebuild anything else.** Every other host in the lab is described by
Pulumi and Ansible; the host that runs Pulumi and Ansible was described by
nothing.

Written in Phase 6a, because 6a is the phase that doubled the artifact count
and made the omission expensive rather than merely untidy.

## What the controller is

`controller01` in the inventory, reached as `ansible_connection: local` — it is
the workstation the automation runs from, not a managed VM. It sits on
`192.168.1.0/24` and reaches the lab's internal `192.168.2.0/24` network
through a WireGuard tunnel to `repo01`. It holds every secret in the
environment.

It is deliberately outside the FIPS boundary and outside the domain. Nothing in
the cluster depends on it at run time: the cluster reconciles from GitLab
whether the controller is up or not. What depends on it is *change* — building,
publishing, and sealing.

## Dependency manifest

Versions are what is installed and working as of 2026-08-15. Pinning here means
"a rebuild should install this, and a different version is a decision, not an
accident".

**Since 2026-08-16 this table is a record rather than the source.** Each pinned
version is declared in a file automation reads, and upgrading is editing one of
those, not this document. A manifest a human maintains beside the thing it
describes is a manifest that drifts; this one now describes files that cannot.

**Since 2026-08-17 there is one place to start reading, and it is not here.**
`ansible/inventory/group_vars/controller/artifacts.yml` is the controller's own
artifact manifest — the counterpart to `group_vars/repo/artifacts.yml`, which
has covered the machines being *built* since Phase 1 while the machine doing
the building had nothing. Every download this host makes has an entry: the four
that Ansible installs carry the version, URL and checksum the roles consume, and
the rest are index rows naming the file their pin actually lives in
(`bootstrap/requirements-controller.txt`, `ansible/requirements.yml`,
`infra/pulumi/requirements.txt`, `infra/pulumi/__main__.py`, and the role
defaults that list apt packages).

The gap that closed was never that the controller's downloads were unpinned —
they were pinned and checksummed already. It was that they were pinned in five
places, so the only way to answer "what does this host pull from the internet"
was to read every role, and two of the pins were split across files: the Pulumi
URL was in `group_vars` while its checksum was in the role's defaults, and the
same for k9s, Flux and kubeseal. A version bump could change the URL and leave
the checksum behind, which fails as a corrupted download rather than as the
half-finished edit it is. Version, URL and checksum are now one entry, the role
defaults are `null`, and both roles assert that a caller supplied all three.

| Dependency | Version | Source | Why it is here |
| --- | --- | --- | --- |
| Ubuntu | 24.04.4 LTS | OS install | Base |
| Ubuntu cloud image | 24.04, `release-20260814` | `cloud-images.ubuntu.com`, checksummed, by Pulumi | The disk every VM in the lab is imported from. Not on this table until 2026-08-17, and not verified either: it was fetched from `noble/current/` — a moving target — with no checksum, which made it the one unpinned artifact in an environment whose whole premise is pinned artifacts |
| Python | system `python3` | `apt python3-venv`, by `bootstrap/` | Runs Ansible and Pulumi |
| Ansible | 13.4.0 | `bootstrap/requirements-controller.txt`, into `~/.venvs/rke2lab` | Configuration management |
| ansible-core | 2.20.3 | dependency of the above | — |
| ansible-lint | 26.3.0 | same requirements file | `ANSIBLE_STANDARDS.md` enforcement |
| ansible-compat | 26.3.0 | dependency of ansible-lint | — |
| `community.docker` | 5.0.6 | `ansible/requirements.yml` | Image staging, compose services |
| `ansible.posix` | 2.1.0 | `ansible/requirements.yml` | `mount`, for `/data1` |
| `community.general` | 12.4.0 | `ansible/requirements.yml` | `filesystem`, `apache2_module`, and `ansible_galaxy_install`, which reconciles this list |
| dnsmasq | distribution | `apt`, by the `split_dns` role | Resolves `dev.lo` here and everything else upstream |
| Pulumi CLI | 3.256.0 | `get.pulumi.com`, checksummed, by `controller_runtime` | Proxmox VM lifecycle |
| `pulumi` (Python) | 3.256.0 | `infra/pulumi/.venv` | — |
| `pulumi_proxmoxve` | 8.3.0 | `infra/pulumi/.venv` | The Proxmox provider |
| `proxmoxve` plugin | 8.3.0 | Pulumi plugin cache, 115 MB | Downloaded by the CLI |
| WireGuard tools | 1.0.20210914 | `apt`, by `controller_runtime` | The tunnel to `repo01` |
| git | 2.43.0 | `apt`, by `controller_runtime` | This repository, and cluster-state |
| kubectl | v1.35.7+rke2r1 | copied from `kubecp01` by `kube_cli_controller` | Exactly the cluster's version |
| k9s | v0.51.0 | GitHub release, checksummed | Operator convenience only |
| Flux CLI | 2.9.4 | **`repo01` Apache**, checksummed | Operator convenience: `flux check`, `flux reconcile`. **No longer bootstraps anything** — see below |
| kubeseal | 0.38.4 | **`repo01` Apache**, checksummed | Produces every SealedSecret |
| `community.crypto` | 3.1.1 | `ansible/requirements.yml` | Phase 6b: the intermediate CA's key and CSR |
| skopeo | 1.13.3 | `apt`, **on repo01** | Copies images registry to registry, replacing the broken `docker save` path |
| helm | 3.21.4 | **`repo01` Apache**, checksummed | Pushes charts into the registry as OCI artifacts |

### Why two of these come from `repo01` and one does not

k9s is downloaded from GitHub. The Flux CLI and kubeseal are not — they are
artifact-manifest entries with `retention: bootstrap`, staged on `repo01` and
fetched from Apache like anything else a rebuild needs.

The line between them is whether the rebuild path runs through the tool. k9s is
a terminal UI for looking at a cluster; if it is missing, someone types
`kubectl` instead. kubeseal is the only way to produce a SealedSecret, and the
`gitops_source` role runs it on every pass to seal the OpenBao unseal keys, the
cluster's intermediate CA, and every service credential — which puts it
squarely in the rebuild path and in the vault's recovery path.

**The Flux CLI is the odd one out, as of 2026-08-16.** It no longer bootstraps
anything: `gitops_bootstrap` installs the vendored component set with `kubectl`
and applies the `GitRepository`/`Kustomization` pair that `gitops_source`
rendered. The CLI is now what k9s is — a convenience for looking at and
prodding a running cluster. It stays on Apache rather than moving to GitHub
because it is already in the manifest and moving it would buy nothing, but its
justification has changed and the table above says so.

What replaced it is worth stating plainly. `flux bootstrap` writes Flux's own
manifests into the cluster-state repository, which made the CLI the author of
the one directory the whole reconcile hangs from, and made that directory's
content depend on which CLI version happened to be installed here. Ansible owns
it now, so there is no file in GitLab that this repository does not render.

### Three things that were not pinned, and now are

All three were recorded here as debts on 2026-08-15 and paid on 2026-08-16 by
`bootstrap/` and the `controller_runtime` role. They are kept in this document
rather than deleted, because each one names a failure that the arrangement
replacing it is built to prevent.

**Ansible was `pip`-installed into Homebrew's Python**, not into a virtual
environment — how it ended up here rather than a decision, and the most fragile
thing on the list: a `brew upgrade` moving `python@3.14` to `python@3.15` takes
Ansible with it. It now lives in `~/.venvs/rke2lab`, created from
`bootstrap/requirements-controller.txt` and reconciled from the same file by
`controller_runtime` on every run. The role asserts that the Ansible executing
it is the one in that environment, because a controller that pins one
environment while running from another has a perfect manifest and an unmanaged
runtime.

**`requirements.yml` floored its collection versions** with `>=`. It pins with
`==` now. A floor records what was tested against nothing in particular: the
version a rebuild installs is then whichever Galaxy published most recently,
which is the one version nobody has ever run this repository against.

**The collections were installed twice, and one of them was undeclared.**
`community.docker`, `ansible.posix`, `community.general` and `kubernetes.core`
each existed under `~/.ansible/collections` and again inside Homebrew's
`site-packages`, with only the first ever loaded — identical versions, so
harmless and invisible at the same time, which is the condition under which one
of them eventually gets upgraded alone. Both the bootstrap script and the role
install into one declared path, and `controller-env.sh` exports
`ANSIBLE_COLLECTIONS_PATH` so the copy Ansible loads is the copy
`requirements.yml` pins.

`kubernetes.core` 6.3.0 was the odd one out: installed, **not** in
`requirements.yml`, and imported by nothing under `ansible/`. It was put there
by hand for something that never landed, and the resolution is that it stays
out. A rebuilt controller will not have it; a phase that wants it declares it
first.

**`controller_setup.sh` did not set up the controller.** Despite the name it
configured dnsmasq and nothing else — no Python, no Ansible, no Pulumi, no
WireGuard, no CLI tooling. It is gone. The split DNS it configured is now the
`split_dns` role driven from `group_vars/controller`, the same role and the
same shape `repo01` has used since Phase 1, and two things in the script that
were wrong rather than merely unautomated went with it: a `srv.local` search
domain that names nothing in this lab, and `expand-hosts` for an `/etc/hosts`
file this controller does not keep.

What replaced it is `bootstrap/controller-bootstrap.sh`, whose scope is one
chicken-and-egg problem and nothing else: **Ansible cannot install Ansible.**
It installs the system packages, creates the virtual environment from the
pinned requirements, and installs the collections. Everything after that is
`playbooks/controller_bootstrap.yml`.

## Cold start, from a bare Ubuntu host

Four commands, and the order between them is the whole content of this section.

```bash
git clone <this repository> && cd code

# 1. The one hand-run step. Ansible cannot install Ansible.
./bootstrap/controller-bootstrap.sh

# 2. Secrets. Restore ~/.config/rke2lab/ from backup, or start from the
#    template the script points at. Nothing below this line runs without it.
source ~/.config/rke2lab/env.sh

# 3. The controller itself: split DNS, the pinned runtimes, Pulumi, the tunnel.
source ~/.venvs/rke2lab/bin/activate
cd ansible && ansible-playbook playbooks/controller_bootstrap.yml

# 4. Everything else. Pulumi builds the VMs, then site.yml builds the lab.
#    site.yml imports step 3 as its first play, so a rebuild that starts here
#    is also correct.
ansible-playbook playbooks/site.yml
```

What each step owns, and the two places the order is load-bearing:

1. **`bootstrap/controller-bootstrap.sh`** installs the system packages,
   creates `~/.venvs/rke2lab` from `bootstrap/requirements-controller.txt`, and
   installs the pinned collections into `~/.ansible/collections`. It is
   idempotent, and re-running it is how a version change in that file is
   applied. Its scope is deliberately one problem: everything it does is
   something that must exist before an `ansible-playbook` command is possible.
2. **Secrets** restore `~/.config/rke2lab/` — `env.sh` at mode 0600, with
   `sealed-secrets-key.yaml` and `k8s-ca/` beside it. `bootstrap/env.sh.example`
   is the full set of names with no values, for the case where there is no
   backup to restore. See `SECRETS.md`.
3. **`playbooks/controller_bootstrap.yml`** does the rest, in this order:
   - **Split DNS**, via the `split_dns` role and `group_vars/controller`.
     **Nothing that resolves a `dev.lo` name works before this**, which is most
     of what follows. Zone verification is off here and on for `repo01`: on a
     cold rebuild this play runs before `core01` exists to answer, and a check
     that cannot pass on a rebuild's first run is a check that gets disabled
     during rebuilds. `controller_trust` proves the same thing later by
     fetching `https://gitlab.dev.lo` by name.
   - **The runtime**, via `controller_runtime`: system packages, the virtual
     environment reconciled against its pins, the collections into one path,
     the Pulumi CLI into `~/.pulumi/bin` — pinned and checksummed — and
     `infra/pulumi/.venv` from `infra/pulumi/requirements.txt`. The provider
     plugin downloads itself on first use, which is the only path Pulumi
     supports.
   - **The tunnel**, last. WireGuard to `repo01`, `10.66.66.2/30` on this end,
     via the `controller_tunnel` role. Ordering trap: the tunnel is what
     reaches `192.168.2.0/24`, so it precedes every Ansible run against an
     internal host — but the play that configures it reaches `repo01` on
     `192.168.1.20`, so configuring the tunnel never depends on the tunnel.

     Three plays, not one, and the split exists so the result can be checked:
     this end is written first, then the gateway end, then this end again to
     prove a handshake completed and that an address inside
     `192.168.2.0/24` answers through it. `PROXMOX.md` asked for that check in
     Phase 1 and nothing implemented it until 2026-08-17. What it catches is a
     gateway that is up but not forwarding, which otherwise surfaces as every
     later playbook timing out against an internal host with an error that
     names the host and says nothing about the path to it.

     Neither peer's **public** key is in inventory any more. Both are derived
     from the private keys in `env.sh` with `wg pubkey`, and the controller
     hands its own to the gateway play. Each used to be a literal pasted into
     the other host's `group_vars` — see `SECRETS.md` for why that failure mode
     was worth engineering away rather than documenting.
4. **`playbooks/site.yml`** builds the environment, and imports step 3 as its
   own first play.

Two things are *not* in this list, because they cannot be: **SSH host keys**
and **the FreeIPA CA**. Both are handled by `controller_trust` inside
`playbooks/controller.yml`, which takes `kubectl` from a cluster node and so
cannot run until a cluster exists. That makes it the last step of a full
rebuild rather than part of the controller's own bring-up — see below.

Two files under `~/.config/rke2lab/` are inputs to step 9 and beyond rather
than outputs of it, and a rebuild that restores only `env.sh` will get as far
as the GitOps push and stop:

- `sealed-secrets-key.yaml` — every SealedSecret in the cluster-state
  repository is sealed against it, and `gitops_bootstrap` restores it into the
  cluster before Flux can deploy a controller that would generate its own.
- `k8s-ca/` — `ipa_sub_ca` regenerates this if it is absent, but regenerating
  means a new intermediate and a re-issue of every certificate in the cluster.
  Restoring it is cheaper and is what the backup is for.

See `SECRETS.md`.

## Two pieces of trust state that are not in any playbook

Both were found by inspection rather than by anything failing loudly, and
neither belongs to a role today.

**SSH host keys have drifted.** `repo01`'s host key changed since it was last
accepted, and `kubewk02` and `kubewk03` were never added at all. Ansible does
not care — `ansible.cfg` sets `host_key_checking = False` — so this is
invisible to automation and blocks a human running plain `ssh` against three of
the eight hosts. On a rebuild every key is new and the whole set needs
re-accepting.

That `host_key_checking = False` is worth stating for what it is: the controller
does not authenticate the hosts it configures, on a network reachable through a
tunnel it also configures. Acceptable in a lab where Proxmox can read every
guest disk anyway; not a setting to carry anywhere else.

**The FreeIPA CA is not in the controller's trust store.** There is no
`/usr/local/share/ca-certificates/dev.lo-ca.crt` here, so `git clone` against
`https://gitlab.dev.lo` fails with `server certificate verification failed`
until `GIT_SSL_CAINFO` is passed by hand. Flux is unaffected — its
`GitRepository` carries the CA in the `flux-system` Secret, placed there at
bootstrap — which is exactly why this went unnoticed: the cluster trusts the CA,
and the machine that publishes to the repository does not.

The CA is already published at `http://192.168.2.99/gitlab/dev.lo-ca.crt` for
the cluster nodes. Installing it here is two commands, and it belongs in
`kube_cli_controller` or a controller role of its own:

```bash
sudo curl -fsSL http://192.168.2.99/gitlab/dev.lo-ca.crt \
  -o /usr/local/share/ca-certificates/dev.lo-ca.crt
sudo update-ca-certificates
```

## Both of those are now paid

The `controller_trust` role does them, and `playbooks/controller.yml` runs it
first. It installs the domain CA into the system trust store, proves a
certificate the domain signed verifies afterwards rather than assuming it, and
records every managed host's SSH key from inventory — so a host added to the
environment is added to `known_hosts` by the same change that creates it.

Installing the CA found a third thing, which is the kind of fault that only
shows up when something finally depends on it:

**Homebrew's Python does not read the system trust store.** `update-ca-certificates`
writes `/etc/ssl/certs/ca-certificates.crt`, and `curl` and `git` picked up the
domain CA immediately. Ansible did not, because it runs on Homebrew's Python,
which verifies against `$(brew --prefix)/etc/openssl@3/cert.pem` instead. Every
`uri` task against an internal HTTPS endpoint therefore needs `ca_path` set
explicitly, and both roles that make one — `controller_trust` and
`openbao_config` — do. This is the same fragility the dependency manifest above
already flags about Ansible living in Homebrew's Python: it is one more thing
that a virtual environment would make ordinary.

## What is still unpaid

Very little, and it is worth being precise about what remains rather than
declaring the rule met.

`OVERVIEW.md` asks for every controller dependency to be documented **and
scripted**. As of 2026-08-16 both halves are met: this document is the manifest,
and `bootstrap/` plus `controller_runtime` are the script. The cold start above
is four commands, one of which is a shell script whose entire scope is that
Ansible cannot install itself, and the other three of which are Ansible.

An audit on **2026-08-17** re-read that claim against the code and found it
true, with one exception and three rough edges. The exception was real: the
Ubuntu cloud image, downloaded by Pulumi from a moving URL with no checksum —
the disk every VM in the lab is imported from, and the only artifact in the
environment that nothing verified. It is pinned and checksummed now.

The three rough edges were all the same shape — automation that worked while
depending on a human to keep two things in agreement:

- The controller's WireGuard endpoint was **inline tasks in a playbook**, which
  `ANSIBLE_STANDARDS.md` forbids twice over, and so had no argument validation.
  It is the `controller_tunnel` role now. The gateway end had been a role since
  Phase 1; only the end reaching it was loose tasks.
- Both **public keys were pasted literals**, one in each end's `group_vars`,
  derived from private keys sitting in `env.sh`. Derived at run time now.
- **Nothing proved the tunnel carried traffic**, which `PROXMOX.md` asked for in
  Phase 1. It does now, in a third play, and the check is what makes the two
  changes above safe to have made.

Worth stating plainly, because the audit was prompted by a belief that the
tunnel was undocumented and unautomated: it was neither. It had been automated
since Phase 1 and described in this document and in `OVERVIEW.md`. What it
lacked was a role boundary, a derivation, and a test.

What is left is not automation, it is **state that only a backup can supply**:

- `~/.config/rke2lab/env.sh` and the two files beside it. A rebuild can create
  every machine in the lab and still not reach the end without them, and no
  amount of automation changes that — they are the secrets, and their whole
  purpose is not to be in this repository. `bootstrap/env.sh.example` narrows
  the gap to values rather than knowledge: a rebuilt controller now knows
  exactly which names it is missing.
- The **live controller in this lab still runs the old arrangement**. Ansible
  is in Homebrew's Python here and the collections are still in two places,
  because migrating a working controller is a change to make deliberately
  rather than as a side effect of writing the automation that supersedes it.
  Running `bootstrap/controller-bootstrap.sh` and then
  `playbooks/controller_bootstrap.yml` on this host is what closes it, and
  until that is done this machine and the manifest disagree — with the manifest
  being the one that a rebuild follows.

One consequence outlives the migration and is worth keeping in view.
`controller_trust` and `openbao_config` pass `ca_path` explicitly because
Homebrew's Python verifies against Homebrew's own bundle rather than the system
trust store. On a controller rebuilt from `bootstrap/`, Ansible runs on a
virtual environment over the system Python, which reads
`/etc/ssl/certs/ca-certificates.crt` — so those two roles will be passing a
path that is now simply correct rather than compensating for anything. They
should stay: explicit is right in both arrangements, and it is what makes the
two hosts behave the same.
