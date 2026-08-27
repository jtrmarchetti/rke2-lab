================================================
PersistentVolumeClaims: expanding and unsticking
================================================

Expanding a claim
=================

Both Longhorn StorageClasses have ``allowVolumeExpansion: true``, so this
works — but the *durable* change is in Git, not in the cluster.

#. Edit the size in the rendered source, under
   ``ansible/files/gitops_source/cluster-state/`` — the ``resources.requests.storage``
   of the PVC or of the StatefulSet's ``volumeClaimTemplates``.

#. Render, review, push:

   .. code-block:: console

      $ ansible-playbook playbooks/gitops.yml -e gitops_source_push=false
      $ ansible-playbook playbooks/gitops.yml

#. Watch it apply:

   .. code-block:: console

      $ kubectl -n <ns> get pvc <name> -w
      $ kubectl -n <ns> describe pvc <name>

.. important::

   **A StatefulSet's ``volumeClaimTemplates`` are immutable.** Editing the size
   there does not resize existing claims, and Flux's apply will fail on the
   StatefulSet with a validation error. The working order is: patch the live
   PVCs first, then change the template so *future* replicas match.

   .. code-block:: console

      $ kubectl -n observability patch pvc storage-loki-0 \
          -p '{"spec":{"resources":{"requests":{"storage":"10Gi"}}}}'

   If Flux still refuses the StatefulSet, delete it with
   ``--cascade=orphan`` and let Flux recreate it; the pods and the PVCs survive.

Longhorn expands the volume online for most workloads. If the PVC reports
``FileSystemResizePending``, the filesystem grows when the pod next restarts:

.. code-block:: console

   $ kubectl -n <ns> rollout restart statefulset/<name>

Shrinking is not possible. Create a smaller claim and migrate if you must.

A claim that will not bind
==========================

.. code-block:: console

   $ kubectl -n <ns> describe pvc <name>

.. list-table::
   :header-rows: 1
   :widths: 34 66

   * - Event says
     - Cause
   * - ``no persistent volumes available``
     - The StorageClass name is wrong, or Longhorn's CSI is not running
   * - ``failed to provision``, insufficient storage
     - The workers are full. :doc:`storage-longhorn`
   * - Nothing at all, stuck ``Pending``
     - ``volumeBindingMode`` waiting on a pod that itself cannot schedule —
       fix the pod first
   * - ``Bound`` but the pod is ``ContainerCreating``
     - Attach, not provisioning. Check ``longhorn-manager`` on the pod's node

A claim that will not delete
============================

A PVC stays ``Terminating`` while anything still uses it. Find the holder
before reaching for a finalizer:

.. code-block:: console

   $ kubectl -n <ns> get pods -o json \
       | jq -r '.items[] | select(.spec.volumes[]?.persistentVolumeClaim.claimName=="<name>") | .metadata.name'

Delete the workload, and the claim follows. Removing the finalizer by hand
leaves Longhorn holding a volume nothing references, which is how the orphans
in :doc:`storage-longhorn` are made.

Choosing a size and class for something new
===========================================

* ``longhorn`` — two replicas, the default, for anything whose loss matters.
* ``longhorn-single`` — one replica, for caches and scratch only.
* Start small. Expanding is routine; shrinking is a migration.
* Remember the ceiling: ~147 GB usable across the whole cluster.
