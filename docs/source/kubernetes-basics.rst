=========================================
Kubernetes, for the sysadmin who has none
=========================================

Enough Kubernetes to operate this cluster. If you already run Kubernetes, skip
to :doc:`components/index`.

The five nouns that matter
==========================

**Pod**
   One or more containers scheduled together on a node. The unit that runs.
   Pods are disposable: something else recreates them, and their names change
   when it does.

**Deployment / StatefulSet / DaemonSet**
   The things that create pods. A Deployment keeps *N* interchangeable pods
   alive. A StatefulSet keeps *N* pods with stable names and their own storage
   (OpenBao, Garage, Loki). A DaemonSet keeps one pod on every node (Cilium,
   Alloy, the Longhorn agents).

**Service**
   A stable name and address in front of a set of pods. Type ``ClusterIP`` is
   reachable only inside the cluster; type ``LoadBalancer`` gets an address
   from the pool on ``192.168.2.40-52``.

**Ingress**
   An HTTP front door: hostname in, Service out. Every ``*.k8s.dev.lo`` web UI
   is an Ingress handled by Traefik on ``192.168.2.41``, with a certificate
   cert-manager issued from the ``k8s-ca`` issuer.

**PersistentVolumeClaim (PVC)**
   A request for disk. Longhorn satisfies it by creating a replicated volume
   across the workers. The claim, not the pod, owns the data.

Namespaces group all of the above. ``kubectl -n <namespace>`` or ``-A`` for all
of them.

The commands you will actually use
==================================

.. code-block:: console

   $ export KUBECONFIG=~/.kube/dev-lo.config

   $ kubectl get nodes
   $ kubectl get pods -A                       # everything, everywhere
   $ kubectl -n openbao get pods -o wide       # which node is it on
   $ kubectl -n openbao describe pod openbao-0 # events: why is it not running
   $ kubectl -n openbao logs openbao-0         # what did it say
   $ kubectl -n openbao logs openbao-0 --previous   # what did it say before it died
   $ kubectl -n openbao exec -it openbao-0 -- sh

``describe`` before ``logs``. Most failures — an image that cannot be pulled, a
volume that will not attach, a node with no room — are in the pod's *events*
and never reach the container's log, because the container never started.

Reading a pod's state
=====================

.. list-table::
   :header-rows: 1
   :widths: 26 74

   * - State
     - What it means here
   * - ``Pending``
     - Not scheduled. No node has room, or its PVC is not bound. ``describe``
       the pod, then the PVC
   * - ``ContainerCreating``
     - Usually a volume still attaching, or a Secret that does not exist yet
   * - ``ImagePullBackOff``
     - The registry said no. In this air-gapped cluster that is nearly always a
       missing mirror rule or a stale deploy token — see
       :doc:`sysadmin/troubleshooting`
   * - ``CrashLoopBackOff``
     - It starts and dies. ``logs --previous`` is the one that shows why
   * - ``Running`` but not ``Ready``
     - Its readiness probe fails. The process is up and the service is not

What is different about this cluster
====================================

**You do not ``kubectl apply``.** Flux reconciles every workload from Git. A
resource you create by hand is not deleted, but a resource you *edit* is
reverted the next time Flux reconciles — typically within minutes. Changes go
through :doc:`developer/adding-a-service`.

**Nothing pulls from the internet.** Every image comes from
``registry.gitlab.dev.lo``, rewritten by a mirror rule in each node's
``registries.yaml``. An image nobody mirrored cannot run here, however valid
its name.

**Control plane nodes are tainted.** ``CriticalAddonsOnly=true:NoExecute``
keeps workloads on the three workers. A pod that must run on a server needs a
matching toleration; almost nothing should.

**Storage is finite and local.** Three workers, one 100 GB disk each, two
replicas per volume: roughly 147 GB usable. See
:doc:`sysadmin/storage-longhorn`.
