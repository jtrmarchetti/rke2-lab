# dev.lo — an RKE2 lab, rebuildable from nothing

A development RKE2 cluster and every external dependency it needs — identity,
DNS, a certificate authority, a Git server with container and package
registries, GitOps reconciliation, a vault, an object store and observability —
inside a virtual Proxmox environment, with no internal host able to reach the
internet.

The whole environment is built from this repository. Nothing in it was
configured by hand and left undocumented, including the machine that does the
building.

## Rebuild it

From a bare Ubuntu 24.04 host with a copy of `~/.config/rke2lab/`:

```bash
git clone <this repository> && cd code

# The one hand-run step. Its entire scope is that Ansible cannot install
# Ansible: system packages, a virtual environment, and the pinned collections.
./bootstrap/controller-bootstrap.sh

# Secrets. Restore from backup, or start from bootstrap/env.sh.example.
source ~/.config/rke2lab/env.sh
source ~/.venvs/rke2lab/bin/activate

# The VMs.
cd infra/pulumi && pulumi up && cd ../..

# Everything else, in phase order. Its first play is the controller itself:
# split DNS, the pinned runtimes, Pulumi, and the tunnel into the lab.
cd ansible && ansible-playbook playbooks/site.yml
```

`playbooks/site.yml` runs the phases back to back, and each phase is runnable on
its own. Roles gate on a transaction rather than on a service reporting itself
up — Phase 2 does not report success until FreeIPA answers LDAP, issues a
Kerberos ticket and accepts an API login — so a false ready in one phase does
not surface as an unexplained failure in the next.

## What is where

| Path | Contents |
| --- | --- |
| `bootstrap/` | The controller's own cold start: the one script, the pinned Python requirements, and the `env.sh` template |
| `infra/pulumi/` | Every VM in the environment, as Python |
| `ansible/` | Every host's configuration: roles, playbooks, and the inventory that drives them |
| `ansible/inventory/group_vars/repo/artifacts.yml` | The artifact manifest — every binary, image and archive the lab consumes, with checksums |
| `plan/` | Architecture, per-phase implementation records, and the standards all automation follows |
| `docs/` | The sysadmin guide: how the built environment is operated, as a Sphinx site |

## Operate it

The sysadmin guide in [`docs/`](docs/) is written for whoever runs this
environment rather than whoever built it: service URLs and how to reach them,
granting access, rotating every credential, adding a GitOps-managed service,
growing Longhorn and PVCs, and a troubleshooting page ordered by symptom. It
assumes no prior Kubernetes.

Published from `main` at **<https://jtrmarchetti.github.io/rke2-lab/>**, by
`.github/workflows/docs.yml`. To build it locally:

```bash
python3 -m venv ~/.venvs/rke2lab-docs
~/.venvs/rke2lab-docs/bin/pip install -r docs/requirements.txt
make -C docs html          # docs/_build/html/index.html
```

A change to the environment is not finished until that guide reflects it — the
same rule the plan documents live under. See
[`docs/source/reference/maintaining-this-guide.rst`](docs/source/reference/maintaining-this-guide.rst).

## Where to read next

Start with [`plan/OVERVIEW.md`](plan/OVERVIEW.md) for the architecture and the
cross-phase rules, [`plan/CONTROLLER.md`](plan/CONTROLLER.md) for the
controller's dependency manifest and cold start, and
[`plan/PHASES.md`](plan/PHASES.md) for the build order.

## Two rules that explain most of the design

**No internal host reaches the internet.** Every artifact is downloaded to
`repo01` and served from there — over Apache and an APT caching proxy before
GitLab exists, and from GitLab's container and package registries afterwards.
The flow is always `internet → repo01 → GitLab → internal nodes`.

**No secret is in this repository.** Everything sensitive lives in
`~/.config/rke2lab/` and is read from the environment at run time; every
playbook asserts what it needs is present before it starts. See
[`plan/SECRETS.md`](plan/SECRETS.md).
