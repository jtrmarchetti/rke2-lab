=====================
Longhorn (storage)
=====================

Longhorn 1.12.1 is the only storage class provider in the cluster. It writes
into one dedicated 100 GB disk per worker, mounted at ``/var/lib/longhorn``.

Sizing, and why
===============

.. list-table::
   :header-rows: 1
   :widths: 26 74

   * - Fact
     - Consequence
   * - Three workers, 100 GB each
     - ~322 GB raw, and Longhorn cannot overrun the hypervisor's thin pool
       however many volumes exist
   * - **Two** replicas, not three
     - ~147 GB usable instead of 98 GB, one fewer fsync per write, and a spare
       node to rebuild onto when one fails
   * - ``longhorn`` is the default class
     - Two replicas. Use it for anything whose loss matters
   * - ``longhorn-single``
     - One replica. Caches and scratch only — a single node loss destroys the
       volume. **Never** for OpenBao

Three replicas on a three-node cluster would put a copy on every node and leave
Longhorn nowhere to rebuild, so a node loss means degraded until it returns.
Two tolerates the same single node loss and heals itself.

Health
======

.. code-block:: console

   $ kubectl -n longhorn-system get pods
   $ kubectl get sc
   $ kubectl get pv
   $ kubectl -n longhorn-system get volumes.longhorn.io
   $ kubectl -n longhorn-system get nodes.longhorn.io -o wide

The UI is at ``https://longhorn.k8s.dev.lo``, behind an oauth2-proxy — see
:doc:`identity`. The ``volumes.longhorn.io`` and ``nodes.longhorn.io`` custom
resources hold the same information the UI shows, which matters on the day the
proxy is what is broken.

What its states mean
====================

.. list-table::
   :header-rows: 1
   :widths: 24 76

   * - Volume state
     - Meaning
   * - ``attached`` / ``healthy``
     - Normal. Both replicas present
   * - ``degraded``
     - Attached and serving, one replica missing. Longhorn rebuilds
       automatically if a node has room
   * - ``detached``
     - No pod is using it. Normal for an idle claim, a problem if a pod is
       waiting
   * - ``faulted``
     - No usable replica. This is data loss territory — do not delete anything
       until you have read the replica list

Common failures
===============

**A volume will not attach.** Almost always the ``longhorn-manager`` or CSI
plugin pod on the target node. ``kubectl -n longhorn-system get pods -o wide``
and look at the node the pending workload was scheduled to.

**A replica will not rebuild.** No node has room, or the node is marked
unschedulable in Longhorn's own node resource — which is separate from the
Kubernetes node being cordoned.

**Everything is degraded at once.** A worker is down, or its
``/var/lib/longhorn`` mount is gone. Check ``df -h /var/lib/longhorn`` on each
worker: if the disk did not mount, Longhorn is writing to the OS disk under the
mount point instead.

**Disk full.** Longhorn over-provisions by default, so the sum of claims can
exceed what the disks hold. See :doc:`../sysadmin/storage-longhorn` for growing
capacity and for the eviction path.

Backups
=======

Longhorn can back up to S3, and Garage is running in the same cluster — which
makes it a convenience, not a disaster recovery plan: a cluster loss takes both.
Anything that must survive the cluster belongs off it.
