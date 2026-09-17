"""Traefik dashboard: the pre-SSO edge-exposure lane.

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

The G7 hardening (audit class of the hubble-auth --trusted-proxy-ip pin,
commit 00fb020): the dashboard entrypoint trusts X-Forwarded-* only from
the estate's pod network and the ingress VIP, pinned in the HCC's
``ports.traefik.forwardedHeaders`` block.

Like test_mtls, this module is a configuration-surface gate: the tests
skip while the rke2-traefik HCC carries no dashboard block, and enforce
on every warm build after, so a rollback or drift fails fast.
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
    Service is the ClusterIP the route's backendRef points at. A dashboard
    port appearing on the LB means the edge surface grew outside the
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
    traefik-dashboard HTTPRoute is Accepted and lands on the dashboard
    Service. That is the whole edge path the HCC block stands behind."""
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
    assert "rke2-traefik-dashboard" in backends, (
        f"no rule lands on the rke2-traefik-dashboard Service "
        f"(backends: {backends}): the dashboard has no backend")


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


def test_traefik_dashboard_serves_ingress_data():
    """The acceptance behavior of the lane: the dashboard UI and its API
    answer through the edge with the ingress's own data. A 200 on an
    empty board would still be 'reachable'; the router/service totals
    prove the ingress is what the dashboard is monitoring. The UI page
    carries the dashboard's own markup (its window.APIUrl bootstrap
    script), which pins the body, not just the status code."""
    session = helpers.make_session()
    page = session.get(TRAEFIK_DASH_BASE + "/dashboard/", allow_redirects=
                      False, timeout=30)
    assert page.status_code == 200, (
        f"GET /dashboard/ gave {page.status_code}: the dashboard UI does "
        f"not serve through the edge")
    assert "window.APIUrl" in page.text, (
        "the /dashboard/ body is not the Traefik dashboard markup "
        "(no window.APIUrl bootstrap): the edge is answering a fallback "
        "page, not the dashboard")

    overview = session.get(TRAEFIK_DASH_BASE + "/api/overview",
                           allow_redirects=False, timeout=30)
    assert overview.status_code == 200, (
        f"GET /api/overview gave {overview.status_code}: the dashboard "
        f"API does not serve through the edge")
    data = overview.json()
    routers = data.get("http", {}).get("routers", {}).get("total", 0)
    services = data.get("http", {}).get("services", {}).get("total", 0)
    assert routers > 0 and services > 0, (
        f"/api/overview reports {routers} routers / {services} services: "
        f"the ingress data the dashboard is for is not reaching it")
