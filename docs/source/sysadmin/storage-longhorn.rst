=====================================
Longhorn: troubleshooting and growing
=====================================

Where the space is
==================

.. code-block:: console

   # on each worker
   $ df -h /var/lib/longhorn

   # from the cluster
   $ kubectl -n longhorn-system get nodes.longhorn.io -o wide
   $ kubectl -n longhorn-system get volumes.longhorn.io
   $ kubectl get pv

Three workers, one 100 GB disk each, two replicas per volume: roughly 147 GB
usable. Longhorn over-provisions, so the sum of what is *claimed* can exceed
what exists — the number that matters is actual usage on the three disks.

Diagnosing
==========

Work in this order. Each step rules out the layer below it.

**1. Is the disk mounted?**

.. code-block:: console

   $ ssh root@192.168.2.31 'df -h /var/lib/longhorn; lsblk'

If the mount is missing, Longhorn has been writing into the OS disk under the
mount point. Fix the mount before anything else — ``ansible-playbook
playbooks/kubewk.yml`` reconciles it.

**2. Is Longhorn's view of the node healthy?**

.. code-block:: console

   $ kubectl -n longhorn-system get nodes.longhorn.io -o yaml | grep -A5 conditions

A node can be schedulable to Kubernetes and unschedulable to Longhorn; they are
separate flags.

**3. Are the manager and CSI pods up on that node?**

.. code-block:: console

   $ kubectl -n longhorn-system get pods -o wide | grep <node>

A volume that will not attach is nearly always this.

**4. What does the volume itself say?**

.. code-block:: console

   $ kubectl -n longhorn-system get volumes.longhorn.io <pv-name> -o yaml
   $ kubectl -n longhorn-system get replicas.longhorn.io | grep <pv-name>

``degraded`` with a rebuild in progress is self-healing. ``degraded`` with no
rebuild means no node has room, or the only candidate is the one that already
holds the surviving replica.

Growing capacity
================

There are three ways, in increasing order of cost.

Reclaim what is already there
-----------------------------

.. code-block:: console

   $ kubectl get pvc -A                     # anything Bound to nothing?
   $ kubectl -n longhorn-system get volumes.longhorn.io | grep detached

Detached volumes belonging to deleted workloads still consume disk. Longhorn's
StorageClasses use ``reclaimPolicy: Delete``, so a deleted PVC does take its
volume with it — but a volume created outside a claim, or one whose PVC was
recreated, can be orphaned. Delete those in the Longhorn UI after confirming
what they were.

Trim retention on the biggest consumers — Prometheus, Loki and Tempo are
usually the top three — and remember Loki's and Tempo's long-term chunks
already go to Garage.

Grow the existing disks
-----------------------

The clean route, and it needs one reboot per worker:

#. Grow the third disk in Pulumi (``infra/pulumi/modules/vm_definitions.py``)
   and ``pulumi up``. Only ever grow: shrinking a virtual disk destroys the
   filesystem on it.
#. Reflect the new size in ``plan/TARGETS.md`` — that file is the source of
   truth for the VM definitions and the two must stay in sync.
#. On the worker, grow the partition and filesystem:

   .. code-block:: console

      $ kubectl drain kubewk01.dev.lo --ignore-daemonsets --delete-emptydir-data
      $ ssh root@192.168.2.31
      # resize2fs /dev/sdc            # ext4, whole-device — check lsblk first
      $ kubectl uncordon kubewk01.dev.lo

#. Longhorn picks up the new capacity on its own. Confirm with
   ``kubectl -n longhorn-system get nodes.longhorn.io -o wide``.

Do **one worker at a time**, and wait for every volume to return to
``healthy`` before starting the next. Two workers down at once, with two
replicas, is data loss.

Add a worker
------------

The most capacity per unit of risk, and the most work: a new VM in Pulumi, an
inventory entry, ``playbooks/kubewk.yml``, and the hypervisor has to have the
memory — which today it does not, at 64 GiB allocated against 62.8 GiB
physical. Grow the hypervisor first.

.. warning::

   Longhorn's three disks cap its contribution to the hypervisor's thin pool at
   about 322 GB no matter how many volumes exist, which is what makes it safe
   to run at all. Growing them relaxes that guarantee — check free space in the
   Proxmox thin pool *and* on ``repo01``'s ``/`` before you do.

Reducing pressure without more disk
===================================

* Move a workload's volume to ``longhorn-single`` **only** if its data is
  genuinely reconstructible. That halves its footprint and forfeits node-loss
  tolerance. Never for OpenBao.
* Shorten Prometheus retention, or drop scrape targets you do not read.
* Set explicit, smaller PVC sizes for new services. Growing a claim later is
  easy (:doc:`storage-pvc`); shrinking one is not possible.
