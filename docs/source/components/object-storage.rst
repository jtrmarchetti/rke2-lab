=====================
Garage (object store)
=====================

Garage 2.3.0, a single-node S3 store in the ``garage`` namespace, on a 20 GiB
two-replica Longhorn volume. Loki and Tempo write their chunks here; anything
else that wants a bucket can too.

.. code-block:: console

   $ kubectl -n garage get pods,pvc
   $ kubectl -n garage exec -it garage-0 -- /garage status
   $ kubectl -n garage exec -it garage-0 -- /garage bucket list
   $ kubectl -n garage exec -it garage-0 -- /garage key list

Endpoints
=========

.. list-table::
   :header-rows: 1
   :widths: 34 66

   * - Name
     - Purpose
   * - ``https://s3.k8s.dev.lo``
     - S3 API, path style
   * - ``https://<bucket>.s3.k8s.dev.lo``
     - S3 API, virtual-host style. The ingress carries the wildcard and DNS is
       told about the bucket form explicitly, because the cluster's DNS answers
       single-label names under ``k8s.dev.lo`` and a bucket name makes two
   * - ``garage.garage.k8s.dev.lo``
     - The Service directly, for in-cluster and admin use

Credentials
===========

Two vault paths, because the halves have different origins and one path with
two writers would mean whichever wrote last dropped the other's keys:

``kv/garage-cluster``
   ``admin-token``, ``metrics-token``, ``rpc-secret`` — authored by hand,
   because Garage's own configuration names them and they must exist before
   Garage does.

``kv/garage``
   ``access_key``, ``secret_key`` — minted by Garage once it has started, then
   written into the vault by ``openbao_secrets`` on the **next** run.

.. note::

   ``kv/garage`` being a run late is structural, not a bug. ``lookup('env')``
   reads the environment the ``ansible-playbook`` process started with, so
   credentials appended to ``env.sh`` during a run are on disk but invisible to
   the run that wrote them. Re-source ``env.sh`` and run ``cluster_init.yml``
   again.

Garage does not federate to Keycloak. There is no OIDC in it to configure — it
authenticates S3 requests with access keys and its admin API with a bearer
token.

Adding a bucket for a new service
=================================

.. code-block:: console

   $ kubectl -n garage exec -it garage-0 -- /garage bucket create myapp
   $ kubectl -n garage exec -it garage-0 -- /garage key create myapp-key
   $ kubectl -n garage exec -it garage-0 -- \
       /garage bucket allow --read --write myapp --key myapp-key

Then put the key into OpenBao and give the workload an ``ExternalSecret`` —
never a Secret in Git. :doc:`../tasks/adding-a-service` has the full path.
