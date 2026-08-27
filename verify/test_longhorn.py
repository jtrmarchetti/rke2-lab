"""Longhorn storage tests: every expected PVC-backed volume exists, is
attached and healthy, and the disks it schedules onto have headroom.

The estate stores its state (Garage's object store, Keycloak's database,
the observability tier, OpenBao's own data) on Longhorn through PVCs. A
volume that is `detached` or a disk that is full is not a warning, it is
the whole cluster quietly losing durability.
"""

from __future__ import annotations

from verify import helpers

# (namespace, pvc_name) for each service that stores state on Longhorn.
# The PVC names come from the workloads that request them; a rename in the
# estate breaks the mapping, which is exactly what this test should catch.
_EXPECTED_PVCS = [
    ("garage", "data-garage-0"),
    ("keycloak", "data-keycloak-db-0"),
    ("observability", "alertmanager-kube-prometheus-stack-alertmanager-db-alertmanager-kube-prometheus-stack-alertmanager-0"),
    ("observability", "prometheus-kube-prometheus-stack-prometheus-db-prometheus-kube-prometheus-stack-prometheus-0"),
    ("observability", "storage-loki-0"),
    ("observability", "storage-tempo-0"),
    ("openbao", "audit-openbao-0"),
    ("openbao", "data-openbao-0"),
]

# A disk under this ratio of free space is "nearing full" for the estate:
# replicas of the biggest volumes would no longer fit, and Longhorn's own
# scheduling would start refusing them.
_MIN_HEADROOM_RATIO = 0.10


def _pvc_to_volume_map() -> dict:
    """Map the longhorn volume name (spec.volumeName) per PVC, per namespace."""
    volumes: dict[str, str] = {}
    for ns, pvc_name in _EXPECTED_PVCS:
        pvc = helpers.kubectl_json("get", "pvc", pvc_name, "-n", ns)
        assert pvc["status"]["phase"] == "Bound", (
            f"{ns}/{pvc_name} is {pvc['status']['phase']}, not Bound"
        )
        volumes[f"{ns}/{pvc_name}"] = pvc["spec"]["volumeName"]
    return volumes


def test_expected_volumes_exist_and_attached():
    """All eight expected volumes exist on Longhorn, each attached and
    healthy. The volumes themselves carry no labels; identity is carried
    by the PVCs, which is why the map goes PVC -> spec.volumeName."""
    longhorn_volumes = helpers.kubectl_json(
        "get", "volume.longhorn.io", "-n", "longhorn-system")
    by_name = {v["metadata"]["name"]: v for v in longhorn_volumes["items"]}

    expected = _pvc_to_volume_map()
    for pvc_label, vol_name in expected.items():
        vol = by_name.get(vol_name)
        assert vol is not None, (
            f"{pvc_label} points at {vol_name}, which does not exist on "
            f"Longhorn — the PVC is bound to a volume the cluster lost"
        )
        status = vol["status"]
        assert status.get("state") == "attached", (
            f"{pvc_label} ({vol_name}) is {status.get('state')}, not attached"
        )
        assert status.get("robustness") == "healthy", (
            f"{pvc_label} ({vol_name}) robustness is {status.get('robustness')}, "
            f"not healthy — replicas have degraded"
        )


def test_longhorn_disk_headroom():
    """No Longhorn disk is within _MIN_HEADROOM_RATIO of full. The disk
    report is keyed by disk UUID with a storageMaximum/storageAvailable
    pair; a full disk here means replicas of existing volumes can no
    longer be placed on it, which is silent data-loss risk, not a
    scheduling error."""
    nodes = helpers.kubectl_json("get", "node.longhorn.io", "-n", "longhorn-system")
    checked = 0
    worst = 1.0
    for node in nodes["items"]:
        disk_status = node["status"].get("diskStatus", {})
        for disk_id, disk in disk_status.items():
            total = disk.get("storageMaximum", 0)
            free = disk.get("storageAvailable", 0)
            if total <= 0:
                continue
            checked += 1
            ratio = free / total
            worst = min(worst, ratio)
            assert ratio > _MIN_HEADROOM_RATIO, (
                f"Longhorn node {node['metadata']['name']} disk "
                f"{disk.get('diskPath', disk_id)} is at "
                f"{100 - ratio * 100:.0f}% used "
                f"({free / (1024 ** 3):.0f} of {total / (1024 ** 3):.0f} GiB "
                f"free) — under the {_MIN_HEADROOM_RATIO * 100:.0f}% headroom "
                f"line; replicas of existing volumes can no longer be placed "
                f"here"
            )
    assert checked > 0, "no schedulable Longhorn disk found at all"
    assert worst > _MIN_HEADROOM_RATIO, f"worst disk headroom is {worst:.2%}"


def test_longhorn_nodes_schedulable():
    """Every Longhorn node reports its Ready and Schedulable conditions
    true. A single node dropping out of scheduling halves replica
    placement for the whole estate, not just that node's share."""
    nodes = helpers.kubectl_json("get", "node.longhorn.io", "-n", "longhorn-system")
    assert nodes["items"], "no Longhorn nodes registered"
    for node in nodes["items"]:
        conditions = {c["type"]: c.get("status")
                      for c in node["status"].get("conditions", [])}
        assert conditions.get("Ready") == "True", (
            f"Longhorn node {node['metadata']['name']} is not Ready"
        )
        assert conditions.get("Schedulable") == "True", (
            f"Longhorn node {node['metadata']['name']} is not schedulable"
        )
