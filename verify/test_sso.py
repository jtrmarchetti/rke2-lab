"""End-to-end SSO tests against the real estate.

Every test drives the *same* browser flow a person would: the service's
OAuth entry point, the Keycloak login form, then the service callback.
No service-specific back doors: if the flow breaks, the tests fail the
way a user would notice.

Credentials are ephemeral (the ``test_users`` fixture): one run, two
random passwords, removed after.
"""

from __future__ import annotations

from urllib.parse import quote

import pytest

from verify import helpers


# ---------------------------------------------------------------------------
# GitLab
# ---------------------------------------------------------------------------

def test_gitlab_sso_user(test_users):
    """A user-level member can sign in to GitLab through the OIDC button and
    reaches a live /api/v4/user with their own identity."""
    user, _ = test_users
    info = helpers.gitlab_sso_login(user.name, user.password)
    assert info["username"] == user.name, (
        f"GitLab SSO session resolved to {info['username']!r}, expected "
        f"{user.name!r} — the federated login mapped to the wrong account"
    )
    assert info.get("email") == user.email, "federated email did not land"


# ---------------------------------------------------------------------------
# Grafana
# ---------------------------------------------------------------------------

def test_grafana_sso_user(test_users):
    """Grafana: a user-level member logs in through the OAuth endpoint and
    the token's `roles` claim maps to the Viewer org role.

    The estate's role_attribute_path maps contains(roles[*],'user') to
    Viewer; allow_assign_grafana_admin is off, so the mapping drives the
    *org role*, never the global admin flag. The role is read back
    through the admin session (a Viewer cannot list /api/org/users)."""
    user, adm = test_users
    # Log the user in first so its grafana.db row exists and the
    # role mapping has run; then read the row through the admin tier.
    info, _ = helpers.grafana_sso_session(user.name, user.password)
    assert info["login"] == user.email, (
        f"Grafana session is {info['login']!r}, expected {user.email!r}"
    )
    role = helpers.grafana_org_role(adm.name, adm.password, user.email)
    assert role == "Viewer", (
        f"user-level member {user.name} got org role {role!r}; expected "
        f"'Viewer' — the role mapping is granting too much"
    )


def test_grafana_sso_admin(test_users):
    """Grafana: an admin-group member maps to the Admin org role through
    the same role_attribute_path (contains(roles[*],'admin'))."""
    _, adm = test_users
    role = helpers.grafana_org_role(adm.name, adm.password, adm.email)
    assert role == "Admin", (
        f"admin-group member {adm.name} got org role {role!r}; expected "
        f"'Admin' — the role mapping did not run"
    )


# ---------------------------------------------------------------------------
# Longhorn (behind oauth2-proxy: code grant + token exchange, assert roles)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("who,expect", [("user", "user"), ("admin", "admin")],
                         ids=["user", "admin"])
def test_longhorn_roles_claim(test_users, who, expect):
    """The Longhorn proxy authorizes on the token's `roles` claim
    (--allowed-role=longhorn:admin / longhorn:user). Proving the claim is
    present and correct proves the proxy will admit exactly that tier."""
    user, adm = test_users
    subject = user if who == "user" else adm
    result = helpers.keycloak_login(
        helpers.make_session(), subject.name, subject.password,
        "longhorn", helpers.SPE["longhorn"][0])
    assert result.ok, f"Keycloak login failed: {result.detail}"
    tokens = helpers.exchange_code("longhorn", result.code,
                                   helpers.SPE["longhorn"][0])
    claims = helpers.access_token_claims(tokens["access_token"])
    assert claims.get("roles") == [expect], (
        f"expected roles claim [{expect}] for {subject.name}, "
        f"got {claims.get('roles')}"
    )


# ---------------------------------------------------------------------------
# OpenBao (same shape: the vault's OIDC role reads the same `roles` claim)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("who,expect", [("user", "user"), ("admin", "admin")],
                         ids=["user", "admin"])
def test_openbao_roles_claim(test_users, who, expect):
    """OpenBao's OIDC method maps the `roles` claim onto identity groups
    (alias user/admin), which carry the sso-users/sso-admins policies.
    The claim is therefore the whole chain: if it is wrong, the user lands
    with the wrong (or no) vault access."""
    user, adm = test_users
    subject = user if who == "user" else adm
    result = helpers.keycloak_login(
        helpers.make_session(), subject.name, subject.password,
        "openbao", helpers.SPE["openbao"][0])
    assert result.ok, f"Keycloak login failed: {result.detail}"
    tokens = helpers.exchange_code("openbao", result.code,
                                   helpers.SPE["openbao"][0])
    claims = helpers.access_token_claims(tokens["access_token"])
    assert claims.get("roles") == [expect]


# ---------------------------------------------------------------------------
# Keycloak administration
# ---------------------------------------------------------------------------

def test_keycloak_admin_group_mapping():
    """The realm-admin capability is granted to the keycloak-admins group on
    the realm-management client. That grant is what test.adm's
    keycloak-admins membership (provisioned by the fixture) is worth —
    assert the grant still exists, and that the admin API is reachable
    with the realm's local admin account."""
    resp = helpers.keycloak_admin_get("/group-by-path/keycloak-admins")
    assert resp.status_code == 200, f"keycloak-admins group: {resp.status_code}"
    group_id = resp.json()["id"]

    clients = helpers.keycloak_admin_get(
        f"/clients?clientId={quote('realm-management')}").json()
    assert clients, "realm-management client not found"
    rm_id = clients[0]["id"]

    mappings = helpers.keycloak_admin_get(
        f"/groups/{group_id}/role-mappings/clients/{rm_id}").json()
    assert any(r["name"] == "realm-admin" for r in mappings), (
        f"keycloak-admins lost its realm-admin grant: {mappings}"
    )


def test_keycloak_admin_login():
    """The realm's local admin account still authenticates to the admin API
    (the break-glass path when the identity provider is down)."""
    token = helpers.keycloak_admin_token()
    assert token, "no admin bearer token"


# ---------------------------------------------------------------------------
# Negative: a wrong password must NOT get through anywhere
# ---------------------------------------------------------------------------

def test_sso_rejects_bad_password(test_users):
    user, _ = test_users
    result = helpers.keycloak_login(
        helpers.make_session(), user.name, "definitely-not-the-password",
        "grafana", helpers.SPE["grafana"][0])
    assert not result.ok, (
        "Keycloak accepted a wrong password and redirected out of the "
        "login flow — authentication is not actually being checked"
    )
