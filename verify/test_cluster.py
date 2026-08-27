"""Baseline cluster health: the estate the rest of the suite measures
must itself be standing up. If this fails, the other suites' results are
uninterpretable, so it runs first and fast.

Checks:
  - all six nodes Ready (3 control plane, 3 worker)
  - every Flux Kustomization applied (Ready=True)
  - every Flux HelmRelease installed and reconciled (Ready=True)
"""

from __future__ import annotations

from verify import helpers

_EXPECTED_NODES = 6
_EXPECTED_HELM_RELEASES = {
    ("cert-manager", "cert-manager"),
    ("external-secrets", "external-secrets"),
    ("longhorn-system", "longhorn"),
    ("observability", "alloy"),
    ("observability", "kube-prometheus-stack"),
    ("observability", "loki"),
    ("observability", "tempo"),
    ("openbao", "openbao"),
}


def test_all_nodes_ready():
    nodes = helpers.kubectl_json("get", "nodes")
    ready = []
    not_ready = []
    for node in nodes["items"]:
        for condition in node["status"]["conditions"]:
            if condition["type"] == "Ready":
                (ready if condition["status"] == "True" else not_ready).append(
                    node["metadata"]["name"])
    assert not not_ready, f"nodes not Ready: {not_ready}"
    assert len(nodes["items"]) == _EXPECTED_NODES, (
        f"expected {_EXPECTED_NODES} nodes, found {len(nodes['items'])} "
        f"({[n['metadata']['name'] for n in nodes['items']]}) — a node "
        f"dropped out of the cluster"
    )
    assert len(ready) == _EXPECTED_NODES


def test_flux_kustomizations_applied():
    kustomizations = helpers.kubectl_json("get", "kustomization", "-A")
    assert kustomizations["items"], "no Flux Kustomizations at all"
    stuck = []
    for kz in kustomizations["items"]:
        status = "Unknown"
        for condition in kz["status"].get("conditions", []):
            if condition["type"] == "Ready":
                status = condition["status"]
        if status != "True":
            stuck.append(
                f"{kz['metadata']['namespace']}/{kz['metadata']['name']}"
                f" ({status})")
    assert not stuck, (
        "Flux Kustomizations not Ready — GitOps source is not fully "
        f"applied: {stuck}"
    )


def test_flux_helm_releases_installed():
    """Every HelmRelease the estate ships is present and Ready. A missing
    name is drift in the gitops source; a Ready!=True is a release stuck
    mid-reconcile."""
    helmreleases = helpers.kubectl_json("get", "helmrelease", "-A")
    found = {(hr["metadata"]["namespace"], hr["metadata"]["name"])
             for hr in helmreleases["items"]}
    missing = _EXPECTED_HELM_RELEASES - found
    assert not missing, (
        f"expected HelmReleases missing from the cluster: "
        f"{sorted(missing)} — the gitops source was not applied"
    )
    stuck = []
    for hr in helmreleases["items"]:
        for condition in hr["status"].get("conditions", []):
            if condition["type"] == "Ready" and condition["status"] != "True":
                stuck.append(
                    f"{hr['metadata']['namespace']}/{hr['metadata']['name']}"
                    f" ({condition.get('reason', '')})"
                )
    assert not stuck, f"HelmReleases not Ready: {stuck}"


def test_no_pods_in_error_states():
    """No pod is CrashLoopBackOff or Error anywhere. The other suites
    measure services through their APIs; a pod that is crash-looping but
    whose service endpoint still resolves to the last good replica would
    read as healthy and mask the incident."""
    pods = helpers.kubectl_json("get", "pods", "-A")
    bad = []
    for pod in pods["items"]:
        phase = pod["status"].get("phase")
        if phase in ("Failed",):
            bad.append(f"{pod['metadata']['namespace']}/{pod['metadata']['name']} ({phase})")
            continue
        for cs in pod["status"].get("containerStatuses", []) + pod["status"].get("initContainerStatuses", []):
            waiting = cs.get("state", {}).get("waiting")
            if waiting and waiting.get("reason") in ("CrashLoopBackOff", "Error"):
                bad.append(
                    f"{pod['metadata']['namespace']}/{pod['metadata']['name']}"
                    f" (waiting: {waiting.get('reason')})"
                )
                break
    assert not bad, f"pods in error states: {bad}"
