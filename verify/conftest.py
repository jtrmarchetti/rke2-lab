"""Shared fixtures for the cluster verification suite.

The suite talks to a running estate; nothing here creates cluster state.
The one piece of state the SSO tests need — two FreeIPA users that exist
for the run and are removed after it — is built and torn down by a
session-scoped fixture so the teardown runs even when a test fails.
"""

from __future__ import annotations

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
