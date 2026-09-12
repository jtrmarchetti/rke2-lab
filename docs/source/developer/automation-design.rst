========================
Automation design
========================

How a change reaches the cluster, and the conventions the automation is
built to. The prose is the shape; :doc:`../reference/ansible-standards` is
the enforcement, and the "The controller machine" section below owns the
controller itself — its dependency manifest and cold-start order.

The chain, end to end
=====================

.. code-block:: text

   1. this repository
        ansible/               inventory + roles (the desired state of the hosts)
        ansible/files/gitops_source/cluster-state/   Jinja templates (the desired state of the cluster)
        infra/pulumi/          the VMs themselves
        bootstrap/             a lost controller, rebuilt from this file
   2. the automation controller
        Pulumi (VMs)  +  Ansible (everything else),
        through the WireGuard tunnel to repo01
   3. GitLab: platform/cluster-state
        rendered from 1 by playbooks/gitops.yml, sealed, committed
   4. Flux
        GitRepository + Kustomizations, reconciling the cluster continuously

Two consequences that are easy to miss and expensive to learn the hard way:

* **The GitLab repository is a rendered artifact, not a source.** A commit
  made there is overwritten on the next render — by design. The only folder
  Ansible does not render is ``clusters/dev-lo/flux-system/`` (owned by the
  Flux CLI; regenerated on bootstrap, force-recreated if edited).
* **The controller is outside the FIPS boundary and outside the domain.**
  Nothing in the cluster depends on it at run time — the cluster reconciles
  from GitLab whether the controller is up or not. What depends on it is
  *change*: building, publishing, sealing. Losing it loses the ability to
  rebuild everything else.

The artifact pipeline
=====================

Nothing inside the internal network reaches the internet. The pipeline that
feeds it, in the order a new artifact must pass:

1. **Stage.** The artifact is declared in an artifact manifest
   (``group_vars/repo/artifacts.yml`` for the estate,
   ``group_vars/controller/artifacts.yml`` for the controller). Images are
   ``type: mirror`` — copied registry to registry by skopeo, never written to
   disk. Files are ``type: file`` — fetched from the internet on ``repo01``,
   then pushed where consumers read them.
2. **Serve.** Before GitLab exists: Apache + apt-cacher-ng on ``repo01``
   (Tier 1). After: GitLab's container and package registries, and a
   Helm-charts OCI repository (Tier 2).
3. **Rewrite.** Nodes pull only what their ``registries.yaml`` rewrites — a
   host-level catch-all per upstream host, not per namespace. A new *host*
   changes that file and costs a rolling RKE2 restart; a new *namespace*
   under an already-listed host costs nothing.
4. **Consume.** Flux installs charts from
   ``oci://registry.gitlab.dev.lo/rke2/charts``; every node's containerd is
   rewired to the same registry.

The conventions the code is built to
====================================

From :doc:`../reference/ansible-standards` — the load-bearing ones:

* **FQCN module names, logic in roles, playbooks are orchestration.**
* **Idempotency is explicit** on every state-changing task, and check mode is
  supported — a second run of any playbook reports zero changes.
* **Desired state lives in inventory**, not in extra vars; role defaults are
  the user-facing parameters.
* **Every download is pinned** — version, URL and checksum in one entry, so a
  bumped version cannot change the URL and leave the checksum behind.
* **A change is not done until the documentation is updated**: the relevant
  page in this guide — see
  :doc:`../reference/maintaining-this-guide`.

This page describes the chain at the level of *what moves where*. The two
pages that go one level deeper into the Ansible half are
:doc:`ansible-design` (how the automation is built, and why each piece is
where it is) and :doc:`ansible-patterns` (the recurring design patterns and
the failures that made them).

The review gate before any change to a running component
=========================================================

.. code-block:: console

   $ cd ansible
   $ ansible-playbook playbooks/gitops.yml -e gitops_source_push=false

Renders the tree, seals into it, prints the diff and stops before the commit.
It is the only way to see what Flux is about to be told without telling it.
Then ``make -C docs html`` — the guide builds with ``-W``, so a broken
cross-reference fails the gate.

The controller machine
======================

The controller is ``controller01`` in the inventory, reached as
``ansible_connection: local`` — the workstation the automation runs from, not
a managed VM. It sits on ``192.168.1.0/24`` and reaches the internal
``192.168.2.0/24`` network through a WireGuard tunnel to ``repo01``. It holds
every secret in the environment and is deliberately outside the domain: nothing
in the cluster depends on it at run time.

Every controller dependency is documented and scripted, so the automation
environment can be rebuilt from scratch. The manifest is
``ansible/inventory/group_vars/controller/artifacts.yml`` — the counterpart to
``group_vars/repo/artifacts.yml``, which covers the machines being *built*
while this one covers the machine doing the building. Every download the
controller makes has an entry: the four that Ansible installs carry the
version, URL and checksum the roles consume, and the rest are index rows
naming the file the pin actually lives in
(``bootstrap/requirements-controller.txt``, ``ansible/requirements.yml``,
``infra/pulumi/requirements.txt``, ``infra/pulumi/__main__.py``, and the role
defaults that list apt packages).

Two dependencies come from ``repo01`` Apache rather than upstream: **kubeseal
and the Flux CLI** are artifact-manifest entries with ``retention: bootstrap``,
staged on ``repo01`` and fetched from Apache. The line between them and what
comes from GitHub (k9s) is whether the rebuild path runs through the tool:
kubeseal is the only way to produce a SealedSecret, which puts it in the
rebuild path and in the vault's recovery path. k9s is a terminal UI; if it is
missing, someone types ``kubectl`` instead.

The one thing that is *not* automation is **state that only a backup can
supply**: ``~/.config/rke2lab/env.sh`` and the two files beside it. A rebuild
can create every machine in the lab and still not reach the end without them.
``bootstrap/env.sh.example`` narrows that gap to *values* rather than
knowledge — a rebuilt controller knows exactly which names it is missing. See
:doc:`../components/secrets`.

Cold start, from a bare Ubuntu host
-----------------------------------

Four commands, and the order between them is the whole content of the
section::

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

What each step owns:

1. ``bootstrap/controller-bootstrap.sh`` installs the system packages,
   creates ``~/.venvs/rke2lab`` from ``bootstrap/requirements-controller.txt``,
   and installs the pinned collections into ``~/.ansible/collections``. Its
   scope is one chicken-and-egg problem: *Ansible cannot install Ansible.*
2. **Secrets** restore ``~/.config/rke2lab/`` — ``env.sh`` at mode 0600, with
   ``sealed-secrets-key.yaml`` and ``k8s-ca/`` beside it.
3. ``playbooks/controller_bootstrap.yml`` does the rest, in this order:
   split DNS (nothing that resolves a ``dev.lo`` name works before this),
   then the pinned runtimes via the ``controller_runtime`` role, then the
   WireGuard tunnel last — because the tunnel is what reaches the internal
   network, it precedes every Ansible run against an internal host, but the
   play that configures it reaches ``repo01`` on ``192.168.1.20``, so
   configuring the tunnel never depends on the tunnel.
4. ``playbooks/site.yml`` builds the environment, and imports step 3 as
   its own first play.

Two things are *not* in this list because they cannot be: **SSH host keys**
and **the FreeIPA CA**, both handled by the ``controller_trust`` role inside
``playbooks/controller.yml``, which takes ``kubectl`` from a cluster node and
so cannot run until a cluster exists.
