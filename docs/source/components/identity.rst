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
is broken. No clients live there, and the only federation permitted is the
filtered one described below — ``keycloak_break_glass`` removes anything else
that appears.

Administering Keycloak
======================

Two ways in, and they are not interchangeable.

.. list-table::
   :header-rows: 1
   :widths: 20 34 46

   * - Who
     - Where
     - What they can do
   * - ``keycloak-admins`` in FreeIPA
     - ``/admin/dev-lo/console`` and ``/admin/master/console``
     - Everything. ``realm-admin`` on ``dev-lo``, and the ``admin`` realm role
       in ``master`` — so realms can be created and master itself edited.
       Signed in as themselves, with their own domain password
   * - Local ``admin``
     - ``https://sso.k8s.dev.lo/admin/master/console``
     - The same, and it keeps working when FreeIPA does not. One shared
       password, in ``env.sh`` and ``kv/keycloak``

Everyday administration is the first row, and it is granted the way every other
grant in this lab is granted:

.. code-block:: console

   $ ipa group-add-member keycloak-admins --users alice

``keycloak-admins`` is the one group with no ``-users`` counterpart. Every
account in the domain can already sign in to Keycloak — that is what being
federated is — so there would be nothing for a user tier to grant.

The second row is break-glass and is meant to be rare: it is one shared
password that names nobody in an audit log. What it buys is availability — it
does not depend on FreeIPA, so it is the way in on the day the directory is
what is broken.

Master is federated, and narrowly
---------------------------------

Granting the ``admin`` realm role in ``master`` needs the group to exist in
``master``, which needs a federation there. That is the arrangement the realm
split exists to avoid, so it is narrowed rather than granted whole. The
provider in ``master`` carries two filters:

.. code-block:: text

   users:  (&(!(nsAccountLock=TRUE))(memberOf=cn=keycloak-admins,cn=groups,cn=accounts,dc=dev,dc=lo))
   groups: (cn=keycloak-admins)

So ``master`` holds the people who administer Keycloak and the one group that
says so — not the domain. Keycloak's guidance is that master holds
administrators rather than application users and business identities, and a
filtered provider is on the right side of that; an unfiltered one is not.
``keycloak_ldap`` refuses to touch ``master`` unless both filters are set, and
``keycloak_break_glass`` will delete any provider there that is not named in
``keycloak_break_glass_allowed_federation``.

.. warning::

   This is a real trade and it is worth knowing which half you gave up. An
   account in ``keycloak-admins`` can now disable or delete the local
   ``admin`` — master's account is no longer protected *from* the directory.
   What survives is that it stays local and unfederated, so it still works
   when the directory does not.

.. note::

   The account Keycloak creates from ``KC_BOOTSTRAP_ADMIN_*`` is **temporary**:
   it carries the user attribute ``is_temporary_admin`` and the console warns on
   every session. ``keycloak_break_glass`` removes that attribute, because the
   password here has a source of record rather than being something someone
   typed to get started. It takes three writes, not one — the attribute is
   unmanaged, and a realm ignores unmanaged attributes on write unless its
   ``unmanagedAttributePolicy`` is open, so the role opens it, writes, and
   restores it.

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

**A group added in FreeIPA is invisible until Keycloak's user cache is
dropped.** This is the one that looks least like its cause. Neither the group
sync nor the user sync invalidates the cache holding an already-imported user,
so ``ipa group-add-member keycloak-admins --users alice`` leaves Alice listed
as a member when you ask the group who is in it — that query goes to the
directory — and absent from her own group list, which is served from the cache.
Every role mapping derived from the group is missing with it, and she is
refused at the console with *You do not have permission to access this
resource* against a realm where every mapping reads back correctly.
``keycloak_ldap`` now posts ``clear-user-cache`` after its syncs; by hand it is
``Realm settings → Sessions`` or a ``POST`` to
``/admin/realms/<realm>/clear-user-cache``.

**An *unfiltered* federation provider in ``master`` is not a leftover to live
with.** One sat there from before the realm split, still enabled, still binding
over plain LDAP, importing every domain account into the realm that administers
every other realm. ``keycloak_break_glass`` removes any provider there that is
not explicitly allowed, and the accounts an absent provider left behind.

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
