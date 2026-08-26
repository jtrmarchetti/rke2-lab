============
SysAdmin
============

The operator's view of ``dev.lo``: keep it healthy, run the day-to-day, fix it
when it breaks, and know what is running where. This section assumes you are
comfortable with Linux and systemd and **no prior Kubernetes experience** —
:doc:`../kubernetes-basics` covers the vocabulary first, and
:doc:`../access` gets you a working shell and browser.

Order of the pages
==================

.. list-table::
   :header-rows: 1
   :widths: 34 66

   * - Page
     - When you open it
   * - :doc:`health-checks`
     - One command set that says whether the estate is healthy; run it first
   * - :doc:`day-to-day`
     - The things you do constantly: grants, restarts, scaling, config
   * - :doc:`maintenance`
     - Planned work: reboots, drains, cold stop, VM sizing
   * - :doc:`verify-suite`
     - The ``verify/`` pytest suite: the machine's own report on the estate
   * - :doc:`rotating-credentials`
     - Which value lives where, and which playbook owns it
   * - :doc:`storage-longhorn`
     - Growing storage and what Longhorn's numbers mean
   * - :doc:`storage-pvc`
     - Expanding a claim and unsticking one that will not
   * - :doc:`troubleshooting`
     - Ordered by symptom, not by component — start here when it hurts
   * - :doc:`versions`
     - What is running, where each version is pinned
   * - :doc:`urls-and-access`
     - Every address and URL, and how to reach them from your desk

.. toctree::
   :maxdepth: 1
   :hidden:

   health-checks
   day-to-day
   maintenance
   verify-suite
   rotating-credentials
   storage-longhorn
   storage-pvc
   troubleshooting
   versions
   urls-and-access

.. admonition:: If something is on fire
   :class: danger

   Go to :doc:`troubleshooting` — it is ordered by symptom. If you cannot
   reach anything at all, start with :doc:`../access` and check the WireGuard
   tunnel first.
