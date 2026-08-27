"""Observability data tests: the three datasources behind Grafana must hold
real, queryable data — not just be configured.

"Real data" is asserted at the datasource, through Grafana's datasource
proxy, so the test covers the whole chain (browser auth to Grafana,
Grafana -> Prometheus/Loki/Tempo). Tempo has no organic trace traffic in
a quiet lab, so a canary trace is injected through the Alloy collector's
OTLP endpoint and then read back through the Tempo datasource: a hit can
only come from this run, and the assertion is about Tempo's query API
returning the span.
"""

from __future__ import annotations

import json
import secrets
import socket
import time
from urllib.parse import quote

import requests

from verify import helpers

_PROM_UID = "prometheus"
_LOKI_UID = "loki"
_TEMPO_UID = "tempo"

# Alloy's OTLP/gRPC + HTTP ingest port; the suite tunnels it to the host
# with kubectl port-forward rather than depending on a cluster service
# being reachable from here.
_ALLOY_OTLP_HOST_PORT = 14318
_ALLOY_OTLP_SVC_PORT = 4318


def _grafana_basic_session() -> requests.Session:
    """Basic-auth session as the local Grafana admin (the break-glass
    account held in OpenBao). Using the real credential exercises the
    same path a human uses when SSO is down, and no secret of the suite's
    own."""
    user, password = helpers.grafana_credentials()
    session = requests.Session()
    session.verify = helpers._ca()
    session.auth = (user, password)
    return session


def _proxy_get(session: requests.Session, uid: str, path: str,
               params: dict | None = None) -> requests.Response:
    return session.get(
        f"{helpers.GRAFANA_BASE}/api/datasources/proxy/uid/{uid}{path}",
        params=params or {}, timeout=60)


def test_prometheus_has_real_targets():
    """The Prometheus datasource answers and knows about a live fleet."""
    session = _grafana_basic_session()
    resp = _proxy_get(session, _PROM_UID, "/api/v1/query",
                      {"query": 'count(up{job!=""})'})
    resp.raise_for_status()
    result = resp.json()["data"]["result"]
    assert result, "Prometheus returned no result for count(up)"
    target_count = int(result[0]["value"][1])
    assert target_count > 20, (
        f"only {target_count} up targets visible through Grafana — the "
        f"scrape config has gone away or Prometheus is not reading the "
        f"cluster"
    )


def test_loki_has_streams():
    """Loki's datasource answers, knows its label names, and an actual log
    query returns lines — not just label metadata."""
    session = _grafana_basic_session()
    labels = _proxy_get(session, _LOKI_UID, "/loki/api/v1/labels")
    labels.raise_for_status()
    assert "namespace" in labels.json()["data"], (
        "no 'namespace' label in Loki — no labelled streams at all: "
        f"{labels.json()['data']}"
    )

    # A range query with start/end in *nanoseconds* — Grafana's proxy
    # expects the raw Loki API units, not its own seconds convention.
    # Integer arithmetic only: a float here serialises in scientific
    # notation and Loki rejects it.
    now_ns = int(time.time() * 1e9)
    hour_ns = 3_600_000_000_000
    resp = _proxy_get(session, _LOKI_UID, "/loki/api/v1/query_range",
                      {"query": '{namespace="observability"}',
                       "limit": "5",
                       "start": str(now_ns - hour_ns),
                       "end": str(now_ns)})
    resp.raise_for_status()
    streams = resp.json()["data"]["result"]
    assert streams, "Loki label query matched zero streams"
    total_lines = sum(len(s["values"]) for s in streams)
    assert total_lines > 0, "Loki streams exist but held no recent log lines"


def _otlp_trace_payload(trace_id: str, span_id: str) -> dict:
    """OTLP/HTTP JSON with a single span. `status.code` must be the
    numeric 0, not the string 'Ok' — the collector rejects the string
    form with a ReadEnumValue error."""
    now_ns = time.time_ns()
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name",
                         "value": {"stringValue": "verify-harness"}},
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "verify-harness"},
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": span_id,
                                "name": "verify-harness.canary",
                                "kind": 1,
                                "startTimeUnixNano": now_ns,
                                "endTimeUnixNano": now_ns + 1_000_000,
                                "status": {"code": 0},
                            }
                        ],
                    }
                ],
            }
        ]
    }


def test_tempo_has_queryable_traces():
    """Tempo must hold traces, verified by injecting a canary through the
    Alloy OTLP endpoint and then finding it in Tempo through Grafana."""
    trace_id = secrets.token_hex(16)
    span_id = secrets.token_hex(8)

    # Tunnel the collector's OTLP port to the host, push the canary, and
    # tear the tunnel back down. The collector pushes to Tempo on its own
    # schedule, so the lookup below polls until the span lands.
    port_forward = helpers.kubectl_popen(
        "-n", "observability", "port-forward", "svc/alloy",
        f"{_ALLOY_OTLP_HOST_PORT}:{_ALLOY_OTLP_SVC_PORT}")
    try:
        # Wait for the local listener to open before posting. A raw
        # socket connect is the right probe: an OTLP endpoint answers
        # HTTP/1.1 with a protocol error, not a 200.
        deadline = time.time() + 20
        while True:
            try:
                with socket.create_connection(
                        ("127.0.0.1", _ALLOY_OTLP_HOST_PORT), timeout=2):
                    break
            except OSError:
                assert time.time() < deadline, (
                    "port-forward to Alloy's OTLP port never opened"
                )
                time.sleep(0.5)

        # Plain HTTP to 127.0.0.1 by design: this is the canary leg, not a
        # service TLS check, so the suite's no-verify=False rule (which
        # covers the cluster endpoints) does not apply here.
        resp = requests.post(
            f"http://127.0.0.1:{_ALLOY_OTLP_HOST_PORT}/v1/traces",
            data=json.dumps(_otlp_trace_payload(trace_id, span_id)),
            headers={"Content-Type": "application/json"},
            timeout=30)
        assert resp.status_code == 200, (
            f"OTLP ingest gave {resp.status_code}: {resp.text[:300]}"
        )
    finally:
        port_forward.kill()
        port_forward.wait()

    # The lookup endpoint wants the raw hex trace id; quote() keeps it
    # URL-safe (hex needs no encoding, but the call documents intent and
    # is robust if the id ever stops being hex).
    session = _grafana_basic_session()
    deadline = time.time() + 90
    last = "no attempt yet"
    while time.time() < deadline:
        try:
            resp = _proxy_get(
                session, _TEMPO_UID, f"/api/traces/{quote(trace_id)}")
            if resp.status_code == 200:
                body = resp.json()
                if body:
                    return
            last = f"{resp.status_code}: {resp.text[:200]}"
        except Exception as exc:  # noqa: BLE001 - probe loop
            last = str(exc)
        time.sleep(5)
    raise AssertionError(
        f"canary trace {trace_id} never appeared in Tempo; last probe: {last}"
    )
