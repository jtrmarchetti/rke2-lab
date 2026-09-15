"""Cilium Hubble: service-mesh flow observability.

The estate's rke2-cilium HelmChartConfig turns ``hubble`` on: the Hubble
server inside every cilium agent, the hubble-relay Deployment, and the
hubble-ui Deployment in kube-system (the chart ships all three; the
estate only carries the values that switch them on). The UI is exposed
on the platform Gateway's ``hubble`` listener - the cert-manager shim
issues the hubble-edge-tls certificate for it, the way it does for
every edge host - and the Keycloak SSO in front of it is a follow-on
card, so the baseline publishes the UI unauthenticated.

Like test_mtls, this module is the configuration-surface gate: it skips
while the feature is not deployed (the HCC has no ``hubble`` block) and
enforces on every warm build after, so a rollback or drift fails fast.
"""

from __future__ import annotations

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
    terminated at the gateway, cert-manager shim; baseline is
    unauthenticated - the SSO card lands in front of it)."""
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
