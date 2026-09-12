"""Gateway API checks against the shared platform edge Gateway.

The estate's edge traffic is served by one Gateway named ``platform`` in
``kube-system`` (programmed by Traefik), with one HTTPS listener per edge
host and per-app HTTPRoutes in their own namespaces. These tests pin the
state the rest of the suite depends on:

  - the platform Gateway is Accepted and Programmed
  - the Gateway carries a listener for every edge host
  - every per-app HTTPRoute is Accepted and bound to a parent
  - each edge hostname serves a valid TLS session that chains to the
    domain CA (the same CA every other suite verifies against)
"""

from __future__ import annotations

import socket
import ssl

from verify import helpers

# (short host = Gateway listener name / sectionName, hostname, HTTPRoute namespace)
_EDGE_HOSTS = [
    ("sso", "sso.k8s.dev.lo", "keycloak"),
    ("grafana", "grafana.k8s.dev.lo", "observability"),
    ("longhorn", "longhorn.k8s.dev.lo", "longhorn-system"),
    ("bao", "bao.k8s.dev.lo", "openbao"),
    ("s3", "s3.k8s.dev.lo", "garage"),
]


def _conditions(resource: dict) -> dict:
    """condition type -> (status, reason) for a resource's status.conditions.

    Gateways write their conditions at status.conditions; HTTPRoutes write
    them per-parent at status.parents[].conditions (the Gateway API shape),
    so read from both places."""
    out = {}
    for c in resource.get("status", {}).get("conditions", []):
        out[c["type"]] = (c["status"], c.get("reason", ""))
    for parent in resource.get("status", {}).get("parents", []):
        for c in parent.get("conditions", []):
            out[c["type"]] = (c["status"], c.get("reason", ""))
    return out


def _platform_gateway() -> dict:
    return helpers.kubectl_json("get", "gateway", "platform", "-n", "kube-system")


def test_platform_gateway_programmed():
    """The platform Gateway itself is Accepted and Programmed. Not
    Programmed means Traefik is not acting on it — the whole edge is
    down even if individual HTTPRoutes read as Accepted."""
    gw = _platform_gateway()
    conds = _conditions(gw)
    status, reason = conds.get("Accepted", ("False", "missing"))
    assert status == "True", (
        f"Gateway platform/kube-system not Accepted ({reason}) — the "
        f"GatewayClass or spec is being rejected"
    )
    status, reason = conds.get("Programmed", ("False", "missing"))
    assert status == "True", (
        f"Gateway platform/kube-system not Programmed ({reason}) — "
        f"Traefik is not programming the shared edge"
    )


def test_platform_gateway_listeners_cover_all_hosts():
    """Every edge host has a listener on the platform Gateway. A missing
    listener means that host's TLS falls through to Traefik's default
    cert even if its HTTPRoute exists."""
    gw = _platform_gateway()
    listener_names = [l["name"] for l in gw["spec"].get("listeners", [])]
    missing = [host for host, _, _ in _EDGE_HOSTS if host not in listener_names]
    assert not missing, (
        f"Gateway platform is missing listeners for: {missing} — the "
        f"platform Gateway no longer covers that host"
    )


def test_httproutes_accepted_and_bound():
    """Every per-app HTTPRoute is Accepted and bound to at least one
    parent. A route that is Accepted but not bound is a silent edge gap
    — the host answers with the fallback."""
    bad = []
    for host, hostname, ns in _EDGE_HOSTS:
        routes = helpers.kubectl_json(
            "get", "httproute", "-n", ns,
            "--field-selector", f"metadata.namespace={ns}",
        )["items"]
        matched = [r for r in routes if hostname in r["spec"].get("hostnames", [])]
        if not matched:
            bad.append(f"{ns}: no HTTPRoute for {hostname}")
            continue
        for r in matched:
            name = r["metadata"]["name"]
            status, reason = _conditions(r).get("Accepted", ("False", "missing"))
            if status != "True":
                bad.append(f"{ns}/{name} not Accepted ({reason})")
                continue
            bound = any(
                c.get("type") == "Accepted" and c.get("status") == "True"
                for p in r.get("status", {}).get("parents", [])
                for c in p.get("conditions", [])
            )
            if not bound:
                bad.append(f"{ns}/{name} Accepted but not bound to a parent")
    assert not bad, f"HTTPRoute problems: {bad}"


def test_edge_hostnames_serve_valid_tls():
    """Each edge hostname terminates TLS with a certificate that chains
    to the domain CA and names the host. This is the end-to-end proof the
    Gateway shim issued a cert for the listener and Traefik is serving it
    — the same thing a browser would see before login."""
    ca = helpers.ca_file()
    problems = []
    for host, hostname, _ in _EDGE_HOSTS:
        ctx = ssl.create_default_context(cafile=ca)
        ctx.check_hostname = True
        try:
            with socket.create_connection((hostname, 443), timeout=15) as sock:
                with ctx.wrap_socket(sock, server_hostname=hostname) as s:
                    cert = s.getpeercert()
        except (ssl.SSLCertVerificationError, ssl.SSLError, OSError) as e:
            problems.append(f"{hostname}: TLS failed ({e})")
            continue
        subject = dict(x[0] for x in cert.get("subject", ()))
        san = [v for t, v in cert.get("subjectAltName", ()) if t == "DNS"]
        if hostname not in san and subject.get("commonName") != hostname:
            problems.append(
                f"{hostname}: certificate does not name the host "
                f"(CN={subject.get('commonName')!r}, SAN={san})"
            )
    assert not problems, f"edge TLS problems: {problems}"
