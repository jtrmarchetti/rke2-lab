=============================
dev.lo RKE2 Environment Guide
=============================

This site is written for two readers — the **sysadmin** who keeps it running
and the **developer** who extends it — and both are assumed to have
**no prior Kubernetes experience**. :doc:`kubernetes-basics` covers the
vocabulary before either section makes sense; :doc:`orientation` is the map
of the estate and :doc:`access` gets you a working shell and browser.

Two facts explain most of what follows, and both are enforced rather than
aspirational:

**No internal host reaches the internet.** Every artifact is downloaded once to
``repo01`` and served from there — over Apache and an APT caching proxy before
GitLab exists, and from GitLab's container and package registries afterwards.

**Nothing is configured by hand.** The cluster's workloads are reconciled by
Flux from a Git repository, and that repository is itself rendered from Ansible
in the automation repo. Editing either the cluster or GitLab directly is
overwritten on the next run. :doc:`developer/adding-a-service` is how you make
a change stick.

.. admonition:: If something is on fire
   :class: danger

   Go to :doc:`sysadmin/troubleshooting` — it is ordered by symptom, not by
   component. If you cannot reach anything at all, start with :doc:`access`
   and check the WireGuard tunnel first.

Who reads what
==============

.. list-table::
   :header-rows: 1
   :widths: 26 37 37

   * -
     - SysAdmin
     - Developer
   * - **You are here because…**
     - the estate runs services you are responsible for: health, day-to-day,
       maintenance, troubleshooting, credentials, storage
     - you want to change how the estate works: its design, its automation,
       adding a service, upgrading a piece of it
   * - **Start at**
     - :doc:`sysadmin/index` — the operator's view
     - :doc:`developer/index` — the builder's view
   * - **The reference both use**
     - :doc:`sysadmin/health-checks`
     - :doc:`sysadmin/versions`

.. toctree::
   :maxdepth: 2
   :caption: Start here

   orientation
   access
   kubernetes-basics

.. toctree::
   :maxdepth: 2
   :caption: SysAdmin — operating the estate

   sysadmin/index

.. toctree::
   :maxdepth: 2
   :caption: Developer — building on the estate

   developer/index

.. toctree::
   :maxdepth: 2
   :caption: The stack

   components/index

.. toctree::
   :maxdepth: 2
   :caption: Reference

   sysadmin/versions
   sysadmin/urls-and-access
   reference/ansible-standards
   reference/proxmox
   reference/cheatsheet
   reference/maintaining-this-guide

Where the authority lives
=========================

This site explains how to operate and extend the environment. The design
record — the *why* behind each decision — now lives in the developer and
reference sections of this guide rather than in a separate tree, and where the
operator pages and the design pages disagree the design pages win:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Page
     - Holds
   * - :doc:`developer/infrastructure-design`
     - Architecture, the artifact model, cross-cutting rules, and why each
       component was chosen
   * - :doc:`developer/automation-design`
     - The controller machine: dependency manifest and cold start
   * - :doc:`components/secrets` + :doc:`sysadmin/rotating-credentials`
     - Every secret, where it lives, and how to rotate it
   * - :doc:`developer/ansible-patterns`
     - What Flux owns, what Ansible covers, and the durable GitOps mechanics
   * - :doc:`reference/ansible-standards`
     - The conventions all automation follows
   * - :doc:`reference/proxmox`
     - The Proxmox cluster, SDN overlay, VM placement and per-VM specs
