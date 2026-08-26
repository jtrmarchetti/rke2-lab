======================
Upgrading the estate
======================

What to move, where the pin lives, and what it costs. The one invariant:
**an upgrade is an edit to a file in this repository, never a command on a
host.** Every version in the estate is pinned exactly once, in a file
automation reads — :doc:`../sysadmin/versions` is the table of those files.

Choosing the next version
=========================

A default is a fact with an expiry date. Check the component is still alive
upstream — not merely still the default, or still the version some document
named last year. When in doubt, stay: major jumps cost review time that is
rarely repaid in a lab, and the version that has run through this
environment's phases is the one the :doc:`../sysadmin/versions` table
records.

The three tiers, and what each costs
====================================

.. list-table::
   :header-rows: 1
   :widths: 22 40 38

   * - Tier
     - Move it
     - What it costs
   * - **A GitOps app or chart**
     - Bump the pin in its manifest under
       ``ansible/files/gitops_source/cluster-state/`` (and the matching
       ``artifacts.yml`` / ``inventory_rke2_publish_chart_sets`` entry, so the
       artifact, the chart set and the manifest agree — see
       :doc:`adding-a-service`)
     - ``playbooks/cluster_services.yml`` to stage the new artifact, then
       ``playbooks/gitops.yml`` (``-e gitops_source_push=false`` first, read
       the diff). Flux picks it up. A rolling restart of the workload
   * - **RKE2**
     - Bump ``inventory_rke2_node_version`` in
       ``group_vars/kubecp`` **and** ``kubewk``, and
       ``inventory_rke2_publish_package_version`` in
       ``group_vars/repo`` — all three must name the same release. The
       installer and image tarballs are staged from the upstream release URL
       in ``group_vars/repo/artifacts.yml`` — the ``.published.<source>``
       marker scheme is what makes the refetch-on-bump automatic, and its
       why is recorded in :doc:`ansible-patterns`
     - ``playbooks/cluster_services.yml`` publishes the package to GitLab,
       then ``kubecp.yml`` and ``kubewk.yml`` install it. The install task
       re-runs the installer only when the installed version differs, and a
       handler restarts RKE2 — a rolling restart of every node. **Control
       plane first, one node at a time** — the etcd killall check in
       :doc:`../sysadmin/maintenance` applies. Workers only after the
       control plane is healthy
   * - **The controller's own tooling**
     - The pin in ``group_vars/controller/artifacts.yml`` or
       ``bootstrap/requirements-controller.txt``
     - ``playbooks/controller.yml``. Nothing in the cluster changes; only the
       machine that drives it

The order that matters
======================

Within one change, the sequence is always:

#. **Artifacts first.** The new image/chart must be staged to ``repo01`` and
   published to GitLab's registries *before* any manifest asks for it, or the
   rollout fails on a pull of something that does not exist in the mirror.

#. **The RKE2 node version, if changing**, before the app manifests that need
   the new cluster features.

#. **GitOps last** — the rendered change lands only when the whole chain is
   in place.

After the change
================

#. ``flux get helmreleases -A`` — every release back to ready. Give
   kube-prometheus-stack its install window (it is the slow one) before
   concluding a release is stuck.

#. The health-checks set: :doc:`../sysadmin/health-checks`.

#. The machine's own report: :doc:`../sysadmin/verify-suite`.

#. A clean second run of every playbook you touched reports zero changes.

#. The version table: :doc:`../sysadmin/versions` — bump the rows, and
   :doc:`../reference/maintaining-this-guide` says the change is not done
   until it is.

.. warning::

   **Stalled HelmReleases do not recover on their own.** A HelmRelease that
   flips to ``Stalled=True`` is terminal: the controller will not retry it,
   and a plain ``flux resume`` does not clear it. The working fix is suspend,
   wait a few seconds, resume — and ``playbooks/cluster_init.yml`` carries a
   ``flux_unstall`` task that does exactly that, idempotently, when nothing
   is stalled.

.. note::

   Longhorn has no external backup target configured: its safety is two
   replicas across two workers, and that is the limit. Losing a volume means
   losing two workers at once — the reboot discipline in
   :doc:`../sysadmin/maintenance` is what protects it.
