"""Shared plumbing for the cluster verification harness.

Nothing here asserts. Each function either talks to the cluster (requests
sessions pinned to the domain CA, kubectl wrapped with the dev config, the
FreeIPA admin channel through the controller's ansible) or returns
structured data a test can assert on.

Conventions:
  - Functions that talk to the cluster raise with a descriptive message on
    failure, so a failing test shows *which* service and *what* it did.
  - No secret appears in module scope; values are read from the
    controller environment (``env.sh``) via :func:`env_var` when needed.
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, urljoin, urlparse

import requests

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

KUBECONFIG_DEFAULT = os.path.expanduser("~/.kube/dev-lo.config")

# The domain CA: every service certificate chains to it, and the whole
# point of this suite is to check the TLS configuration too, so no test
# may verify with verify=False.
CA_FILE_ENV = "DEVLO_CA_FILE"
CA_FILE_DEFAULT = "/usr/local/share/ca-certificates/dev.lo-ca.crt"

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_ANSIBLE_DIR = os.path.join(_REPO_ROOT, "ansible")
_ANSIBLE_CFG = os.path.join(_ANSIBLE_DIR, "ansible.cfg")


def env_var(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(
            f"{name} is not set; run `source ~/.config/rke2lab/env.sh` "
            f"before starting the suite."
        )
    return value


def _ca() -> str:
    return os.environ.get(CA_FILE_ENV, CA_FILE_DEFAULT)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def make_session() -> requests.Session:
    """A requests session pinned to the dev.lo domain CA.

    ``verify`` is a path, not a flag: a missing CA file makes the session
    useless rather than silently permissive.
    """
    ca = _ca()
    if not os.path.exists(ca):
        raise RuntimeError(
            f"domain CA file {ca} not found; export {CA_FILE_ENV} if it "
            f"lives elsewhere on this host"
        )
    session = requests.Session()
    session.verify = ca
    return session


# ---------------------------------------------------------------------------
# kubectl
# ---------------------------------------------------------------------------

def _kubectl_exe() -> str:
    return shutil.which("kubectl") or os.path.expanduser("~/.local/bin/kubectl")


def kubectl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run kubectl against the dev cluster config. KUBECONFIG is set
    explicitly so the suite does not depend on whatever the shell had
    exported."""
    env = dict(os.environ)
    env["KUBECONFIG"] = env.get("KUBECONFIG") or KUBECONFIG_DEFAULT
    proc = subprocess.run([_kubectl_exe(), *args], capture_output=True,
                          text=True, env=env)
    if check and proc.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed: "
                           f"{proc.stderr.strip()[:400]}")
    return proc


def kubectl_json(*args: str):
    proc = kubectl("-o", "json", *args)
    return json.loads(proc.stdout)


def kubectl_popen(*args: str) -> subprocess.Popen:
    """Non-blocking kubectl; the suite uses it only for port-forward,
    which runs until it is killed."""
    env = dict(os.environ)
    env["KUBECONFIG"] = env.get("KUBECONFIG") or KUBECONFIG_DEFAULT
    return subprocess.Popen([_kubectl_exe(), *args],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=env)


# ---------------------------------------------------------------------------
# Keycloak
# ---------------------------------------------------------------------------

KC_BASE = "https://sso.k8s.dev.lo"
KC_REALM = "dev-lo"
KC_AUTH_URL = f"{KC_BASE}/realms/{KC_REALM}/protocol/openid-connect/auth"
KC_TOKEN_URL = f"{KC_BASE}/realms/{KC_REALM}/protocol/openid-connect/token"
KC_ADMIN_API = f"{KC_BASE}/admin/realms/{KC_REALM}"

# Client -> (redirect_uri, env var holding the client secret). Matches
# keycloak_clients_applications in the controller inventory; the secrets
# are the same values the services run with.
SPE: dict[str, tuple[str, str]] = {
    "grafana": ("https://grafana.k8s.dev.lo/login/generic_oauth",
                "OIDC_CLIENT_SECRET_GRAFANA"),
    "longhorn": ("https://longhorn.k8s.dev.lo/oauth2/callback",
                 "OIDC_CLIENT_SECRET_LONGHORN"),
    "openbao": ("http://localhost:8250/oidc/callback",
                "OIDC_CLIENT_SECRET_OPENBAO"),
}
GITLAB_CALLBACK = "https://gitlab.dev.lo/users/auth/openid_connect/callback"


@dataclass
class KeycloakResult:
    """Outcome of a headless Keycloak browser-flow login."""
    ok: bool
    code: Optional[str] = None
    state: Optional[str] = None
    location: Optional[str] = None
    detail: str = ""


def _hidden_fields(page_html: str) -> dict:
    """Hidden input fields of a login form, name -> value.

    Attributes appear in either order across pages, so each ``<input>``
    tag is parsed as a whole rather than with one fixed-order regex.
    """
    fields: dict[str, str] = {}
    for m in re.finditer(r"<input\b[^>]*>", page_html):
        tag = m.group(0)
        if 'type="hidden"' not in tag:
            continue
        name = re.search(r'name="([^"]*)"', tag)
        if not name:
            continue
        value = re.search(r'value="([^"]*)"', tag)
        fields[html.unescape(name.group(1))] = html.unescape(
            value.group(1) if value else "")
    return fields


def _parse_form(page: requests.Response) -> tuple[str, dict]:
    """(form_action, hidden_fields) of the login form on *page*.

    The action is returned exactly as the page wrote it (relative or
    absolute); callers resolve it against the page URL, the way a
    browser would.
    """
    m = re.search(r'<form[^>]+action="([^"]+)"', page.text)
    action = html.unescape(m.group(1)) if m else str(page.url)
    return action, _hidden_fields(page.text)


def _submit_login_form(session: requests.Session,
                       page: requests.Response,
                       username: str, password: str
                       ) -> tuple[Optional[KeycloakResult],
                                  Optional[requests.Response]]:
    """POST the form on *page*.

    Returns (result, None) when the flow ends, or (None, next_page) when
    the response is another challenge page carrying its own form.
    """
    action, hidden = _parse_form(page)
    body = dict(hidden)
    body["username"] = username
    body["password"] = password
    body["credentialId"] = hidden.get("credentialId", "")
    target = urljoin(str(page.url), action)
    nxt = session.post(target, data=body, allow_redirects=False, timeout=30)
    if nxt.status_code in (302, 303):
        loc = nxt.headers.get("Location", "")
        if "login-actions" not in loc:
            qs = parse_qs(urlparse(loc).query)
            if qs.get("code", [""])[0]:
                return KeycloakResult(ok=True, code=qs["code"][0],
                                      state=qs.get("state", [None])[0],
                                      location=loc), None
            return (KeycloakResult(ok=False, location=loc,
                                   detail="redirect out of Keycloak "
                                          "without a code parameter"),
                    None)
        # A challenge page: follow its Location to the form it renders.
        next_page = session.get(urljoin(str(page.url), loc),
                                allow_redirects=False, timeout=30)
        return None, next_page
    # 200 in place: an error message or a challenge form rendered here.
    return None, nxt


def keycloak_login(session: requests.Session, username: str, password: str,
                   client_id: Optional[str] = None,
                   redirect_uri: Optional[str] = None,
                   auth_url: Optional[str] = None) -> KeycloakResult:
    """Drive the realm's browser login flow for a client's redirect.

    ok=True with code/state when the browser would be redirected out of
    Keycloak; ok=False with a detail string when the flow stayed on
    Keycloak (bad credentials, no client role, ...).

    This is the real browser flow: reach the authorization endpoint,
    submit the login form, then the second challenge form when Keycloak
    offers one. The session must be a fresh, cookie-persistent
    requests.Session — the KCB cookies are what keep the flow from
    400-ing.

    Two entry points:
    * ``auth_url`` — the exact authorization URL the *service* issued in
      its own 302 (carrying the service's state/PKCE parameters). Use
      this for browser-callback flows (Grafana, GitLab): the service
      validates the state it set, so the login must ride the service's
      own request, not a fabricated one.
    * ``client_id``/``redirect_uri`` — the helper builds its own
      authorization request. Use this for the pure token-grant flows
      where there is no service session to keep stateful.
    """
    if auth_url is not None:
        resp = session.get(auth_url, allow_redirects=False, timeout=30)
    else:
        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": "openid profile email",
            "state": "verify-state",
        }
        resp = session.get(KC_AUTH_URL, params=params,
                           allow_redirects=False, timeout=30)
    # The authorization request may 302 to the login page first; follow at
    # most that one hop, never past the login form itself.
    if resp.status_code in (302, 303):
        hop = resp.headers.get("Location", "")
        if "login" in hop or hop.startswith("/"):
            resp = session.get(urljoin(KC_BASE, hop) if hop.startswith("/")
                               else hop, allow_redirects=False, timeout=30)
    if resp.status_code in (302, 303):
        # An existing session already holds an out-redirect (should not
        # happen for an anonymous session, but the flow ends here either
        # way).
        loc = resp.headers.get("Location", "")
        qs = parse_qs(urlparse(loc).query)
        if qs.get("code", [""])[0]:
            return KeycloakResult(ok=True, code=qs["code"][0],
                                  state=qs.get("state", [None])[0],
                                  location=loc)
        return KeycloakResult(ok=False, location=loc,
                              detail="redirected away from the login flow "
                                     "without a code")

    # Login form, then (at most) the second challenge form.
    page = resp
    for _ in range(2):
        result, page = _submit_login_form(session, page, username, password)
        if result is not None:
            return result
        if page is None:
            break

    return KeycloakResult(ok=False,
                          location=str(page.url) if page is not None else "",
                          detail="flow stayed on a Keycloak login page")


def exchange_code(client_id: str, code: str, redirect_uri: str) -> dict:
    """Authorization-code grant against the realm token endpoint. The
    client secret is the environment value the service itself runs with,
    so a failure here is broken client wiring, not just a bad username."""
    secret = env_var(SPE[client_id][1])
    resp = requests.post(
        KC_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": secret,
            "redirect_uri": redirect_uri,
        },
        verify=_ca(), timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"token exchange for {client_id} failed "
                           f"({resp.status_code}): {resp.text[:300]}")
    return resp.json()


def access_token_claims(access_token: str) -> dict:
    """Decode a JWT payload without re-validating the signature (the
    token endpoint already did)."""
    payload = access_token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def keycloak_admin_token() -> str:
    """Bearer token for the admin API.

    admin-cli is a public client with direct grants, so this is a plain
    resource-owner password grant for the admin account — the same grant
    the keycloak_clients role uses. The account is local to the *master*
    realm (Keycloak's bootstrap realm), not the app realm, so the grant
    must be made against master even though the token is then used on the
    dev-lo admin API.
    """
    resp = requests.post(
        f"{KC_BASE}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": "admin",
            "password": env_var("KEYCLOAK_ADMIN_PASSWORD"),
        },
        verify=_ca(), timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"admin token grant failed "
                           f"({resp.status_code}): {resp.text[:300]}")
    return resp.json()["access_token"]


def keycloak_admin_get(path: str) -> requests.Response:
    """GET on the realm admin API with a fresh admin bearer token."""
    session = make_session()
    token = keycloak_admin_token()
    return session.get(f"{KC_ADMIN_API}{path}",
                       headers={"Authorization": f"Bearer {token}"},
                       timeout=30)


def keycloak_clear_user_cache() -> None:
    """Flush Keycloak's locally imported FreeIPA user cache for this realm.

    With importEnabled, a user is imported the moment they appear in the
    directory — which is between `ipa user-add` and `ipa passwd`, so the
    cache holds a user whose stored credential does not match the password
    the harness just set. The realm-level clear (the same endpoint the
    keycloak_ldap role uses) forces a re-import on the next lookup; without
    it, a freshly provisioned user can fail to log in until the 60s
    cache TTL expires on its own.
    """
    session = make_session()
    token = keycloak_admin_token()
    resp = session.post(f"{KC_ADMIN_API}/clear-user-cache",
                       headers={"Authorization": f"Bearer {token}"},
                       timeout=30)
    assert resp.status_code == 204, (
        f"clear-user-cache gave {resp.status_code}: {resp.text[:200]}")


# ---------------------------------------------------------------------------
# Grafana
# ---------------------------------------------------------------------------

GRAFANA_BASE = "https://grafana.k8s.dev.lo"


def grafana_credentials() -> tuple[str, str]:
    """The local Grafana admin — the break-glass account held in OpenBao
    (kv/grafana). The same credentials a human uses when SSO is down."""
    root = env_var("OPENBAO_ROOT_TOKEN")
    session = make_session()
    resp = session.get(f"{BAO_BASE}/v1/kv/data/grafana",
                       headers={"X-Vault-Token": root}, timeout=30)
    resp.raise_for_status()
    data = resp.json()["data"]["data"]
    return data["username"], data["password"]


def grafana_sso_session(username: str, password: str):
    """Establish a Grafana SSO session and return (info, session).

    Drives the browser flow against Grafana's generic_oauth endpoint, then
    returns the /api/user payload plus the authenticated session. The
    session is needed for the follow-up /api/org/users lookup, which is
    how a role assertion can prove the mapping actually ran (the
    role_attribute_path maps the token's ``roles`` claim onto the
    *org role*; this estate sets allow_assign_grafana_admin=false, so the
    global isGrafanaAdmin flag is never the thing the mapping produces).
    """
    session = make_session()
    resp = session.get(f"{GRAFANA_BASE}/login/generic_oauth",
                       allow_redirects=False, timeout=30)
    assert resp.status_code in (302, 303), (
        f"generic_oauth gave {resp.status_code}, expected a redirect to "
        f"Keycloak — Grafana's OAuth entry point is not configured"
    )
    # Grafana set the state (and OAuth callback URL) in this 302; the
    # login must ride Grafana's own authorization request or the
    # callback will reject the foreign state.
    auth_url = urljoin(str(resp.url), resp.headers["Location"])
    result = keycloak_login(session, username, password, auth_url=auth_url)
    assert result.ok, (
        f"Keycloak login failed: {result.detail} (loc={result.location})"
    )

    # The out-redirect already carries Grafana's expected code and state.
    final = session.get(result.location, allow_redirects=True, timeout=60)
    assert final.status_code == 200, f"post-login page gave {final.status_code}"

    api = session.get(f"{GRAFANA_BASE}/api/user", timeout=30)
    if api.status_code == 401:
        raise RuntimeError(
            "Grafana rejected the SSO session (grafana_session cookie not "
            "established or the OAuth callback misfired)"
        )
    api.raise_for_status()
    return api.json(), session


def grafana_org_role(admin_username: str, admin_password: str,
                     target_login: str) -> str:
    """The org role a SSO user's login row carries (e.g. 'Viewer').

    /api/org/users lists the org's members with their per-org roles, but
    it needs an Admin/Editor org role to call, so the lookup rides the
    admin-tier session. The row matching ``target_login`` carries the
    role the role_attribute_path evaluated to at that user's last SSO
    login.
    """
    _, session = grafana_sso_session(admin_username, admin_password)
    members = session.get(f"{GRAFANA_BASE}/api/org/users", timeout=30)
    if members.status_code == 403:
        raise RuntimeError(
            f"{admin_username} cannot list /api/org/users (403) — the "
            f"admin-tier SSO session did not map to the Admin org role"
        )
    members.raise_for_status()
    for row in members.json():
        login = row.get("userLogin") or row.get("login")
        email = row.get("userEmail") or row.get("email")
        if login == target_login or email == target_login:
            return row["role"]
    raise RuntimeError(
        f"no /api/org/users row for {target_login!r}; "
        f"members: {[(r.get('userLogin') or r.get('login'), r['role'])
                     for r in members.json()]}"
    )


# ---------------------------------------------------------------------------
# GitLab
# ---------------------------------------------------------------------------

GITLAB_BASE = "https://gitlab.dev.lo"


def gitlab_sso_login(username: str, password: str) -> dict:
    """OIDC browser flow against GitLab; returns /api/v4/user on success.

    The sign-in page's OIDC form posts an authenticity_token to
    /users/auth/openid_connect; GitLab then exchanges the code from
    Keycloak and establishes the session.
    """
    session = make_session()
    resp = session.get(f"{GITLAB_BASE}/users/sign_in",
                       allow_redirects=False, timeout=30)
    if resp.status_code in (302, 303):
        resp = session.get(resp.headers["Location"], allow_redirects=False,
                           timeout=30)
    assert resp.status_code == 200, f"sign_in gave {resp.status_code}"
    form = re.search(r'<form[^>]+action="([^"]*openid_connect[^"]*)"',
                     resp.text)
    token = re.search(r'name="authenticity_token"[^>]*value="([^"]*)"',
                      resp.text)
    assert form and token, "GitLab sign-in form has no openid_connect form"

    target = urljoin(GITLAB_BASE, form.group(1))
    resp2 = session.post(
        target,
        data={"authenticity_token": html.unescape(token.group(1))},
        allow_redirects=False, timeout=30)
    assert resp2.status_code in (302, 303), (
        f"openid_connect POST gave {resp2.status_code}: {resp2.text[:200]}"
    )
    # This 302 is GitLab's own authorization request — it carries the
    # state/PKCE parameters GitLab will validate at the callback, so the
    # login must ride GitLab's URL, not a fabricated one.
    auth_url = urljoin(str(resp2.url), resp2.headers["Location"])
    result = keycloak_login(session, username, password, auth_url=auth_url)
    assert result.ok, (
        f"Keycloak login failed: {result.detail} (loc={result.location})"
    )

    # The out-redirect already carries GitLab's expected code and state.
    final = session.get(result.location, allow_redirects=True, timeout=60)
    assert final.status_code == 200, f"post-login page gave {final.status_code}"
    api = session.get(f"{GITLAB_BASE}/api/v4/user", timeout=30)
    api.raise_for_status()
    return api.json()


# ---------------------------------------------------------------------------
# OpenBao
# ---------------------------------------------------------------------------

BAO_BASE = "https://bao.k8s.dev.lo"


def bao_api(path: str, token: Optional[str] = None, method: str = "GET",
            json_body: Optional[dict] = None,
            expect: tuple = (200,)) -> requests.Response:
    """A call to the OpenBao API; root token unless a token is given."""
    session = make_session()
    headers = {"X-Vault-Token": token or env_var("OPENBAO_ROOT_TOKEN")}
    kwargs: dict = {"headers": headers, "timeout": 30}
    if json_body is not None:
        kwargs["json"] = json_body
    resp = session.request(method, f"{BAO_BASE}{path}", **kwargs)
    if resp.status_code not in expect:
        raise RuntimeError(
            f"OpenBao {method} {path} gave {resp.status_code} "
            f"({resp.text[:200]})"
        )
    return resp


def bao_kv_data(path: str) -> dict:
    resp = bao_api(f"/v1/kv/data/{path}")
    return resp.json()["data"]["data"]


def bao_kv_keys_present(expected: dict[str, list[str]]) -> None:
    """Assert each expected KV path exists and carries the listed keys."""
    missing: list[str] = []
    for path, keys in expected.items():
        try:
            data = bao_kv_data(path)
        except RuntimeError as exc:
            missing.append(f"{path}: {exc}")
            continue
        for key in keys:
            if key not in data:
                missing.append(f"{path}: missing key {key}")
    assert not missing, "OpenBao KV gaps: " + "; ".join(missing)


# ---------------------------------------------------------------------------
# FreeIPA admin channel
# ---------------------------------------------------------------------------

# Keycloak's LDAP provider caches group membership (cachePolicy DEFAULT,
# 60s TTL); the suite waits out the cache after provisioning so no test
# races the sync.
_IPA_SYNC_SETTLE_S = 80


def _ipa_admin(*commands: str, strict: bool = True) -> None:
    """Run ipa commands as admin inside the FreeIPA container.

    Mirrors the pattern the roles use: ansible into core01, pipe the
    admin password (base64, from the environment) into the container,
    kinit, then run the commands. The password never appears in argv.
    *strict* (provisioning) fails the channel on any command failure;
    the teardown path passes strict=False so a missing user does not
    mask the test result.
    """
    pwd_b64 = base64.b64encode(env_var("FREEIPA_ADMIN_PASSWORD").encode()).decode()
    if strict:
        body = "; ".join(commands)
        inner = f"set -e; cat|base64 -d|kinit admin >/dev/null; {body}"
    else:
        body = "; ".join(f"{c} || true" for c in commands)
        inner = f"cat|base64 -d|kinit admin >/dev/null; {body}"
    remote = (
        f"printf '%s' '{pwd_b64}' | docker exec -i freeipa-server "
        f"bash -c '{inner}'"
    )
    env = dict(os.environ)
    env["ANSIBLE_CONFIG"] = _ANSIBLE_CFG
    proc = subprocess.run(
        ["ansible", "core01", "-m", "shell", "-a", remote],
        capture_output=True, text=True, env=env, cwd=_ANSIBLE_DIR,
    )
    if proc.returncode != 0:
        out = (proc.stdout + proc.stderr).replace(pwd_b64, "<redacted>")
        raise RuntimeError(f"ipa admin channel failed:\n{out[:1500]}")


def ipa_user_add(name: str, email: str, password: str) -> None:
    _ipa_admin(
        f"ipa user-add {name} --first=Verify --last=Test --email={email}",
        f"echo '{password}' | ipa passwd {name}",
    )


def ipa_group_add_member(group: str, user: str) -> None:
    _ipa_admin(f"ipa group-add-member {group} --users={user}")


def ipa_group_remove_member(group: str, user: str) -> None:
    _ipa_admin(f"ipa group-remove-member {group} --users={user}",
               strict=False)


def ipa_user_del(name: str) -> None:
    _ipa_admin(f"ipa user-del {name}", strict=False)


# ---------------------------------------------------------------------------
# Ephemeral FreeIPA users
# ---------------------------------------------------------------------------

@dataclass
class TestUser:
    name: str
    email: str
    password: str
    groups: list = field(default_factory=list)

    def add_groups(self, *groups: str) -> None:
        self.groups.extend(g for g in groups if g not in self.groups)


def make_test_users() -> tuple[TestUser, TestUser]:
    """Fresh, unguessable credentials for the run.

    ``test`` joins only the user-level SSO groups; ``test.adm`` joins every
    admin-level group plus keycloak-admins. Passwords are per-run, so a
    failure cannot be replayed from a captured log.
    """
    user = TestUser(name="test", email="verify-test@dev.lo",
                    password=secrets.token_urlsafe(24))
    user.add_groups("gitlab-users", "grafana-users", "longhorn-users",
                    "openbao-users")
    adm = TestUser(name="test.adm", email="verify-test-adm@dev.lo",
                   password=secrets.token_urlsafe(24))
    adm.add_groups("gitlab-admins", "grafana-admins", "longhorn-admins",
                   "openbao-admins", "keycloak-admins")
    return user, adm


def provision_user(u: TestUser) -> None:
    # Self-heal: a run whose teardown failed may have left this user
    # behind. Remove it (no-op if absent) so the strict add below works.
    ipa_user_del(u.name)
    ipa_user_add(u.name, u.email, u.password)
    for group in u.groups:
        ipa_group_add_member(group, u.name)


def remove_user(u: TestUser) -> None:
    for group in u.groups:
        ipa_group_remove_member(group, u.name)
    ipa_user_del(u.name)


def wait_for_keycloak_sync() -> None:
    """Sleep out Keycloak's LDAP membership cache (60s TTL)."""
    time.sleep(_IPA_SYNC_SETTLE_S)
