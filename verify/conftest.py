"""Shared fixtures for the cluster verification suite.

The suite talks to a running estate; nothing here creates cluster state.
The one piece of state the SSO tests need — two FreeIPA users that exist
for the run and are removed after it — is built and torn down by a
session-scoped fixture so the teardown runs even when a test fails.
"""

from __future__ import annotations

import secrets

import pytest

from . import helpers


@pytest.fixture(scope="session")
def test_users() -> tuple[helpers.TestUser, helpers.TestUser]:
    """Provision the two ephemeral FreeIPA users, hand them out, and remove
    both on teardown regardless of test outcome.

    `user` is the plain user; `adm` carries every admin group plus
    keycloak-admins. Passwords are per-run. Provisioning waits out
    Keycloak's LDAP membership cache, because the SSO assertions read
    group membership through the realm, not FreeIPA directly.
    """
    user, adm = helpers.make_test_users()
    helpers.provision_user(user)
    helpers.provision_user(adm)
    helpers.wait_for_keycloak_sync()
    # importEnabled means Keycloak may have cached the user between
    # user-add and passwd with a stale credential; flush it so the
    # SSO assertions see the password the harness just set.
    helpers.keycloak_clear_user_cache()
    try:
        yield user, adm
    finally:
        # Remove in reverse order; remove_user tolerates a user that was
        # never added (e.g. the fixture failed mid-provision).
        for u in (adm, user):
            try:
                helpers.remove_user(u)
            except Exception:
                # Teardown is best-effort: a hung FreeIPA channel should not
                # mask the real test result, but it must be visible.
                print(f"WARNING: teardown for {u.name!r} failed")


@pytest.fixture(scope="session")
def hubble_tiers() -> tuple[helpers.TestUser, helpers.TestUser,
                            helpers.TestUser]:
    """The Hubble UI's three identities, driven through the live hubble-auth
    proxy: a hubble-users member, a hubble-admins member, and a user who is a
    federated SSO user but in no hubble group (the denied tier).

    The first two are the estate's test_users; the third exists only for the
    denial proof, so this fixture provisions exactly that one user and tears
    it down on its own.
    """
    user, adm = helpers.make_test_users()
    user.add_groups("hubble-users")
    adm.add_groups("hubble-admins")
    nobody = helpers.TestUser(name="hub-sso-none-test",
                              email="hub-sso-none@dev.lo",
                              password=secrets.token_urlsafe(24))
    nobody.add_groups("gitlab-users")  # an SSO user, but no hubble group
    for u in (user, adm, nobody):
        helpers.provision_user(u)
    helpers.wait_for_keycloak_sync()
    helpers.keycloak_clear_user_cache()
    try:
        yield user, adm, nobody
    finally:
        for u in (nobody, adm, user):
            try:
                helpers.remove_user(u)
            except Exception:
                print(f"WARNING: teardown for {u.name!r} failed")
