=========================
dev.lo Sysadmin Guide
=========================

This guide is for the people who keep the ``dev.lo`` RKE2 environment running:
operating the services it hosts, adding new ones, rotating its credentials, and
fixing it when it breaks. It assumes you are comfortable with Linux and systemd
and assumes **no prior Kubernetes experience** — :doc:`kubernetes-basics`
covers what you need before the rest of the guide makes sense.

Two facts explain most of what follows, and both are enforced rather than
aspirational:

**No internal host reaches the internet.** Every artifact is downloaded once to
``repo01`` and served from there — over Apache and an APT caching proxy before
GitLab exists, and from GitLab's container and package registries afterwards.

**Nothing is configured by hand.** The cluster's workloads are reconciled by
Flux from a Git repository, and that repository is itself rendered from Ansible
in the automation repo. Editing either the cluster or GitLab directly is
overwritten on the next run. :doc:`tasks/adding-a-service` is how you make a
change stick.

.. admonition:: If something is on fire
   :class: danger

   Go to :doc:`tasks/common-issues` — it is ordered by symptom, not by
   component. If you cannot reach anything at all, start with
   :doc:`access` and check the WireGuard tunnel first.

.. toctree::
   :maxdepth: 2
   :caption: Start here

   orientation
   access
   kubernetes-basics

.. toctree::
   :maxdepth: 2
   :caption: The stack

   components/index

.. toctree::
   :maxdepth: 2
   :caption: How to

   tasks/index

.. toctree::
   :maxdepth: 2
   :caption: Reference

   reference/service-urls
   reference/cheatsheet
   reference/versions
   reference/maintaining-this-guide

Where the authority lives
=========================

This guide explains how to operate the environment. It is not the design
record, and where the two disagree the design record wins:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Document
     - Holds
   * - ``plan/OVERVIEW.md``
     - Architecture, the artifact model, and the cross-phase rules
   * - ``plan/TARGETS.md``
     - Per-VM specifications: CPU, RAM, disks, addresses
   * - ``plan/SECRETS.md``
     - Every secret, where it lives, and how to rotate it
   * - ``plan/CLUSTER_COMPONENTS.md``
     - Which component was chosen and why
   * - ``plan/PHASE<N>_IMPLEMENTATION.md``
     - What each build phase did, and what it learned by failing
   * - ``plan/ANSIBLE_STANDARDS.md``
     - The conventions all automation follows
