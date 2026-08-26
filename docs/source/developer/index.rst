=============
Developer
=============

The builder's view of ``dev.lo``: how it is designed, how the automation is
designed, and the two operations a developer actually performs on it — adding a
service and upgrading a piece of the infrastructure. Everything here assumes
you will make the change in the automation repo, not in the cluster, and that
:doc:`../kubernetes-basics` already gave you the vocabulary.

Order of the pages
==================

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Page
     - When you open it
   * - :doc:`infrastructure-design`
     - *Why* the estate is shaped this way: the network, DNS, artifact and
       certificate models, and what they mean for a new service
   * - :doc:`automation-design`
     - How a change reaches the cluster, the artifact pipeline, and the
       conventions the automation is built to
   * - :doc:`ansible-design`
     - The Ansible half in detail: layout, the variable rules, idempotency,
       and why each piece is where it is
   * - :doc:`ansible-patterns`
     - The recurring design patterns — marker files, the Flux/Ansible
       ownership split, out-of-band recovery — and the failures that made
       them
   * - :doc:`adding-a-service`
     - The step-by-step path from a new image to a running, federated service
   * - :doc:`upgrades`
     - How to move a piece forward: RKE2, a chart, an app, the controller

.. toctree::
   :maxdepth: 1
   :hidden:

   infrastructure-design
   automation-design
   ansible-design
   ansible-patterns
   adding-a-service
   upgrades

.. admonition:: The one rule that makes the rest safe
   :class: important

   Nothing in the cluster is edited directly. The cluster reconciles from Git,
   and the Git repository is rendered from this repository by
   ``playbooks/gitops.yml``. A change made in the cluster or in GitLab is
   overwritten on the next run — by design. The developer's tool is the
   repository; the cluster is a projection of it.
