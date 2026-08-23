====================================
Testing the cluster: the verify suite
====================================

The repository ships a pytest harness in ``verify/`` that checks the running
estate end to end: it drives the *same* flows a person would (the service's
SSO entry point, the Keycloak login form, the service callback) and reads
actual data back out of the datasources — not just "no error". It is
deliberately separate from Ansible: Ansible reconciles state, the suite
measures it.

What it checks
==============

``verify/test_cluster.py`` — baseline health: all nodes Ready, every Flux
Kustomization applied, every HelmRelease installed and Ready, no pod in a
crash state. The other suites only make sense on a healthy estate, so this
runs first and fails fast.

``verify/test_sso.py`` — single sign-on, per service:

* GitLab — a federated user lands on their own ``/api/v4/user``.
* Grafana — the token's ``roles`` claim maps to the right *org role*
  (Viewer for the user tier, Admin for the admin tier) via
  ``role_attribute_path``. The estate sets
  ``allow_assign_grafana_admin: false``, so the mapping drives the org
  role, never the global admin flag — the assertion reads the role back
  through the admin session, which is the tier allowed to list
  ``/api/org/users``.
* Longhorn (behind oauth2-proxy) and OpenBao (the vault's OIDC method) —
  the token's ``roles`` claim is exactly what authorizes those two, so
  the test proves the claim, which proves the tier.
* Keycloak — group membership maps to the realm roles, and the master
  realm's ``admin`` still logs in.
* A wrong password is rejected by the real login form — the negative
  case, so a permissive IdP is caught.

``verify/test_grafana_data.py`` — the three datasources behind Grafana hold
*real* data: Prometheus knows about live targets, Loki answers a label query
and a log query returns actual lines, and Tempo holds a trace the suite
injected itself through the Alloy OTLP endpoint (a canary span, found back
through Grafana). "No data" screens would not show up in the UI but pass
none of these.

``verify/test_longhorn.py`` — all expected PVC-backed volumes exist and are
attached with healthy replicas; no Longhorn disk is near full; every
Longhorn node is Ready and schedulable.

``verify/test_openbao.py`` — the vault holds exactly the runtime secrets a
cold rebuild needs (including the Garage S3 key), the SSO ACL policies are
defined, the admin policy is scoped, and the root token still carries the
root policy (the offline way-in).

Running it
==========

.. code-block:: console

   $ source ~/.config/rke2lab/env.sh
   $ ~/.venvs/rke2lab/bin/python -m pytest verify/ -v

The suite talks to the estate over the public URLs; the only things it
needs are the controller environment (``env.sh``) and the domain CA
(``/usr/local/share/ca-certificates/dev.lo-ca.crt`` — every request is
pinned to it; the suite refuses ``verify=False``). Running a single file:

.. code-block:: console

   $ ~/.venvs/rke2lab/bin/python -m pytest verify/test_sso.py -v

The ephemeral users
===================

The SSO tests need users that exist for the run and not after. A
session-scoped fixture (``verify/conftest.py``) provisions two FreeIPA
users — ``test`` in every ``*-users`` group and ``test.adm`` in every
``*-admins`` group plus ``keycloak-admins`` — with fresh random passwords,
and removes both on teardown, even when a test fails.

Two things the fixture does that are not obvious:

* After provisioning it flushes Keycloak's imported-user cache
  (``POST /admin/realms/dev-lo/clear-user-cache``). With LDAP
  ``importEnabled``, a user is imported the moment it appears in the
  directory — between the ``user-add`` and the ``passwd`` — so the cached
  entry carries a credential the harness never set. Without the flush, a
  freshly provisioned user can fail to log in for up to the cache TTL.
* The Grafana user rows are *not* cleaned up: they are keyed by the
  deterministic federated email, so a re-run is a repeat login, which is
  what the estate must survive (see below).

What a failure means
====================

A failing SSO test is a report that the sign-in flow broke the way a user
would notice it — which endpoint 302'd where, which step of the OAuth
round trip failed, and what the service did with the token. Read the
assertion message; it names the step.

Grafana repeat logins
---------------------

Grafana's generic OAuth user-sync hook can only reconcile an *existing*
user from the token's email when ``[auth] oauth_allow_insecure_email_lookup``
is set; without it, a second sign-in of the same user dies with
``user not found`` in the post-auth hook and the session is never
established. The estate carries that setting (set in the GitOps-managed
Grafana values), so repeat logins of a known user work. If a Grafana SSO
test suddenly 401s on a user that logged in before, this is the first
thing to check.
