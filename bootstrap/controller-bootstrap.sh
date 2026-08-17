#!/usr/bin/env bash
#
# Turns a bare Ubuntu 24.04 host into one that can run this repository's
# Ansible. That is its entire job, and the job exists because of a
# chicken-and-egg problem that no amount of Ansible can solve: the automation
# cannot install the thing that runs the automation.
#
# Everything else the controller needs — split DNS, the WireGuard tunnel, the
# Pulumi CLI, the domain CA, the cluster tooling — is Ansible, and lives in
# playbooks/controller_bootstrap.yml and playbooks/controller.yml. This script
# deliberately does none of it. Its predecessor, controller_setup.sh, was named
# for setting up the controller while actually configuring dnsmasq and nothing
# else; the difference between a script's name and its contents is exactly the
# tribal knowledge plan/OVERVIEW.md forbids.
#
# Idempotent: safe to re-run, and re-running is how you apply a version change
# in bootstrap/requirements-controller.txt.
#
# Usage:
#
#   ./bootstrap/controller-bootstrap.sh
#   source ~/.venvs/rke2lab/bin/activate
#   cd ansible && ansible-playbook playbooks/controller_bootstrap.yml
#
# It installs system packages, so it will ask for sudo. It installs Python
# packages as the invoking user, into a virtual environment it owns, and never
# into the system Python.

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

# Overridable so a second controller, or a test of this script, does not have
# to collide with an existing environment. The Ansible role that reconciles
# this virtual environment reads the same variable name from inventory.
VENV="${RKE2LAB_VENV:-${HOME}/.venvs/rke2lab}"

# The interpreter the environment is built on, named rather than resolved.
# `python3` is whatever comes first on PATH, and on a controller with Homebrew
# installed that is Homebrew's Python — the exact interpreter the comment above
# says not to build on. Ansible built on it segfaults inside libpython during
# long runs, which reads as a broken playbook rather than a broken interpreter.
# Overridable for a host whose system Python is somewhere else.
PYTHON="${RKE2LAB_PYTHON:-/usr/bin/python3}"

# Where ansible-galaxy puts collections. Named explicitly rather than left to
# the default, because the default depends on which Ansible is running and a
# controller with two copies of a collection is how they silently diverge.
COLLECTIONS="${RKE2LAB_COLLECTIONS:-${HOME}/.ansible/collections}"

log() { printf '\n== %s\n' "$*"; }

# The minimum set to get Ansible running and to let it reach the lab.
#
# python3-venv is what makes the virtual environment possible; installing
# Ansible into the system Python — or, as this controller once did, into
# Homebrew's Python — ties it to an interpreter that something else upgrades.
# git is how this repository and the cluster-state repository are handled.
# The rest are what Ansible's own modules shell out to.
PACKAGES=(
    ca-certificates
    curl
    git
    openssh-client
    python3-pip
    python3-venv
    rsync
)

log "Installing system packages"
sudo apt-get update
sudo apt-get install -y "${PACKAGES[@]}"

log "Creating the automation virtual environment at ${VENV} (${PYTHON})"
if [ ! -x "${VENV}/bin/python" ]; then
    "${PYTHON}" -m venv "${VENV}"
fi
"${VENV}/bin/python" -m pip install --upgrade pip

log "Installing pinned Python dependencies"
"${VENV}/bin/python" -m pip install \
    --requirement "${REPO_ROOT}/bootstrap/requirements-controller.txt"

# Into one declared path, with the virtual environment's own ansible-galaxy.
# Using the system or Homebrew copy is how this controller ended up with each
# collection installed twice, only one of which was ever loaded.
log "Installing pinned Ansible collections into ${COLLECTIONS}"
ANSIBLE_COLLECTIONS_PATH="${COLLECTIONS}" \
    "${VENV}/bin/ansible-galaxy" collection install \
    --requirements-file "${REPO_ROOT}/ansible/requirements.yml" \
    --collections-path "${COLLECTIONS}"

# Not a nicety. Every playbook's preflight assertion reads these names out of
# the environment, and a missing env.sh fails at the first secret rather than
# here, where the fix is obvious.
if [ ! -f "${HOME}/.config/rke2lab/env.sh" ]; then
    log "No ~/.config/rke2lab/env.sh — secrets are not present yet"
    cat <<EOF
Restore it from backup, or start from the template:

    install -d -m 0700 ~/.config/rke2lab
    install -m 0600 ${REPO_ROOT}/bootstrap/env.sh.example ~/.config/rke2lab/env.sh
    \${EDITOR:-vi} ~/.config/rke2lab/env.sh

A restore also needs sealed-secrets-key.yaml and k8s-ca/ beside it; see
plan/SECRETS.md.
EOF
fi

log "Bootstrap complete"
cat <<EOF
Next, in this order:

    source ${VENV}/bin/activate
    source ~/.config/rke2lab/env.sh
    cd ${REPO_ROOT}/ansible
    ansible-playbook playbooks/controller_bootstrap.yml

That playbook owns the rest of the controller: split DNS, the WireGuard tunnel
to repo01, the Pulumi CLI and its virtual environment, and the shell
environment. Only then is the controller able to build anything else.

The full order for a cold start is in plan/CONTROLLER.md.
EOF
