===========================
Identity: FreeIPA, Keycloak
===========================

Identity is federated, not duplicated. FreeIPA is the authority for who someone
is and what groups they are in. Keycloak holds only the sentence FreeIPA cannot
express — that members of ``grafana-admins`` are administrators of Grafana —
and issues the tokens services actually read.

The model
=========

Two groups per application in FreeIPA, ``<app>-admins`` and ``<app>-users``,
mapped to two Keycloak client roles named ``admin`` and ``user``. Granting
someone access to Grafana is one command in FreeIPA and nothing else:

.. code-block:: console

   $ ipa group-add-member grafana-users --users alice

Each service decides what the two mean in its own terms: Grafana turns them
into Admin and Viewer, OpenBao into two policies, oauth2-proxy into permission
to reach Longhorn at all.

Realms
======

Applications and federated users live in the **``dev-lo``** realm. The
``master`` realm administers every other realm and holds Keycloak's own local
administrator, which stays local deliberately: a Keycloak whose administrators
were themselves federated would be unadministrable exactly when FreeIPA is what
is broken.

Health
======

.. code-block:: console

   $ kubectl -n keycloak get pods
   $ kubectl -n keycloak logs -l app.kubernetes.io/name=keycloak --tail=100
   $ kubectl -n keycloak get pvc            # its PostgreSQL

   $ curl -s https://sso.k8s.dev.lo/realms/dev-lo/.well-known/openid-configuration | head

Keycloak runs on the cluster with its own PostgreSQL; the database password
comes from OpenBao through ESO. It binds FreeIPA over **LDAPS** at
``ldaps://core.dev.lo:636``.

Who federates, and how
======================

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Service
     - Mechanism
   * - Grafana
     - Native OIDC. ``role_attribute_path`` reads the ``roles`` claim;
       ``role_attribute_strict`` refuses a user with no role
   * - OpenBao
     - Native OIDC, with a second callback on ``localhost:8250`` so
       ``bao login -method=oidc`` works from a terminal
   * - GitLab
     - Native OIDC, but **no administrator mapping** — Enterprise only. Admin
       rights are granted in GitLab by ``root``
   * - Longhorn
     - **No authentication of any kind exists in Longhorn.** An oauth2-proxy
       stands in front of it in full reverse-proxy mode. It is the only
       component SSO adds to the cluster
   * - Garage
     - Does not federate. It speaks S3 and an admin bearer token; its keys stay
       in the vault

Client secrets are **authored**, not generated: written by hand into ``env.sh``
and then *set* on the Keycloak client. That is what keeps SSO from adding an
ordering constraint — the value exists before either end does, so a workload
can start holding a credential for a client Keycloak has not created yet, and
the two begin working together with no restart.

Things that will bite you
=========================

**A new user's email is not "verified".** FreeIPA has no such notion, so
Keycloak imports every federated user with ``emailVerified`` false, and
oauth2-proxy refuses the login with an HTTP 500 that names the address. A
hardcoded-attribute mapper on the federation provider fixes it for the whole
realm — the per-service ``--insecure-oidc-allow-unverified-email`` flag does
not, because the next service behind a proxy meets the same wall.

**A mapper only shapes users as they are imported.** Adding one changes nothing
about users already in the realm; a **full** sync is required.
``triggerChangedUsersSync`` will not do — it selects on the directory's modify
timestamp, which correcting a mapper on Keycloak's side does not touch, so
every user is skipped and it looks exactly like a successful sync.

**A stale LDAP federation provider still exists in the ``master`` realm.**
Nothing prunes it; it should be removed by hand.

**Keycloak needs an x86-64-v2 CPU.** The VMs run ``cpu_type: x86-64-v2-AES``
for this reason. ``/proc/cpuinfo``'s model name still reads ``QEMU Virtual CPU
version 2.5+`` — check the **flags** (``sse4_2``, ``popcnt``, ``ssse3``).

FIPS
====

Dev is outside the FIPS boundary and production is inside it, on the same
manifests: FIPS is a Kustomize **component** at
``apps/keycloak/components/fips`` that a cluster opts into. It needs both
``--features=fips`` and ``KC_FIPS_MODE=strict``, and under strict mode the LDAPS
truststore must be BCFKS rather than the PEM ``dev-lo`` uses.
