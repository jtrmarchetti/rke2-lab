"""Cilium Hubble: service-mesh flow observability, plus its SSO front-end
and RBAC tiers.

The estate's rke2-cilium HelmChartConfig turns ``hubble`` on: the Hubble
server inside every cilium agent, the hubble-relay Deployment, and the
hubble-ui Deployment in kube-system (the chart ships all three; the
estate only carries the values that switch them on). The UI is exposed
on the platform Gateway's ``hubble`` listener - the cert-manager shim
issues the hubble-edge-tls certificate for it, the way it does for
every edge host.

The UI has no authentication of its own, so the gitops tree puts an
oauth2-proxy (hubble-auth) in front of it, the estate's longhorn-auth
pattern: unauthenticated requests are redirected to Keycloak and
admission is decided on the hubble client's roles. The RBAC tiers
(hubble-view for hubble-users, hubble-admin for hubble-admins) are the
API-level half of the same boundary.

Like test_mtls, this module is a configuration-surface gate: the Hubble
tests skip while the HCC has no ``hubble`` block, the SSO tests skip
while the hubble-auth proxy is not deployed yet, and everything
enforces on every warm build after, so a rollback or drift fails fast.
"""

from __future__ import annotations

import json
import os
import subprocess

import yaml

import pytest

from verify import helpers


def _hubble_configured() -> bool:
    """Whether the rke2-cilium HelmChartConfig carries a ``hubble``
    block, i.e. the feature has been deployed. The estate ships Hubble
    only through that one template, so its presence is the feature
    being live."""
    try:
        hcc = helpers.kubectl_json("get", "helmchartconfig", "-n",
                                   "kube-system", "rke2-cilium")
        values = yaml.safe_load(hcc["spec"]["valuesContent"]) or {}
        return "hubble" in values
    except (RuntimeError, KeyError):
        # No reachable HCC means the cluster itself is not up; the rest of
        # the suite already needs it, so do not silently skip.
        raise


pytestmark = pytest.mark.skipif(
    not _hubble_configured(),
    reason=("the rke2-cilium HelmChartConfig carries no hubble block: "
            "Hubble is not deployed on this estate yet. This suite "
            "enforces the post-feature configuration surface and skips "
            "until the IaC build lands it."))


def _cilium_values() -> dict:
    """The rke2-cilium HelmChartConfig valuesContent, parsed to a dict.

    The HCC carries valuesContent as a YAML *string*; the deep-merge RKE2
    applies is exactly that block, so asserting on the parsed structure
    is asserting on what the chart actually renders."""
    hcc = helpers.kubectl_json("get", "helmchartconfig", "-n", "kube-system",
                               "rke2-cilium")
    return yaml.safe_load(hcc["spec"]["valuesContent"])


def test_hcc_enables_hubble_relay_and_ui():
    """The values block switches the whole chain on: the agent-side Hubble
    server, the relay, and the UI. A partial block (relay on, agent off,
    or vice versa) is a chart validation error - UI without relay fails
    the chart's validate template - so pin the trio together."""
    values = _cilium_values()
    hubble = values["hubble"]
    assert hubble["enabled"] is True, (
        "hubble is off: the agents run no Hubble server, the relay has "
        "no flows and the UI is an empty shell")
    assert hubble["relay"]["enabled"] is True, (
        "hubble.relay is off: the UI cannot reach any agent's Hubble "
        "server without the relay")
    assert hubble["ui"]["enabled"] is True, (
        "hubble.ui is off: nothing renders the flows the relay collects")


def test_hubble_workloads_ready():
    """The relay and UI Deployments are ready. The relay's own readiness
    proves the agent loop: the relay only reports ready once it has
    connected to an agent's Hubble gRPC server (4244) with the chart's
    auto-generated client certificate."""
    relay = helpers.kubectl_json("get", "deploy", "hubble-relay", "-n",
                                 "kube-system")
    ui = helpers.kubectl_json("get", "deploy", "hubble-ui", "-n",
                              "kube-system")
    assert relay["status"].get("readyReplicas", 0) >= 1, (
        "hubble-relay is not ready: the relay cannot reach the agents' "
        "Hubble servers (TLS or the 4244 endpoint), so the UI would show "
        "no flows even if the agents run Hubble")
    assert ui["status"].get("readyReplicas", 0) >= 1, (
        "hubble-ui is not ready: neither the frontend nginx nor the "
        "backend API is serving; the UI edge route answers an empty page")


def test_agents_run_the_hubble_server():
    """The cilium configmap carries the Hubble settings - the agent-side
    half of the loop. Without enable-hubble the relay's connections
    refuse and the UI shows nothing, regardless of the Deployments."""
    config = helpers.kubectl_json("get", "cm", "cilium-config", "-n",
                                  "kube-system")["data"]
    assert config.get("enable-hubble") == "true", (
        "the cilium configmap has no enable-hubble: the agent rolled "
        "without the Hubble server; the relay connects to nothing")


def test_hubble_edge_listener_and_route():
    """The platform Gateway carries the hubble listener and the hubble-ui
    HTTPRoute is Accepted and bound: that is the UI's edge path (TLS
    terminated at the gateway, cert-manager shim)."""
    gw = helpers.kubectl_json("get", "gateway", "platform", "-n",
                              "kube-system")
    listeners = [l["name"] for l in gw["spec"].get("listeners", [])]
    assert "hubble" in listeners, (
        "the platform Gateway has no hubble listener: the UI has no edge "
        "path; the hubble-edge-tls certificate is never issued and "
        "hubble.k8s.dev.lo falls through to Traefik's default cert")

    cert = helpers.kubectl_json("get", "certificate", "hubble-edge-tls",
                                "-n", "kube-system")
    status = next((c for c in cert.get("status", {}).get("conditions", [])
                   if c["type"] == "Ready"), {}).get("status", "False")
    assert status == "True", (
        "hubble-edge-tls is not ready: the Gateway shim did not issue "
        "the listener's certificate; edge TLS for the UI falls back to "
        "the default cert")

    route = helpers.kubectl_json("get", "httproute", "hubble-ui", "-n",
                                 "kube-system")
    conds = {c["type"]: c["status"]
             for p in route.get("status", {}).get("parents", [])
             for c in p.get("conditions", [])}
    assert conds.get("Accepted") == "True", (
        f"hubble-ui HTTPRoute not Accepted ({conds.get('Accepted', 'missing')}): "
        f"the UI is unreachable at the edge even with a ready listener")


def _hubble_sso_live() -> bool:
    """Whether the hubble-auth oauth2-proxy is deployed: the SSO
    front-end (and its RBAC tiers) live in the gitops apps/hubble-ui
    tree, so their absence means the SSO card has not been applied to
    this cluster yet. Skip the SSO tests in that case; the Hubble
    tests above still run."""
    try:
        helpers.kubectl_json("get", "deploy", "hubble-auth", "-n",
                             "kube-system")
        return True
    except RuntimeError:
        return False


_sso_tests = pytest.mark.skipif(
    not _hubble_sso_live(),
    reason=("hubble-auth (the Hubble UI's oauth2-proxy) is not deployed "
            "on this estate yet; the SSO tests skip until the gitops "
            "build lands it"))


def _subject_access_review(user: str, groups: list[str],
                           resource_attributes: dict) -> bool:
    """Run a SubjectAccessReview and return its allowed verdict.

    The estate's API has no user-level authentication (cluster access
    rides the kubeconfig), so the K8s RBAC tiers are enforced as Group
    subjects. A synthetic user + groups is the only way to ask the API
    server what a member of a FreeIPA-derived SSO group may do."""
    sar = {"apiVersion": "authorization.k8s.io/v1", "kind": "SubjectAccessReview",
           "spec": {"user": user, "resourceAttributes": resource_attributes}}
    if groups:
        sar["spec"]["groups"] = groups
    proc = subprocess.run(
        [helpers._kubectl_exe(), "--kubeconfig",
         os.environ.get("KUBECONFIG") or helpers.KUBECONFIG_DEFAULT,
         "create", "-f", "-", "-o", "json"],
        input=json.dumps(sar), capture_output=True, text=True,
        env=dict(os.environ, KUBECONFIG=helpers.KUBECONFIG_DEFAULT),
    )
    assert proc.returncode == 0, (
        f"SubjectAccessReview failed: {proc.stderr.strip()[:300]}")
    return json.loads(proc.stdout)["status"]["allowed"]


@_sso_tests
def test_hubble_sso_proxy_fronts_the_ui():
    """The hubble-ui HTTPRoute's backend is the hubble-auth proxy, not the
    UI service: that switch is what puts SSO in front of the UI. A route
    that still points at hubble-ui means the UI is serving unauthenticated
    at the edge again."""
    route = helpers.kubectl_json("get", "httproute", "hubble-ui", "-n",
                                 "kube-system")
    refs = [r["backendRefs"][0]["name"] for r in route["spec"]["rules"]]
    assert "hubble-auth" in refs, (
        f"hubble-ui route backend is {refs}: the UI is not fronted by the "
        f"hubble-auth proxy, so it serves unauthenticated at the edge")

    proxy = helpers.kubectl_json("get", "deploy", "hubble-auth", "-n",
                                 "kube-system")
    assert proxy["status"].get("readyReplicas", 0) >= 1, (
        "hubble-auth is not ready: the proxy in front of the UI is down "
        "and the whole UI is unreachable (a down SSO proxy is a hard "
        "outage, not a degraded one)")


@_sso_tests
def test_hubble_sso_proxy_admits_on_the_roles_claim():
    """The proxy authorizes on the hubble client's roles: its args carry
    --allowed-role=hubble:user and hubble:admin (the keycloak-oidc
    shape, matching resource_access.hubble.roles) and neither an
    open upstream nor a wildcard allowed-group. That is the whole
    authentication surface: get those wrong and either nobody can get in
    or anyone can."""
    proxy = helpers.kubectl_json("get", "deploy", "hubble-auth", "-n",
                                 "kube-system")
    args = proxy["spec"]["template"]["spec"]["containers"][0].get("args", [])
    assert "--provider=keycloak-oidc" in args, (
        f"the proxy runs no keycloak-oidc provider ({args}): SSO is not "
        f"actually on")
    assert "--allowed-role=hubble:user" in args, (
        "no --allowed-role=hubble:user: a hubble-users member is denied "
        "the UI even though the claim says otherwise")
    assert "--allowed-role=hubble:admin" in args, (
        "no --allowed-role=hubble:admin: a hubble-admins member is "
        "denied the UI")
    assert not any(a.startswith("--allowed-group=") for a in args), (
        "an allowed-group is set: admission is no longer decided on the "
        "roles claim")
    assert not any(a.startswith("--trusted-ip") or a == "--pass-user-to-upstream"
                   for a in args), (
        "an open bypass flag is set: the proxy admits a request without "
        "a session")
    # G7 hardening, reader-visible on the live args. The proxy must pin the
    # PKCE code-challenge method to S256 (leaving it unset runs the OIDC
    # code exchange on a plain, guessable code and the proxy logs the PKCE
    # unset warning) and pin the reverse-proxy's hop to an explicit
    # --trusted-proxy-ip allow-list (leaving it unset makes the proxy trust
    # 0.0.0.0/0 to forge X-Forwarded-*). Both are the two startup warnings
    # the G7 audit flagged.
    assert "--code-challenge-method=S256" in args, (
        "no --code-challenge-method=S256: the OIDC code exchange runs on a "
        "plain (guessable) code, and the proxy logs the PKCE unset warning "
        "G7 flagged")
    assert any(a.startswith("--trusted-proxy-ip=") for a in args), (
        "no --trusted-proxy-ip= pin: with --reverse-proxy on and no "
        "trusted-proxy-ip, the proxy trusts every connecting IP (0.0.0.0/0) "
        "to forge X-Forwarded-* headers - G7's second warning")
    # The client and cookie secrets must come from the ExternalSecret's
    # synced Secret (hubble-auth), not inline.
    envs = proxy["spec"]["template"]["spec"]["containers"][0].get("env", [])
    by_name = {e["name"]: e.get("valueFrom", {}) for e in envs}
    client_ref = by_name.get("OAUTH2_PROXY_CLIENT_SECRET", {}).get(
        "secretKeyRef", {})
    assert client_ref.get("key") == "client-secret" and \
        client_ref.get("name") == "hubble-auth", (
        f"the client secret is not mounted from the hubble-auth secret "
        f"(got {client_ref})")
    cookie_ref = by_name.get("OAUTH2_PROXY_COOKIE_SECRET", {}).get(
        "secretKeyRef", {})
    assert cookie_ref.get("key") == "cookie-secret" and \
        cookie_ref.get("name") == "hubble-auth", (
        f"the cookie secret is not mounted from the hubble-auth secret "
        f"(got {cookie_ref})")


@_sso_tests
def test_hubble_sso_secret_is_synced():
    """The hubble-auth Secret exists and holds both keys: the
    ExternalSecret synced kv/oidc-hubble (client-secret + cookie-secret).
    A Secret missing one key is the proxy crashing on start (client) or
    refusing to mint cookies (cookie)."""
    secret = helpers.kubectl_json("get", "secret", "hubble-auth", "-n",
                                  "kube-system")
    data = secret.get("data", {})
    assert "client-secret" in data, (
        "hubble-auth has no client-secret: the ExternalSecret did not "
        "sync, and the proxy cannot start")
    assert "cookie-secret" in data, (
        "hubble-auth has no cookie-secret: the proxy starts but cannot "
        "establish a session")


@_sso_tests
@pytest.mark.parametrize("group,resource_attributes,expected", [
    ("hubble-users",
     {"verb": "list", "resource": "ciliumnetworkpolicies", "group": "cilium.io"},
     True),
    ("hubble-users",
     {"verb": "list", "resource": "pods", "group": ""},
     True),
    ("hubble-users",
     {"verb": "update", "resource": "deployments", "name": "hubble-relay",
      "namespace": "kube-system", "group": "apps"},
     False),
    ("hubble-admins",
     {"verb": "list", "resource": "ciliumnetworkpolicies", "group": "cilium.io"},
     True),
    ("hubble-admins",
     {"verb": "update", "resource": "deployments", "name": "hubble-relay",
      "namespace": "kube-system", "group": "apps"},
     True),
    ("hubble-admins",
     {"verb": "get", "resource": "helmchartconfigs", "name": "rke2-cilium",
      "group": "helm.cattle.io"},
     True),
    # Negative (review D3): the HCC read is pinned to rke2-cilium by
    # resourceNames even though HelmChartConfig is cluster-scoped - the
    # estate's API server honors resourceNames on this resource, so an
    # admin may NOT read another chart's HCC. Asserting the negative locks
    # that the admin tier's HCC access stays scoped to the one that carries
    # the hubble values, not every HelmChartConfig in the cluster.
    ("hubble-admins",
     {"verb": "get", "resource": "helmchartconfigs", "name": "rke2-traefik",
      "group": "helm.cattle.io"},
     False),
    # The view tier has no HCC rule at all: a hubble-users member may not
    # read even the scoped rke2-cilium HCC.
    ("hubble-users",
     {"verb": "get", "resource": "helmchartconfigs", "name": "rke2-cilium",
      "group": "helm.cattle.io"},
     False),
    (None,
     {"verb": "list", "resource": "ciliumnetworkpolicies", "group": "cilium.io"},
     False),
], ids=["user-views-mesh", "user-views-pods", "user-no-manage",
        "admin-views-mesh", "admin-manages-relay", "admin-manages-hcc",
        "admin-hcc-scoped", "user-no-hcc",
        "nobody-denied"])
def test_hubble_rbac_tiers(group, resource_attributes, expected):
    """The K8s RBAC tiers enforce per group: hubble-users may read the
    service-mesh surface (the UI's data) but not manage the Hubble
    control-plane; hubble-admins may do both; a user in neither group is
    denied. A tier that allows too much is a privilege escalation, so
    both directions are asserted - including the cluster-scoped HCC read,
    where admin is pinned to rke2-cilium by resourceNames (the review D3
    negative: admin may NOT read another chart's HCC) and the view tier
    has no HCC rule at all."""
    assert _subject_access_review("verify-test", [group] if group else [],
                                  resource_attributes) is expected, (
        f"RBAC tier {group or '(no group)'}: "
        f"{resource_attributes['verb']} "
        f"{resource_attributes.get('group', 'core')}/"
        f"{resource_attributes.get('resource')} "
        f"({resource_attributes.get('name', '')}) expected "
        f"allowed={expected}")


# ---------------------------------------------------------------------------
# Live SSO through the hubble-auth proxy (real identities, real flows)
#
# The RBAC-tier tests above ask the API what a tier may do. These drive the
# *actual* SSO front-end the way a browser would: an anonymous request to the
# UI 302s to the hubble Keycloak client, a member is admitted onto the UI, and
# a non-member is turned away at the proxy. This is the end-to-end proof the
# config-surface and SAR tests stand in for.
# ---------------------------------------------------------------------------


def _hubble_agent_pod() -> str:
    """A running cilium-agent pod: the `hubble observe` channel reads the
    flow pipeline from an agent, the estate's established way to prove the
    mesh's traffic is observable."""
    pods = helpers.kubectl_json(
        "get", "pod", "-n", "kube-system", "-l", "k8s-app=cilium",
        "--field-selector", "status.phase=Running")
    assert pods["items"], ("no running cilium-agent pod: cannot read the "
                           "agent-side flow pipeline to prove mesh traffic "
                           "is observable")
    return pods["items"][0]["metadata"]["name"]


@_sso_tests
def test_hubble_sso_user_admitted(hubble_tiers):
    """A member of hubble-users, driving the real hubble-auth proxy, is
    admitted onto the Hubble UI: the anonymous request redirects to the
    hubble Keycloak client, the callback issues a session, and the UI page
    answers 200 with the app's own markup. If the user tier cannot reach
    the UI, the whole user-facing half of the feature is broken."""
    user, _adm, _nobody = hubble_tiers
    report = helpers.hubble_sso_flow(user.name, user.password)
    assert report["admitted"], (
        f"a hubble-users member was not admitted to the Hubble UI "
        f"(stage={report['stage']} detail={report.get('detail', '')} "
        f"callback={report.get('callback_status')} "
        f"page={report.get('page_status')})")
    assert "Hubble" in report.get("page_title", ""), (
        f"the admitted session reached a page titled "
        f"{report.get('page_title', '')!r}, not the Hubble UI")


@_sso_tests
def test_hubble_sso_admin_admitted(hubble_tiers):
    """A member of hubble-admins is admitted onto the Hubble UI through the
    same proxy. The admin tier must reach at least everything the user tier
    does; a regression that only breaks the admin path would otherwise be
    invisible to the user-tier test."""
    _user, adm, _nobody = hubble_tiers
    report = helpers.hubble_sso_flow(adm.name, adm.password)
    assert report["admitted"], (
        f"a hubble-admins member was not admitted to the Hubble UI "
        f"(stage={report['stage']} detail={report.get('detail', '')} "
        f"callback={report.get('callback_status')} "
        f"page={report.get('page_status')})")


@_sso_tests
def test_hubble_sso_nonmember_denied(hubble_tiers):
    """A user who is a federated SSO user but in no hubble group is denied
    at the proxy: Keycloak still issues a token (the user is a valid
    identity), but the hubble client carries neither the user nor the admin
    role, so the callback 403s, no session cookie is minted, and the request
    is bounced back to Keycloak instead of the upstream UI. This is the
    denial half of the boundary: if a non-member is admitted, the SSO gate
    has no teeth."""
    _user, _adm, nobody = hubble_tiers
    report = helpers.hubble_sso_flow(nobody.name, nobody.password)
    assert report["denied"], (
        f"a hubble non-member was not denied at the proxy "
        f"(stage={report['stage']} detail={report.get('detail', '')} "
        f"callback={report.get('callback_status')} "
        f"page={report.get('page_status')})")
    assert not report["admitted"], (
        "a hubble non-member was admitted to the UI: the SSO gate admits "
        "everyone, so the roles claim is not actually the admission gate")
    assert report["re_bounced"], (
        "after the 403 the proxy did not re-bounce the request to the "
        "hubble Keycloak client: the denied session is not being cleared "
        "correctly")


@_sso_tests
def test_hubble_mesh_traffic_is_visible():
    """The Hubble pipeline actually carries service-mesh traffic: an agent
    reports live flow records. This is what the UI's service map renders, so
    an empty agent pipeline means the UI would open on an empty board even
    for a correctly-admitted user. Read the way the estate reads agent-side
    data: `hubble observe` on a running cilium-agent, bounded so the test
    cannot hang on a quiet cluster."""
    pod = _hubble_agent_pod()
    proc = helpers.kubectl(
        "exec", pod, "-n", "kube-system", "-c", "cilium-agent", "--",
        "timeout", "30", "hubble", "observe", "--last", "5", check=False)
    assert proc.returncode == 0, (
        f"hubble observe failed on {pod}: {(proc.stderr or '')[:300]}")
    out = proc.stdout.strip()
    assert out, (
        "hubble observe returned no flows: the agent's Hubble pipeline is "
        "not reporting service-mesh traffic, so the UI would show an empty "
        "board even to an admitted user")
    assert any(tok in out for tok in ("FORWARDED", "DROPPED", "->", "<->")), (
        f"hubble observe output is not a flow record: {out[:200]!r}")


@_sso_tests
def test_hubble_sso_proxy_logs_show_no_auth_errors():
    """The hubble-auth proxy's own logs carry no authentication errors.
    The expected `[AuthFailure]` denial lines (a non-member's 403) are
    correct behavior, not errors; a hard error signature (a panic, a
    failed OIDC issuer/secret load, a 5xx) is a broken proxy.

    G7 hardening: the two startup WARNINGs the G7 audit flagged - the
    unset PKCE code-challenge method and the unset --trusted-proxy-ip on
    --reverse-proxy - are now pinned in the Deployment, so a fresh proxy
    pod's log must carry neither. The [AuthFailure] denial lines remain
    expected (a non-member's 403 is correct behavior)."""
    proc = helpers.kubectl(
        "logs", "deploy/hubble-auth", "-n", "kube-system", check=False)
    assert proc.returncode == 0, (
        f"could not read the hubble-auth proxy logs: {proc.stderr[:300]}")
    log = proc.stdout or ""
    # Not a vacuous pass: we must have actually read the live proxy log.
    # The banner proves the proxy came up and configured its Keycloak OIDC
    # client; an empty log would mean the pod restarted and the scan says
    # nothing.
    assert "OAuthProxy configured for Keycloak OIDC Client ID: hubble" in log, (
        "the hubble-auth log does not carry the OIDC banner "
        "(OAuthProxy configured for Keycloak OIDC Client ID: hubble): the "
        "proxy is not up and serving the hubble client, so the SSO "
        "front-end is down")
    # G7: the two pinned hardening flags must have cleared the two startup
    # warnings the G7 audit flagged. Neither signature may appear in the
    # live proxy log.
    g7_warnings = [l for l in log.splitlines() if
                   "not enabled one with --code-challenge-method" in l
                   or "no --trusted-proxy-ip CIDRs were configured" in l]
    assert not g7_warnings, (
        "the hubble-auth log still carries a G7-flagged startup warning "
        "(unset PKCE code-challenge method and/or unset --trusted-proxy-ip): "
        "the G7 hardening did not land on the live proxy:\n"
        + "\n".join(g7_warnings[:5]))
    hard_errors = []
    for i, line in enumerate(log.splitlines(), 1):
        low = line.lower()
        if any(sig in low for sig in
               ("panic", "fatal", "issuer not found",
                "failed to verify", "invalid issuer",
                "unable to load", "secret not found",
                "connection refused", "500 internal")):
            hard_errors.append(f"{i}: {line.strip()[:160]}")
    assert not hard_errors, (
        "the hubble-auth proxy logged authentication errors:\n"
        + "\n".join(hard_errors[:10])
        + "\na broken OIDC issuer/secret load or panic means the SSO "
        "front-end is not serving admissions reliably")
