=====================================
Testing the cluster: the verify suite
=====================================

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

``verify/test_mtls.py`` — the service-to-service mTLS configuration surface:
the ``rke2-cilium`` HelmChartConfig carries mutual authentication (SPIRE)
and IPsec encryption, the ``cilium-ipsec-keys`` Secret holds a well-formed
key line, the pilot policy enforces the SSO path, the SPIRE stack is
scheduled, and the edge TLS termination is still owned by the platform
Gateway — nothing in the mesh pulls it in. The whole module skips until
the feature is deployed on the estate (the HCC has no ``authentication``
block yet); once the build lands it, it enforces on every warm build so a
drift or rollback fails fast.

``verify/test_mtls_dataplane.py`` — the data-plane half of the same feature:
the SPIRE server reports healthy (SVIDs can be issued), a live agent-to-agent
mTLS handshake between two throwaway pods in a ``mtls-verify`` namespace
succeeds under a required-auth CiliumNetworkPolicy, a third pod outside the
rule's ``fromEndpoints`` is refused (per-connection enforcement, not just a
config flag), the running agents report IPsec enabled and the node kernels
carry ESP xfrm policies, and the edge hosts still terminate TLS with the
domain CA. The probes run in the throwaway namespace, which the fixture
tears down on exit; like the config surface, the whole module skips until
the feature is deployed.

``verify/test_hubble.py`` — the Hubble configuration surface: the
``rke2-cilium`` HelmChartConfig carries the ``hubble`` values block that
switches on the agent-side Hubble server, the relay and the UI; the relay
and UI Deployments in ``kube-system`` are Ready (the relay's readiness
proves it reached an agent's Hubble server over TLS); the cilium
configmap still carries ``enable-hubble``; and the platform Gateway
carries the ``hubble`` listener with the shim-issued ``hubble-edge-tls``
certificate and an Accepted, bound HTTPRoute. Like the mTLS module, the
whole module skips until the values block is on the estate (the HCC has no
``hubble`` key) and enforces on every warm build after. ``hubble`` also
joins the edge-host list in ``verify/test_gateway.py``: the listener, the
route and the TLS-to-domain-CA check cover it with the other edge hosts.

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
