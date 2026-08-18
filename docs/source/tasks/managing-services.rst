=================
Managing services
=================

Where things are
================

.. list-table::
   :header-rows: 1
   :widths: 22 34 44

   * - Service
     - URL
     - Sign in with
   * - GitLab
     - ``https://gitlab.dev.lo``
     - Keycloak, or ``root`` locally
   * - Keycloak
     - ``https://sso.k8s.dev.lo/admin``
     - Your domain account, through ``keycloak-admins`` — which also
       administers **master**. The local ``admin`` is break-glass
   * - OpenBao
     - ``https://bao.k8s.dev.lo``
     - Keycloak (OIDC), or the root token
   * - Grafana
     - ``https://grafana.k8s.dev.lo``
     - Keycloak, or local ``admin``
   * - Longhorn
     - ``https://longhorn.k8s.dev.lo``
     - Keycloak, through oauth2-proxy. No other way in
   * - Garage S3
     - ``https://s3.k8s.dev.lo``
     - S3 access keys from ``kv/garage``
   * - FreeIPA
     - ``https://core.dev.lo``
     - ``admin``, with the directory password
   * - Kubernetes API
     - ``https://kube.dev.lo:6443``
     - ``~/.kube/dev-lo.config``
   * - Artifacts (Apache)
     - ``http://192.168.2.99/``
     - None; internal network and tunnel only

All ``k8s.dev.lo`` names resolve to ``192.168.2.41`` and need the SOCKS proxy
and the domain CA from your workstation — see :doc:`../access`.

Granting someone access
=======================

Always in FreeIPA. Never in Keycloak, and never in the service:

.. code-block:: console

   # on core01
   $ docker exec -it freeipa-server bash
   $ kinit admin

   $ ipa user-add alice --first Alice --last Smith
   $ ipa group-add-member grafana-users  --users alice
   $ ipa group-add-member longhorn-admins --users alice
   $ ipa group-add-member keycloak-admins --users alice   # administers SSO

   $ ipa group-show grafana-admins       # who has this today

Groups follow the pattern ``<app>-admins`` and ``<app>-users`` for every
federated application: ``grafana``, ``longhorn``, ``openbao``, ``gitlab``.
Removal is ``ipa group-remove-member``.

``keycloak-admins`` is the exception with no ``-users`` half: its members
administer both Keycloak realms, and everyone else in the domain can already
sign in to Keycloak without any group at all.

.. note::

   A change takes effect at the user's **next sign-in**. Keycloak maps groups
   to roles when it issues a token, and OpenBao attaches policies at login.
   Nothing propagates to an open session.

   New users need a full directory sync in Keycloak before they appear, if the
   periodic sync has not run yet.

   A *new group membership* for a user Keycloak has already imported needs
   more than a sync: its user cache has to be dropped, or the membership stays
   invisible while the console shows it as correct. Re-running
   ``keycloak_ldap`` does both.

Restarting a workload
=====================

Flux will put back anything you delete, which makes restarts safe:

.. code-block:: console

   $ kubectl -n <ns> rollout restart deploy/<name>
   $ kubectl -n <ns> rollout restart statefulset/<name>
   $ kubectl -n <ns> delete pod <name>            # blunt, and fine

   $ kubectl -n <ns> rollout status deploy/<name>

For a StatefulSet holding data — OpenBao, Garage, Keycloak's database — expect
the restart to cost a volume detach and re-attach, and expect OpenBao to come
back **sealed** until the unsealer catches it.

Making a change stick
=====================

Editing a live resource with ``kubectl edit`` is reverted by Flux, usually
within minutes. It is a fine way to test a hypothesis and never a way to
configure anything. The real path is :doc:`adding-a-service`.

Scaling
=======

Replica counts live in the rendered manifests, so scaling is a repo change like
any other. Temporarily, for an experiment:

.. code-block:: console

   $ kubectl -n <ns> scale deploy/<name> --replicas=2

Remember the ceiling: three workers with 10 GiB each, on a hypervisor with no
memory headroom. Check ``kubectl top nodes`` before adding replicas of
anything.

Suspending a service
====================

.. code-block:: console

   $ flux suspend helmrelease <name> -n <ns>
   $ flux resume  helmrelease <name> -n <ns>

   $ flux get helmreleases -A          # suspended ones are marked here

A suspended HelmRelease looks like a healthy one in ``kubectl``. Check
``flux get`` before concluding that reconciliation is broken.
