========================
Maintaining this guide
========================

This guide describes a running environment, so it goes stale the same way the
plan documents do: silently, and in the direction of being confidently wrong.
Keeping it accurate is part of making a change, not a follow-up task.

The rule
========

**A change to the cluster is not finished until this guide reflects it.**

That is the same rule ``plan/`` already lives under, extended to the operator's
view. It is enforced by convention rather than by tooling, so the checklist
below is the whole of the mechanism.

What triggers an update
=======================

.. list-table::
   :header-rows: 1
   :widths: 42 58

   * - If the change…
     - Then update
   * - Adds or removes a service
     - :doc:`../tasks/managing-services`, :doc:`service-urls`, and a page
       under :doc:`../components/index` if it is a new technology
   * - Adds or removes a credential
     - :doc:`../tasks/rotating-credentials`, and the secret table in
       :doc:`../components/secrets`
   * - Changes a URL, hostname or address
     - :doc:`service-urls` and :doc:`../orientation`
   * - Bumps a version
     - :doc:`versions`
   * - Changes VM sizing, disks or the network
     - :doc:`../orientation` and :doc:`../tasks/node-maintenance`
   * - Changes how a change reaches the cluster
     - :doc:`../tasks/adding-a-service` and :doc:`../components/gitops`
   * - Cost you an hour to diagnose
     - :doc:`../tasks/common-issues` — this is the highest-value page in the
       guide and it only grows by someone adding what bit them

House style
===========

* **Write for a sysadmin with no Kubernetes.** Expand the acronym once, then
  use it.
* **Commands over prose.** If the reader will type it, show it.
* **Say why, once, where it is surprising.** Two replicas rather than three,
  no forwarders on ``core01``, authored client secrets — each of those is a
  decision someone will otherwise "fix".
* **Do not restate the plan documents.** Link to them. They own the design
  record; this guide owns the operating procedure.
* **Verify before you write.** Several documented facts in this project turned
  out never to have been true. Run the command and paste what it said.

Building it
===========

.. code-block:: console

   $ python3 -m venv ~/.venvs/rke2lab-docs
   $ ~/.venvs/rke2lab-docs/bin/pip install -r docs/requirements.txt
   $ make -C docs html

The build runs with ``-W``, so a broken cross-reference or an orphaned page
fails it. Output is ``docs/_build/html/``, which is not committed.

Publishing
==========

``.github/workflows/docs.yml`` builds the guide on every pull request that
touches ``docs/`` and publishes it to GitHub Pages on every push to ``main``:

    https://jtrmarchetti.github.io/rke2-lab/

The pull request build is the useful half day to day — it runs the same ``-W``
build, so a broken reference is caught before it reaches ``main``. Deployment
is skipped for pull requests, so a fork cannot publish to the site.

.. note::

   The published site is public, and the repository it is built from already
   is. That is the reason the guide names hosts, addresses and *which file*
   holds a credential, and never a credential itself — the same rule ``plan/``
   follows. Keep it that way: no secret value belongs in ``docs/``, including
   in an example.

Nothing in the build reaches the lab. Sphinx runs against the checkout alone,
and ``conf.py`` carries no extension that fetches anything, so the site can be
published from a runner that has never seen the environment.

The build has no network dependency of its own: Sphinx and furo are pinned in
``docs/requirements.txt``, and nothing in the source fetches anything. That is
deliberate — the guide must build on the controller, which is the machine
someone will be sitting at when the environment is broken.
