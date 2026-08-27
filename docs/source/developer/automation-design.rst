========================
Automation design
========================

How a change reaches the cluster, and the conventions the automation is
built to. The prose is the shape; ``plan/ANSIBLE_STANDARDS.md`` is the
enforcement, and ``plan/CONTROLLER.md`` owns the controller machine itself.

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

From ``plan/ANSIBLE_STANDARDS.md`` — the load-bearing ones:

* **FQCN module names, logic in roles, playbooks are orchestration.**
* **Idempotency is explicit** on every state-changing task, and check mode is
  supported — a second run of any playbook reports zero changes.
* **Desired state lives in inventory**, not in extra vars; role defaults are
  the user-facing parameters.
* **Every download is pinned** — version, URL and checksum in one entry, so a
  bumped version cannot change the URL and leave the checksum behind.
* **A change is not done until the documentation is updated**: the relevant
  ``plan/`` document, and this site — see
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
