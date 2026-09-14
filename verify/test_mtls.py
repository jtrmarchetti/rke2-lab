"""Cilium service-to-service mTLS configuration.

The estate encrypts east-west traffic with Cilium: mutual authentication
backed by the chart's in-cluster SPIRE (SPIFFE SVIDs per endpoint) plus
IPsec encryption of the pod network, enforced per-service by CiliumNetwork
policies that require the mTLS handshake. Traefik and the platform Gateway
keep terminating TLS at the edge; these tests pin that boundary - they
assert the Cilium-side configuration is present and that nothing in the
Cilium values pulled the edge TLS job in.

The deep data-plane checks (a live handshake between two throwaway pods,
SPIRE SVID issuance, per-connection enforcement) live in the validation
gate; this suite is the configuration-surface gate that must hold on every
warm build so a drift or rollback of any one piece fails fast.

PyYAML is a hard transitive dependency of the controller's automation
venv (ansible requires it), and this suite runs in that same venv, so it
is imported here for the HelmChartConfig valuesContent block.
"""

from __future__ import annotations

import base64
import re

import pytest
import yaml

from verify import helpers


def _mtls_configured() -> bool:
    """Whether the estate's rke2-cilium HelmChartConfig carries the mTLS
    configuration, i.e. the feature has been deployed.

    The single signal: the HCC valuesContent has an ``authentication``
    block. The estate ships the feature only through this one template, so
    its presence is the feature being live. Read as a collection-time
    marker so the whole module skips while the feature is not yet deployed
    (pre-build) and enforces on every warm build after.
    """
    try:
        hcc = helpers.kubectl_json("get", "helmchartconfig", "-n",
                                   "kube-system", "rke2-cilium")
        values = yaml.safe_load(hcc["spec"]["valuesContent"]) or {}
        return "authentication" in values
    except (RuntimeError, KeyError):
        # No reachable HCC means the cluster itself is not up; the rest of
        # the suite already needs it, so do not silently skip.
        raise


pytestmark = pytest.mark.skipif(
    not _mtls_configured(),
    reason=("the rke2-cilium HelmChartConfig carries no authentication "
            "block: the mTLS feature is not deployed on this estate yet. "
            "This suite enforces the post-feature configuration surface and "
            "skips until the IaC build lands it."))

# The agent-to-agent handshake port; a node firewall rule that stops it
# breaks mTLS silently (the connection drops instead of being admitted).
_MUTUAL_AUTH_PORT = 4250
# One key line in the shape Cilium's IPsec datapath expects:
#   <key-id>+ rfc4106(gcm(aes)) <40-hex PSK> 128
# The leading "+" forces per-tunnel key derivation (the global-key mode
# is deprecated). A malformed line shows up as the agents failing to bring
# the IPsec datapath up, so pin the shape here.
_IPSEC_KEY_LINE = re.compile(r"^\d+\+ rfc4106\(gcm\(aes\)\) [0-9a-f]{40} 128$")


def _cilium_values() -> dict:
    """The rke2-cilium HelmChartConfig valuesContent, parsed to a dict.

    The HCC carries valuesContent as a YAML *string*; the deep-merge RKE2
    applies is exactly that block, so asserting on the parsed structure is
    asserting on what the chart actually renders."""
    hcc = helpers.kubectl_json("get", "helmchartconfig", "-n", "kube-system",
                               "rke2-cilium")
    return yaml.safe_load(hcc["spec"]["valuesContent"])


def test_cilium_hcc_enables_mutual_authentication():
    values = _cilium_values()
    spire = values["authentication"]["mutual"]["spire"]
    assert values["authentication"]["enabled"] is True, (
        "authentication is off: the chart will not perform mTLS handshakes "
        "even where a policy requires one")
    assert spire["enabled"] is True, (
        "spire integration is off: no identity backend, SVIDs are never "
        "issued and every required-auth policy fails closed")
    assert spire["install"]["enabled"] is True, (
        "spire install is off: the estate relies on the chart's in-cluster "
        "SPIRE; an external install would need its own image channel")


def test_cilium_ipsec_keys_secret_present_and_well_formed():
    secret = helpers.kubectl_json("get", "secret", "-n", "kube-system",
                                  "cilium-ipsec-keys")
    line = base64.b64decode(secret["data"]["keys"]).decode()
    assert _IPSEC_KEY_LINE.match(line), (
        f"the cilium-ipsec-keys Secret does not carry a usable key line: "
        f"{line!r} is not '<id>+ rfc4106(gcm(aes)) <40-hex PSK> 128'. "
        f"The IPsec datapath will not start on a malformed line.")


def test_cilium_policy_encrypts_pod_traffic():
    values = _cilium_values()
    encryption = values["encryption"]
    assert encryption.get("enabled") is True, (
        "encryption is off: mutual authentication alone is identity-only; "
        "without the encrypted pod network the estate's east-west traffic "
        "still crosses the nodes in the clear")
    assert encryption.get("type") == "ipsec", (
        f"encryption type is {encryption.get('type')!r}, not ipsec: the "
        f"keyed secret and the keygen follow the ipsec path, so a type "
        f"change here is a configuration drift")


def test_cilium_namespace_policy_posture_is_additive():
    # With the chart default (true) the first CNP in a namespace flips it
    # to default-deny and can black-hole DNS or egress; the estate keeps
    # rollout additive so a removed policy restores default-allow.
    values = _cilium_values()
    assert values.get("enableNonDefaultDenyPolicies") is False, (
        "enableNonDefaultDenyPolicies is not false: the first policy in a "
        "namespace would flip it to default-deny and rolling out a new "
        "CNP could black-hole that namespace")


def test_pilot_mtls_policy_enforced_on_sso_path():
    # The pilot mTLS policy lives in the keycloak namespace and enforces the
    # SSO path: keycloak -> keycloak-db, port 5432.
    cnp = helpers.kubectl_json("get", "cnp", "-n", "keycloak",
                               "cnp-mutual-auth-keycloak-db")
    spec = cnp["spec"]
    assert spec["endpointSelector"]["matchLabels"]["app"] == "keycloak-db", (
        "the pilot CNP no longer selects the SSO database: the policy "
        "moved without an approval of the mTLS scope")
    rule = spec["ingress"][0]
    assert rule["authentication"]["mode"] == "required", (
        "the pilot rule no longer requires the mTLS handshake: the SSO "
        "path is back to plain L3/L4, which is what this feature exists "
        "to replace")
    # The rule must still be scoped to the SSO consumer, so the
    # handshake requirement cannot cascade onto other namespaces.
    assert rule["fromEndpoints"][0]["matchLabels"] == {"app": "keycloak"}, (
        f"the rule's fromEndpoints moved to "
        f"{rule['fromEndpoints'][0]['matchLabels']}: the mTLS requirement "
        f"now applies to an unplanned consumer")


def test_spire_stack_scheduled():
    # The tripwire for the air-gapped image channel: a missing SPIRE
    # mirror surfaces as these workloads stuck, not as a mysterious
    # policy denial.
    sts = helpers.kubectl_json("get", "statefulset", "-n", "cilium-spire",
                               "spire-server")
    assert sts["status"].get("readyReplicas", 0) >= 1, (
        f"spire-server is not Ready: {sts['status'].get('conditions')} - "
        f"a stuck server is the difference between SVIDs being issued and "
        f"every required-auth policy failing closed")
    ds = helpers.kubectl_json("get", "daemonset", "-n", "cilium-spire",
                              "spire-agent")
    desired = ds["status"].get("desiredNumberScheduled", 0)
    scheduled = ds["status"].get("updatedNumberScheduled", 0)
    assert desired > 0 and scheduled == desired, (
        f"spire-agent is {scheduled}/{desired} scheduled: a node without "
        f"its agent cannot mint SVIDs for anything it hosts, and every "
        f"mTLS connection touching that node fails closed")


def test_edge_tls_termination_stays_on_traefik():
    # The hard boundary: mTLS landed in Cilium, and Traefik must still be
    # the only place that terminates TLS at the edge. Two pins: the
    # platform Gateway listener is still Terminate, and no edge-TLS job
    # (a mesh CA, a service-level trust bundle) has moved into the
    # Cilium values.
    gw = helpers.kubectl_json("get", "gateway", "-n", "kube-system",
                              "platform")
    listeners = gw["spec"]["listeners"]
    tls_modes = {l.get("tls", {}).get("mode") for l in listeners if "tls" in l}
    assert tls_modes == {"Terminate"}, (
        f"the platform Gateway listener tls modes are {tls_modes}, not "
        f"Terminate: the edge TLS termination moved and the whole "
        f"test_gateway suite's assumptions are broken")
    values = _cilium_values()
    # No mesh-CA / per-endpoint TLS-terminating keys: those belong to a
    # service-mesh CA design, not to this estate's SPIRE + IPsec one.
    for stray in ("meshService", "trustBundleConfig", "l7Service"):
        assert stray not in values, (
            f"Cilium values gained '{stray}': the edge TLS termination or "
            f"a separate mesh CA moved into Cilium, which the design "
            f"explicitly keeps on Traefik")


def test_mtls_handshake_port_open_on_nodes():
    # R6: the agent-to-agent channel (port 4250) must be open on every
    # node. The estate's nodes are L2/L3-flat with no host firewalls; a
    # ufw/nftables rule that appears later is exactly the kind of thing
    # this catches before a required-auth policy silently fails closed.
    # Probed from the controller over the tunnel, one node at a time.
    import socket
    nodes = helpers.kubectl_json("get", "nodes")["items"]
    blocked = []
    for node in nodes:
        name = node["metadata"]["name"]
        ip = next(a["address"] for a in node["status"]["addresses"]
                  if a["type"] == "InternalIP")
        try:
            with socket.create_connection((ip, _MUTUAL_AUTH_PORT),
                                         timeout=5):
                pass
        except OSError as exc:
            blocked.append(f"{name} ({ip}): {exc}")
    assert not blocked, (
        f"the mTLS handshake port {_MUTUAL_AUTH_PORT} is not reachable "
        f"on: {blocked} - a host firewall rule is blocking the "
        f"agent-to-agent channel and required-auth policies fail closed")
