"""OpenBao content tests: the vault holds exactly the runtime secrets the
estate expects, and the two OIDC policies carry the access tiers that
correspond to the FreeIPA groups.

All reads go through the root token (the offline way-in the roles
preserve), never through a secret that the vault itself holds.
"""

from __future__ import annotations

from verify import helpers

# What openbao_secrets writes. The `garage` entry is conditional on Garage
# having minted an S3 key (garage_init records it in env.sh), so it is
# asserted separately and its absence is a legitimate state.
_EXPECTED_KV = {
    "keycloak": ["admin-password", "db-password"],
    "garage-cluster": ["admin-token", "metrics-token", "rpc-secret"],
    "grafana": ["username", "password"],
    "oidc-grafana": ["client-secret"],
    "oidc-longhorn": ["client-secret", "cookie-secret"],
}


def test_expected_secrets_present():
    """Every runtime secret the estate needs to cold-rebuild lives in the
    vault. A missing key here is the difference between `flux reconcile`
    working on a rebuild and someone hand-typing a credential they can no
    longer find."""
    helpers.bao_kv_keys_present(_EXPECTED_KV)


def test_garage_s3_secret_present():
    """The Garage S3 key is written by garage_init and read by Loki/Tempo.
    If it is missing, garage_init never recorded a minted key and the
    object store's own credentials are gone."""
    helpers.bao_kv_keys_present({"garage": ["access_key", "secret_key"]})


def test_sso_policies_defined():
    """The two ACL policies behind the OIDC login path exist and are
    non-empty."""
    for policy in ("sso-admins", "sso-users"):
        resp = helpers.bao_api(f"/v1/sys/policies/acl/{policy}")
        text = resp.json()["data"]["policy"]
        assert text.strip(), f"policy {policy!r} is empty"


def test_admin_policy_is_scoped():
    """The admin policy may read and write the kv tree but must not grant
    the root-token paths: seal/unseal, rekey, step-down, or auth
    management. That invariant is what keeps the vault openable when
    Keycloak itself is down."""
    resp = helpers.bao_api("/v1/sys/policies/acl/sso-admins")
    text = resp.json()["data"]["policy"]
    for forbidden in ("sys/seal", "sys/unseal", "sys/rekey", "sys/step-down",
                      '"auth/*"', '"sudo"'):
        assert forbidden not in text, (
            f"sso-admins grants {forbidden} — the OIDC tier can seal or "
            f"rekey the vault it holds break-glass credentials for"
        )


def test_root_token_still_has_root_policy():
    """The offline way-in survives: the root token must still carry the
    root policy. If the OIDC work replaced it rather than sitting beside
    it, the vault's one true back door is gone."""
    resp = helpers.bao_api("/v1/auth/token/lookup-self")
    policies = resp.json()["data"]["policies"]
    assert "root" in policies, (
        f"root token carries {policies} — the root policy is missing and "
        f"the vault has no offline access path"
    )
