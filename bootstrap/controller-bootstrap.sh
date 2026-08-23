#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

VENV="${RKE2LAB_VENV:-${HOME}/.venvs/rke2lab}"

PYTHON="${RKE2LAB_PYTHON:-/usr/bin/python3}"

COLLECTIONS="${RKE2LAB_COLLECTIONS:-${HOME}/.ansible/collections}"

log() { printf '\n== %s\n' "$*"; }

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

log "Installing pinned Ansible collections into ${COLLECTIONS}"
ANSIBLE_COLLECTIONS_PATH="${COLLECTIONS}" \
    "${VENV}/bin/ansible-galaxy" collection install \
    --requirements-file "${REPO_ROOT}/ansible/requirements.yml" \
    --collections-path "${COLLECTIONS}"

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
