"""Traefik dashboard: the edge-exposure lane and its SSO front.

The estate's rke2-traefik HelmChartConfig owns the backend surface:
``service.additionalServices.dashboard`` renders the rke2-traefik-dashboard
ClusterIP (the plain-HTTP dashboard entrypoint, container 8080), and the
Gateway-API provider exposes it only through the platform Gateway's
``traefik`` listener — the LoadBalancer Service stays web/websecure
(80/443), so the dashboard is never a direct edge port.

The gitops tree (apps/traefik-dashboard) owns the edge surface: the
listener on the platform Gateway (edge TLS from the cert-manager shim's
traefik-edge-tls, like every other edge host) and the HTTPRoute whose
root rule redirects ``/`` to ``/dashboard/`` at the TLS-terminating
listener. Doing the redirect there — not on the plain-HTTP entrypoint
behind it — is what keeps the Location https: the entrypoint's own
internal dashboard redirect is bound to the scheme it sees, which is
plain HTTP behind the edge.

The dashboard has no authentication of its own, so the route's catch-all
rule lands on the traefik-auth oauth2-proxy (the estate's hubble-auth
pattern): an anonymous request is redirected to the Keycloak ``traefik``
client, and a member of traefik-users or traefik-admins is admitted onto
the dashboard behind the proxy. The dashboard carries no admin tier of
its own, so both tiers are admitted to view it; the two tiers are
enforced at the edge, on the token's ``roles`` claim, not inside the UI.

Like test_mtls and test_hubble, this module is a configuration-surface
gate: the HCC-shape tests skip while the rke2-traefik HCC carries no
dashboard block, the SSO tests skip while the traefik-auth proxy is not
deployed, and everything enforces on every warm build after, so a
rollback or drift fails fast.
"""

from __future__ import annotations

import yaml

import pytest

from verify import helpers

TRAEFIK_DASH_BASE = "https://traefik.k8s.dev.lo"


def _dashboard_exposed() -> bool:
    """Whether the rke2-traefik HelmChartConfig carries the dashboard
    Service, i.e. this lane has been deployed. The estate exposes the
    dashboard only through that one HCC block, so its presence is the
    feature being live."""
    try:
        hcc = helpers.kubectl_json("get", "helmchartconfig", "-n",
                                   "kube-system", "rke2-traefik")
        values = yaml.safe_load(hcc["spec"]["valuesContent"]) or {}
        return "dashboard" in (values.get("service", {})
                               .get("additionalServices", {}))
    except (RuntimeError, KeyError):
        # No reachable HCC means the cluster itself is not up; the rest of
        # the suite already needs it, so do not silently skip.
        raise


pytestmark = pytest.mark.skipif(
    not _dashboard_exposed(),
    reason=("the rke2-traefik HelmChartConfig carries no dashboard block: "
            "the dashboard-exposure lane has not landed on this estate "
            "yet. This suite enforces the post-lane configuration "
            "surface and skips until the build lands it."))


def _traefik_values() -> dict:
    """The rke2-traefik HelmChartConfig valuesContent, parsed to a dict.

    The HCC carries valuesContent as a YAML *string*; the deep-merge RKE2
    applies is exactly that block, so asserting on the parsed structure
    is asserting on what the chart actually renders."""
    hcc = helpers.kubectl_json("get", "helmchartconfig", "-n", "kube-system",
                               "rke2-traefik")
    return yaml.safe_load(hcc["spec"]["valuesContent"])


def test_traefik_dashboard_hcc_pins_the_surface():
    """The HCC values block is the whole backend surface: the dashboard
    Service (a ClusterIP, off the LoadBalancer pool), the dashboard
    entrypoint off the LB but exposed for the extra Service, and the
    G7-scoped forwarded-headers trust on the plain-HTTP entrypoint. A
    rollback that drops any one of these re-exposes the dashboard as a
    bare edge port, re-404s it, or re-opens X-Forwarded-* to 0.0.0.0/0."""
    values = _traefik_values()
    svc = values["service"]
    assert svc["additionalServices"]["dashboard"]["type"] == "ClusterIP", (
        "the dashboard Service is not pinned to ClusterIP: it would claim "
        "an external address from the LoadBalancer pool")
    ports = values["ports"]
    assert ports["traefik"]["expose"]["default"] is False, (
        "the dashboard entrypoint is exposed on the default LB Service: "
        "the dashboard would be a direct edge port, outside the platform "
        "Gateway's listener/HTTPS boundary")
    assert ports["traefik"]["expose"].get("dashboard") is True, (
        "the dashboard entrypoint is not exposed for the additional "
        "dashboard Service: the route's backend has nothing to serve")
    fh = ports["traefik"]["forwardedHeaders"]
    assert fh["insecure"] is True, (
        "the dashboard entrypoint does not trust X-Forwarded-Proto on its "
        "plain-HTTP hop: behind the edge the scheme the entrypoint sees "
        "stays http, so scheme-sensitive behavior (redirects, cookies) "
        "degrades even through the Gateway")
    assert fh.get("trustedIPs"), (
        "no forwardedHeaders.trustedIPs pin: the entrypoint would trust "
        "X-Forwarded-* from 0.0.0.0/0 — the G7 audit class the hubble "
        "lane closed with its --trusted-proxy-ip allow-list")
    assert any(cidr.endswith("/32") for cidr in fh["trustedIPs"]), (
        "the ingress VIP is not pinned as a /32 second hop alongside the "
        "pod-network CIDR: the Gateway's own hop is not in the trust set")


def test_traefik_dashboard_service_shape():
    """The live Services match the pinned shape: the rke2-traefik
    LoadBalancer stays 80/443 (web/websecure only) and the dashboard
    Service is the ClusterIP the proxy fronts. A dashboard port
    appearing on the LB means the edge surface grew outside the
    Gateway lane."""
    lb = helpers.kubectl_json("get", "svc", "rke2-traefik", "-n",
                              "kube-system")
    lb_ports = [p["port"] for p in lb["spec"]["ports"]]
    assert lb_ports == [80, 443], (
        f"the rke2-traefik LoadBalancer carries ports {lb_ports}: the "
        f"dashboard entrypoint must stay off the LB pool (edge surface "
        f"is the platform Gateway, never a direct port)")

    dash = helpers.kubectl_json("get", "svc", "rke2-traefik-dashboard",
                                "-n", "kube-system")
    assert dash["spec"]["type"] == "ClusterIP", (
        "the rke2-traefik-dashboard Service is not a ClusterIP: the "
        "dashboard would claim an external address")
    assert [p["port"] for p in dash["spec"]["ports"]] == [8080], (
        "the rke2-traefik-dashboard Service does not expose the dashboard "
        "entrypoint on 8080")
    refs = dash["spec"]["selector"]
    assert refs.get("app.kubernetes.io/name") == "rke2-traefik", (
        f"the dashboard Service selects {refs}: it would front the wrong "
        f"pods")


def test_traefik_dashboard_edge_surface():
    """The platform Gateway carries the traefik listener with edge TLS,
    the cert-manager shim issued the listener certificate, and the
    traefik-dashboard HTTPRoute is Accepted and lands on the SSO
    proxy. That is the whole edge path the HCC block stands behind."""
    gw = helpers.kubectl_json("get", "gateway", "platform", "-n",
                              "kube-system")
    listener = next((l for l in gw["spec"]["listeners"]
                     if l["name"] == "traefik"), None)
    assert listener is not None, (
        "the platform Gateway has no traefik listener: the dashboard has "
        "no edge path and traefik.k8s.dev.lo falls through to Traefik's "
        "default cert")
    assert listener["port"] == 443 and listener["protocol"] == "HTTPS", (
        f"the traefik listener is {listener['port']}/{listener['protocol']}: "
        f"the edge TLS termination is not HTTPS")
    assert any(r["name"] == "traefik-edge-tls"
               for r in listener["tls"]["certificateRefs"]), (
        "the traefik listener does not reference traefik-edge-tls: the "
        "listener's TLS is not the shim-issued certificate")

    cert = helpers.kubectl_json("get", "certificate", "traefik-edge-tls",
                                "-n", "kube-system")
    status = next((c for c in cert.get("status", {}).get("conditions", [])
                   if c["type"] == "Ready"), {}).get("status", "False")
    assert status == "True", (
        "traefik-edge-tls is not ready: the Gateway shim did not issue "
        "the listener's certificate; edge TLS falls back to the default "
        "cert")

    route = helpers.kubectl_json("get", "httproute", "traefik-dashboard",
                                 "-n", "kube-system")
    conds = {c["type"]: c["status"]
             for p in route.get("status", {}).get("parents", [])
             for c in p.get("conditions", [])}
    assert conds.get("Accepted") == "True", (
        f"traefik-dashboard HTTPRoute not Accepted "
        f"({conds.get('Accepted', 'missing')}): the dashboard is "
        f"unreachable at the edge even with a ready listener")
    backends = [r["backendRefs"][0]["name"] for r in route["spec"]["rules"]
                if r.get("backendRefs")]
    assert "traefik-auth" in backends, (
        f"no rule lands on the traefik-auth proxy "
        f"(backends: {backends}): the SSO front is not in the edge "
        f"path, so the dashboard would serve unauthenticated")


def test_traefik_dashboard_root_redirects_https():
    """The route's root rule redirects / to /dashboard/ *at the Gateway*,
    so the Location stays https. Without the Gateway-level redirect the
    plain-HTTP entrypoint behind it emits an http:// Location (its
    internal dashboard redirect is bound to the scheme it sees), and a
    browser following the documented host root would end on port 80 with
    a 404. The check is against the live header, not the manifest: a
    drift that drops the filter re-appears here as an http Location."""
    session = helpers.make_session()
    root = session.get(TRAEFIK_DASH_BASE + "/", allow_redirects=False,
                       timeout=30)
    assert root.status_code in (301, 302), (
        f"GET / gave {root.status_code}: the dashboard root no longer "
        f"redirects to /dashboard/ (a non-redirect here means the "
        f"root-rule filter drifted)")
    location = root.headers.get("Location", "")
    assert location.startswith("https://"), (
        f"the root redirect's Location is {location!r} (scheme "
        f"downgraded to http): the redirect is no longer produced at the "
        f"TLS-terminating listener, so a browser would follow the "
        f"documented host root to port 80 and 404")
    assert location.rstrip("/") == f"{TRAEFIK_DASH_BASE}/dashboard", (
        f"the root redirect points at {location!r}, not the dashboard "
        f"prefix")


# ---------------------------------------------------------------------------
# SSO front (the traefik-auth oauth2-proxy in front of the dashboard)
# ---------------------------------------------------------------------------

def _traefik_sso_live() -> bool:
    """Whether the traefik-auth oauth2-proxy is deployed: the SSO front
    lives in the gitops apps/traefik-dashboard tree, so its absence
    means the SSO card has not been applied to this cluster yet. Skip
    the SSO tests in that case; the HCC-shape tests above still run."""
    try:
        helpers.kubectl_json("get", "deploy", "traefik-auth", "-n",
                             "kube-system")
        return True
    except RuntimeError:
        return False


_sso_tests = pytest.mark.skipif(
    not _traefik_sso_live(),
    reason=("traefik-auth (the dashboard's oauth2-proxy) is not deployed "
            "on this estate yet; the SSO tests skip until the gitops "
            "build lands it"))


@_sso_tests
def test_traefik_sso_proxy_fronts_the_dashboard():
    """The traefik-dashboard HTTPRoute's catch-all backend is the
    traefik-auth proxy, not the dashboard Service: that switch is what
    puts SSO in front of the dashboard. A route that still points at
    rke2-traefik-dashboard means the dashboard is serving
    unauthenticated at the edge again."""
    route = helpers.kubectl_json("get", "httproute", "traefik-dashboard",
                                 "-n", "kube-system")
    refs = [r["backendRefs"][0]["name"] for r in route["spec"]["rules"]
            if r.get("backendRefs")]
    assert "traefik-auth" in refs, (
        f"the traefik-dashboard route backend is {refs}: the dashboard is "
        f"not fronted by the traefik-auth proxy, so it serves "
        f"unauthenticated at the edge")

    proxy = helpers.kubectl_json("get", "deploy", "traefik-auth", "-n",
                                 "kube-system")
    assert proxy["status"].get("readyReplicas", 0) >= 1, (
        "traefik-auth is not ready: the proxy in front of the dashboard "
        "is down and the whole dashboard is unreachable (a down SSO "
        "proxy is a hard outage, not a degraded one)")


@_sso_tests
def test_traefik_sso_proxy_admits_on_the_roles_claim():
    """The proxy authorizes on the traefik client's roles: its args carry
    --allowed-role=traefik:user and traefik:admin (the keycloak-oidc
    shape, matching resource_access.traefik.roles) and neither an open
    upstream nor a wildcard allowed-group. The dashboard carries no
    admin tier of its own, so both tiers are admitted to view it. That
    is the whole authentication surface: get those wrong and either
    nobody can get in or anyone can."""
    proxy = helpers.kubectl_json("get", "deploy", "traefik-auth", "-n",
                                 "kube-system")
    args = proxy["spec"]["template"]["spec"]["containers"][0].get("args", [])
    assert "--provider=keycloak-oidc" in args, (
        f"the proxy runs no keycloak-oidc provider ({args}): SSO is not "
        f"actually on")
    assert "--allowed-role=traefik:user" in args, (
        "no --allowed-role=traefik:user: a traefik-users member is "
        "denied the dashboard even though the claim says otherwise")
    assert "--allowed-role=traefik:admin" in args, (
        "no --allowed-role=traefik:admin: a traefik-admins member is "
        "denied the dashboard")
    assert not any(a.startswith("--allowed-group=") for a in args), (
        "an allowed-group is set: admission is no longer decided on the "
        "roles claim")
    assert not any(a.startswith("--trusted-ip") or a == "--pass-user-to-upstream"
                   for a in args), (
        "an open bypass flag is set: the proxy admits a request without "
        "a session")
    # G7 hardening, reader-visible on the live args. The proxy must pin
    # the PKCE code-challenge method to S256 (leaving it unset runs the
    # OIDC code exchange on a plain, guessable code and the proxy logs
    # the PKCE unset warning) and pin the reverse-proxy's hop to an
    # explicit --trusted-proxy-ip allow-list (leaving it unset makes
    # the proxy trust 0.0.0.0/0 to forge X-Forwarded-*).
    assert "--code-challenge-method=S256" in args, (
        "no --code-challenge-method=S256: the OIDC code exchange runs on "
        "a plain (guessable) code, and the proxy logs the PKCE unset "
        "warning G7 flagged")
    assert any(a.startswith("--trusted-proxy-ip=") for a in args), (
        "no --trusted-proxy-ip= pin: with --reverse-proxy on and no "
        "trusted-proxy-ip, the proxy trusts every connecting IP "
        "(0.0.0.0/0) to forge X-Forwarded-* headers - G7's second "
        "warning")
    # The upstream must be the dashboard Service, not an open one.
    assert any(a == "--upstream=http://rke2-traefik-dashboard.kube-system"
               ".svc.cluster.local:8080" for a in args), (
        f"the proxy's upstream is {args}: the admission has nothing "
        f"dashboard-shaped behind it")
    # The client and cookie secrets must come from the ExternalSecret's
    # synced Secret (traefik-auth), not inline.
    envs = proxy["spec"]["template"]["spec"]["containers"][0].get("env", [])
    by_name = {e["name"]: e.get("valueFrom", {}) for e in envs}
    client_ref = by_name.get("OAUTH2_PROXY_CLIENT_SECRET", {}).get(
        "secretKeyRef", {})
    assert client_ref.get("key") == "client-secret" and \
        client_ref.get("name") == "traefik-auth", (
        f"the client secret is not mounted from the traefik-auth secret "
        f"(got {client_ref})")
    cookie_ref = by_name.get("OAUTH2_PROXY_COOKIE_SECRET", {}).get(
        "secretKeyRef", {})
    assert cookie_ref.get("key") == "cookie-secret" and \
        cookie_ref.get("name") == "traefik-auth", (
        f"the cookie secret is not mounted from the traefik-auth secret "
        f"(got {cookie_ref})")


@_sso_tests
def test_traefik_sso_secret_is_synced():
    """The traefik-auth Secret exists and holds both keys: the
    ExternalSecret synced kv/oidc-traefik (client-secret +
    cookie-secret). A Secret missing one key is the proxy crashing on
    start (client) or refusing to mint cookies (cookie)."""
    secret = helpers.kubectl_json("get", "secret", "traefik-auth", "-n",
                                  "kube-system")
    data = secret.get("data", {})
    assert "client-secret" in data, (
        "traefik-auth has no client-secret: the ExternalSecret did not "
        "sync, and the proxy cannot start")
    assert "cookie-secret" in data, (
        "traefik-auth has no cookie-secret: the proxy starts but cannot "
        "establish a session")


@_sso_tests
def test_traefik_sso_anonymous_denied():
    """An unauthenticated request to the dashboard is denied at the
    edge: the proxy redirects to the Keycloak ``traefik`` client
    instead of serving the UI. This is the anonymous half of the
    boundary; a 200 here means the SSO front is not actually in the
    path."""
    session = helpers.make_session()
    anon = session.get(TRAEFIK_DASH_BASE + "/dashboard/", allow_redirects=
                       False, timeout=30)
    location = anon.headers.get("Location", "")
    assert anon.status_code in (302, 303), (
        f"an anonymous GET /dashboard/ gave {anon.status_code}, expected "
        f"a redirect to Keycloak: the dashboard answers an anonymous "
        f"request, so the SSO front is bypassed")
    assert "client_id=traefik" in location, (
        f"the anonymous redirect's Location is {location[:160]!r}: the "
        f"proxy did not bounce to the traefik Keycloak client")


@_sso_tests
def test_traefik_sso_user_admitted(traefik_tiers):
    """A member of traefik-users, driving the real traefik-auth proxy, is
    admitted onto the dashboard: the anonymous request redirects to the
    traefik Keycloak client, the callback issues a session, and the
    dashboard page answers 200 with the app's own markup plus its
    /api/overview data. If the user tier cannot reach the dashboard, the
    whole user-facing half of the feature is broken."""
    user, _adm, _nobody = traefik_tiers
    report = helpers.traefik_sso_flow(user.name, user.password)
    assert report["admitted"], (
        f"a traefik-users member was not admitted to the dashboard "
        f"(stage={report['stage']} detail={report.get('detail', '')} "
        f"callback={report.get('callback_status')} "
        f"page={report.get('page_status')})")
    assert "Traefik" in report.get("page_title", ""), (
        f"the admitted session reached a page titled "
        f"{report.get('page_title')!r}, not the dashboard")
    assert report.get("api_status") == 200 and \
        report.get("api_routers", 0) > 0 and report.get("api_services", 0) > 0, (
        f"the admitted session's /api/overview gave "
        f"{report.get('api_status')} "
        f"({report.get('api_routers')} routers / "
        f"{report.get('api_services')} services): the ingress data the "
        f"dashboard is for is not reaching it behind the SSO gate")


@_sso_tests
def test_traefik_sso_admin_admitted(traefik_tiers):
    """A member of traefik-admins is admitted onto the dashboard through
    the same proxy. The dashboard carries no admin tier of its own, so
    the admin tier is admitted to view it exactly like the user tier;
    a regression that only breaks the admin path would otherwise be
    invisible to the user-tier test."""
    _user, adm, _nobody = traefik_tiers
    report = helpers.traefik_sso_flow(adm.name, adm.password)
    assert report["admitted"], (
        f"a traefik-admins member was not admitted to the dashboard "
        f"(stage={report['stage']} detail={report.get('detail', '')} "
        f"callback={report.get('callback_status')} "
        f"page={report.get('page_status')})")


@_sso_tests
def test_traefik_sso_nonmember_denied(traefik_tiers):
    """A user who is a federated SSO user but in no traefik group is
    denied at the proxy: Keycloak still issues a token (the user is a
    valid identity), but the traefik client carries neither the user
    nor the admin role, so the callback 403s, no session cookie is
    minted, and the request is bounced back to Keycloak instead of the
    upstream dashboard. This is the denial half of the boundary: if a
    non-member is admitted, the SSO gate has no teeth."""
    _user, _adm, nobody = traefik_tiers
    report = helpers.traefik_sso_flow(nobody.name, nobody.password)
    assert report["denied"], (
        f"a traefik non-member was not denied at the proxy "
        f"(stage={report['stage']} detail={report.get('detail', '')} "
        f"callback={report.get('callback_status')} "
        f"page={report.get('page_status')})")
    assert not report["admitted"], (
        "a traefik non-member was admitted to the dashboard: the SSO "
        "gate admits everyone, so the roles claim is not actually the "
        "admission gate")
    assert report["re_bounced"], (
        "after the 403 the proxy did not re-bounce the request to the "
        "traefik Keycloak client: the denied session is not being "
        "cleared correctly")
