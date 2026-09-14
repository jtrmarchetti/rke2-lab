"""Deep data-plane gate for Cilium service-to-service mTLS.

``test_mtls`` is the configuration-surface gate: it pins that the
HelmChartConfig, the IPsec key Secret, the pilot policy and the SPIRE
stack are all present. This module proves the *data plane*:

  - the SPIRE server and agents are healthy (SVIDs can be issued)
  - a live agent-to-agent mTLS handshake succeeds between two throwaway
    pods, gated by a CiliumNetworkPolicy that requires the handshake
  - the same policy refuses a connection from a third pod that is not a
    fromEndpoint of the rule (per-connection enforcement, not just a
    config flag)
  - the encrypted pod network is actually up: the agent reports IPsec
    enabled and the node kernel carries ESP policies
  - the edge TLS boundary is unchanged: Traefik/Gateway still terminate
    TLS with domain-CA certs for the edge hosts (the same assertion the
    gateway suite makes over the network; here it is re-asserted against
    the serving certificate so a mesh CA sneaking into the edge is
    caught at the byte level)

The probes run in a throwaway namespace (``mtls-verify``) owned by a
session fixture; the namespace is deleted on teardown even when a test
fails, so a re-run is a clean start.
"""

from __future__ import annotations

import socket
import ssl
import subprocess
import time

import pytest
import yaml

from verify import helpers

# The throwaway probe namespace and the busybox image it runs. Manifests
# in the estate reference upstream image refs; the node-level
# registries.yaml rewrite (docker.io/* -> the GitLab registry mirror,
# where the artifacts.yml mirror entry lands the tag) resolves the pull
# in the air-gapped estate - the same convention the gitops workloads
# (e.g. keycloak) follow.
_PROBE_NS = "mtls-verify"
_PROBE_IMAGE = "docker.io/library/busybox:1.37.0"
_PROBE_PORT = "9999"
# One probe payload per sender, so the server's log tells the tests
# exactly which pod's traffic was admitted.
_CLIENT_PAYLOAD = "mtls-probe-client"
_OUTSIDE_PAYLOAD = "mtls-probe-outside"


def _mtls_live() -> bool:
    """Whether the estate carries the mTLS feature (same marker as
    ``test_mtls``: the rke2-cilium HCC has an authentication block).
    Read at collection time; this module's probes need the live data
    plane, so the whole module skips until the build lands the feature.
    """
    try:
        hcc = helpers.kubectl_json(
            "get", "helmchartconfig", "-n", "kube-system", "rke2-cilium")
        values = yaml.safe_load(hcc["spec"]["valuesContent"]) or {}
        return "authentication" in values
    except (RuntimeError, KeyError):
        raise

pytestmark = pytest.mark.skipif(
    not _mtls_live(),
    reason=("the rke2-cilium HelmChartConfig carries no authentication "
            "block: the mTLS feature is not deployed on this estate yet. "
            "The data-plane probes skip until the build lands it."))


# ---------------------------------------------------------------------------
# Probe resources
# ---------------------------------------------------------------------------

def _probe_manifests() -> str:
    """The probe namespace's contents: three pods and the required-auth
    CNP. The server pod keeps a log of what each connection sends; the
    two probes are run *from* the client and outside pods."""
    ns = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": _PROBE_NS},
    }
    server = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "mtls-probe-server",
            "namespace": _PROBE_NS,
            "labels": {"app": "mtls-probe"},
        },
        "spec": {
            "containers": [{
                "name": "nc",
                "image": _PROBE_IMAGE,
                # One accept at a time, appended to the log: the log is
                # the admission record the tests read back.
                "command": [
                    "sh", "-c",
                    (f"while :; do nc -l -p {_PROBE_PORT} -w 5 "
                     f">> /probe.log 2>&1; done"),
                ],
            }],
        },
    }
    client = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "mtls-probe-client",
            "namespace": _PROBE_NS,
            "labels": {"app": "mtls-client"},
        },
        "spec": {
            "containers": [{
                "name": "busybox",
                "image": _PROBE_IMAGE,
                "command": ["sh", "-c", "sleep infinity"],
            }],
        },
    }
    outside = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "mtls-probe-outside",
            "namespace": _PROBE_NS,
            "labels": {"app": "mtls-outside"},
        },
        "spec": {
            "containers": [{
                "name": "busybox",
                "image": _PROBE_IMAGE,
                "command": ["sh", "-c", "sleep infinity"],
            }],
        },
    }
    # The gate itself: connections into the server pod on 9999 are
    # admitted only when they come from the client's identity AND the
    # agent-to-agent mTLS handshake completes. A sender not in
    # fromEndpoints, or a sender whose handshake fails, is refused.
    # Purely additive (enableNonDefaultDenyPolicies is false estate-wide):
    # it adds mTLS to this one flow, nothing else.
    cnp = {
        "apiVersion": "cilium.io/v2",
        "kind": "CiliumNetworkPolicy",
        "metadata": {
            "name": "cnp-mtls-probe",
            "namespace": _PROBE_NS,
        },
        "spec": {
            "endpointSelector": {"matchLabels": {"app": "mtls-probe"}},
            "ingress": [{
                "fromEndpoints": [
                    {"matchLabels": {"app": "mtls-client"}}
                ],
                "authentication": {"mode": "required"},
                "toPorts": [
                    {"ports": [
                        {"port": _PROBE_PORT, "protocol": "TCP"}
                    ]}
                ],
            }],
        },
    }
    docs = [ns, server, client, outside, cnp]
    return "\n---\n".join(yaml.safe_dump(d, sort_keys=False)
                          for d in docs)


def _pod_ip(pod: str) -> str:
    p = helpers.kubectl_json("get", "pod", pod, "-n", _PROBE_NS)
    ip = p["status"].get("podIP")
    assert ip, f"pod {pod} has no podIP yet"
    return ip


def _probe_log() -> str:
    """The server pod's connection log (its admission record)."""
    proc = helpers.kubectl(
        "exec", "mtls-probe-server", "-n", _PROBE_NS, "--",
        "cat", "/probe.log", check=False)
    return (proc.stdout or "") + (proc.stderr or "")


@pytest.fixture(scope="session")
def mtls_probe():
    """The live probe pair: a required-auth CNP plus three busybox pods
    in a throwaway namespace. Admits only what it tears down."""
    helpers.kubectl_apply(_probe_manifests())
    # Let the CNP observe the endpoints before any test measures it; a
    # fresh CNP takes one endpoint-regeneration cycle to become active.
    for _ in range(40):
        proc = helpers.kubectl(
            "exec", "mtls-probe-server", "-n", _PROBE_NS,
            "--", "true", check=False)
        if proc.returncode == 0:
            break
        time.sleep(15)
    else:
        raise RuntimeError("the probe server pod never became reachable")
    time.sleep(30)
    server_ip = _pod_ip("mtls-probe-server")
    try:
        yield {"server_ip": server_ip}
    finally:
        proc = helpers.kubectl("delete", "namespace", _PROBE_NS,
                               "--wait=true", "--timeout=180s",
                               check=False)
        if proc.returncode != 0:
            print("WARNING: probe namespace teardown failed:\n"
                  f"{(proc.stdout or '') + (proc.stderr or '')}")


def _send(payload: str, sender_pod: str, server_ip: str,
          timeout_s: int = 15) -> subprocess.CompletedProcess:
    """Send one payload from a probe pod to the server's port."""
    return helpers.kubectl(
        "exec", sender_pod, "-n", _PROBE_NS, "--",
        "sh", "-c",
        (f"printf %s\\n {payload!r} | nc -w {timeout_s} "
         f"{server_ip} {_PROBE_PORT}"),
        check=False)


# ---------------------------------------------------------------------------
# SPIRE
# ---------------------------------------------------------------------------

def test_spire_server_healthy():
    """The mesh CA (SPIRE server) is up: without it no SVID is ever
    issued and every required-auth policy fails closed."""
    proc = helpers.kubectl(
        "exec", "spire-server-0", "-n", "cilium-spire",
        "-c", "spire-server", "--",
        "/opt/spire/bin/spire-server", "healthcheck",
        check=False)
    out = ((proc.stdout or "") + (proc.stderr or "")).lower()
    assert "healthy" in out, (
        "spire-server healthcheck did not report healthy: "
        f"{out.strip()[:300]} - the mesh CA is down and no SVID can be "
        "issued; every required-auth policy fails closed")


# ---------------------------------------------------------------------------
# The handshake
# ---------------------------------------------------------------------------

def test_mtls_handshake_succeeds(mtls_probe):
    """A connection from the client pod to the server pod succeeds under
    a policy that requires the mTLS handshake.

    This is the live proof the identity half of mTLS works end to
    end: SPIRE issued SVIDs for both endpoints, the two agents
    completed the out-of-band handshake on 4250, and Cilium admitted
    the connection on the strength of that handshake plus the policy
    rule. If SVID issuance were broken, the required-auth rule fails
    closed and this connection never completes."""
    server_ip = mtls_probe["server_ip"]
    _send(_CLIENT_PAYLOAD, "mtls-probe-client", server_ip)
    # The handshake and policy-observation cycles cost a few seconds;
    # allow for them before reading the admission record.
    for _ in range(12):
        if _CLIENT_PAYLOAD in _probe_log():
            return
        time.sleep(5)
    assert _CLIENT_PAYLOAD in _probe_log(), (
        "the client pod's connection was not admitted under the "
        "required-auth policy: the mTLS handshake did not complete "
        "(SVIDs not issued, agent-to-agent channel blocked, or the "
        f"CNP did not take effect). Server log: {_probe_log()[:300]!r}")


def test_mtls_policy_refuses_unlisted_sender(mtls_probe):
    """A connection from a pod that is NOT a fromEndpoint of the rule
    is refused: the policy is enforced per connection, not just
    declared. Without this the 'required' auth mode could be silently
    narrowed to an unintended sender set."""
    server_ip = mtls_probe["server_ip"]
    _send(_OUTSIDE_PAYLOAD, "mtls-probe-outside", server_ip)
    time.sleep(10)
    log = _probe_log()
    assert _OUTSIDE_PAYLOAD not in log, (
        "the outside pod's connection was admitted even though it is not "
        "a fromEndpoint of the required-auth rule: the policy no longer "
        "enforces the intended sender set. Server log: "
        f"{log[:300]!r}")


# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------

def _ds_pod(daemonset: str, namespace: str) -> str:
    """One Ready pod of a DaemonSet, by name. ``kubectl exec`` resolves
    pod names only, so the dataset selector has to be picked first."""
    pods = helpers.kubectl_json(
        "get", "pod", "-n", namespace,
        "-l", "k8s-app=%s" % daemonset, "--field-selector",
        "status.phase=Running")
    names = [p["metadata"]["name"] for p in pods["items"]]
    assert names, f"no running pod of DaemonSet {namespace}/{daemonset}"
    return names[0]


def test_cilium_ipsec_datapath_active():
    """The agent reports the IPsec datapath enabled: the confidentiality
    half of mTLS (the encrypted pod network) is configured in the
    running agent, not just in the HCC."""
    pod = _ds_pod("cilium", "kube-system")
    proc = helpers.kubectl(
        "exec", pod, "-n", "kube-system", "-c", "cilium-agent",
        "--", "cilium", "config", "enable-ipsec", check=False)
    assert proc.returncode == 0, (
        f"the cilium agent's config command failed: "
        f"{(proc.stderr or '').strip()[:200]}")
    assert proc.stdout.strip().lower() == "true", (
        "the running cilium agent does not report IPsec enabled: the "
        "encrypted pod network is off and east-west traffic crosses the "
        f"nodes in the clear. `cilium config enable-ipsec` gave "
        f"{proc.stdout.strip()!r}")


def test_node_xfrm_policies_carry_esp():
    """The node kernel is actually encrypting: xfrm policies with an
    ESP template exist on the nodes. IPsec that is configured in the
    agent but not installed in the kernel (module missing, xfrm
    state) still shows the agent flag on and still leaves traffic
    unencrypted - the kernel is where the claim is proven.

    Read from the cilium-agent pod on each node: it runs host-network,
    so its view of ``ip xfrm`` is the node's own xfrm state."""
    nodes = helpers.kubectl_json("get", "nodes")["items"]
    checked, bad = 0, []
    for node in nodes:
        name = node["metadata"]["name"]
        pods = helpers.kubectl_json(
            "get", "pod", "-n", "kube-system", "-l", "k8s-app=cilium",
            "--field-selector",
            f"spec.nodeName={name},status.phase=Running")
        if not pods["items"]:
            bad.append(f"{name}: no running cilium-agent pod")
            continue
        pod = pods["items"][0]["metadata"]["name"]
        # The kernel exposes xfrm state in /proc/net/xfrm_policy; the
        # agent pod runs host-network, so the file is the node's own.
        # (The agent image is not guaranteed to carry the iproute2
        # tools, so the procfs read is the reliable channel.)
        proc = helpers.kubectl(
            "exec", pod, "-n", "kube-system", "-c", "cilium-agent", "--",
            "sh", "-c", "cat /proc/net/xfrm_policy",
            check=False)
        if proc.returncode != 0:
            bad.append(f"{name}: xfrm query failed: "
                       f"{(proc.stderr or '').strip()[:150]}")
            continue
        checked += 1
        if "proto=esp" not in proc.stdout:
            bad.append(f"{name}: no ESP policy in the node's xfrm state")
    assert checked, f"no node was checkable: {bad}"
    assert not bad, (
        "nodes without ESP xfrm policies:\n" + "\n".join(bad) +
        "\nthe IPsec datapath is not installed in the kernel, so pod "
        "traffic is not actually encrypted")


# ---------------------------------------------------------------------------
# Edge boundary
# ---------------------------------------------------------------------------

def test_edge_tls_certificates_are_domain_ca():
    """The edge still terminates TLS with the domain CA, not with
    anything the mesh issues. A mesh CA or per-service trust bundle
    reaching the edge would show up here as a cert that does not chain
    to the domain CA - exactly the drift this feature must not cause.
    (The gateway suite asserts the same from the network side; this
    re-asserts against the served certificate itself, so a trust-bundle
    change is caught at the byte level even if the Gateway's own
    conditions still read healthy.)"""
    ca = helpers.ca_file()
    problems = []
    for hostname in ["sso.k8s.dev.lo", "grafana.k8s.dev.lo",
                     "longhorn.k8s.dev.lo", "bao.k8s.dev.lo",
                     "s3.k8s.dev.lo"]:
        ctx = ssl.create_default_context(cafile=ca)
        ctx.check_hostname = True
        try:
            with socket.create_connection((hostname, 443),
                                          timeout=15) as sock:
                with ctx.wrap_socket(sock,
                                     server_hostname=hostname) as s:
                    s.getpeercert()
        except ssl.SSLError as e:
            problems.append(
                f"{hostname} no longer serves a certificate chaining to "
                f"the domain CA ({e}) - the edge TLS termination changed "
                f"shape: check whether a mesh CA or trust bundle moved "
                f"into the edge")
    assert not problems, "edge TLS boundary moved:\n" + "\n".join(problems)
