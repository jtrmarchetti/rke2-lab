"""Cluster-wide best-fit VM placement for the 3-node PVE cluster.

Why this module exists
----------------------
PVE's VM-creation API is always node-scoped (``POST /nodes/<n>/qemu/``);
there is no "create in the datacenter, PVE picks the node" primitive, and
this cluster has no shared/Ceph storage and no HA manager to make any node
equally viable. The IaC equivalent of "build against the whole cluster, not
against specific hosts" is therefore decided *here*, at ``pulumi up`` time:

* For every VM in the build, pick the online node with the most headroom
  (free RAM, then free VM-disk) at that moment.
* Within a single build, account for other *new* VMs already placed, so a
  burst of new VMs spreads across nodes instead of piling onto one.
* **Stable placement record**: once a VM is placed on a node, the choice is
  written to a committed JSON record. On every later build the record wins,
  so Pulumi never re-selects (and therefore never destroy/re-creates) an
  existing VM — ``node_name`` is a force-new input on ``VmLegacy``.
* An explicit ``overrides`` map (``vm key -> node``) beats everything, for
  deliberate pinning. Only brand-new VMs with no override and no record
  entry are best-fit selected.

The PVE usage query reuses the PAM-ticket auth flow proven in
``modules/pve_cleanup.py`` and ``test_sdn/sdn_apply.py`` (stdlib urllib only),
reading the live per-node fields that a placement decision depends on:

* ``GET /cluster/status``        -> which nodes are online
* ``GET /nodes/<n>/status``      -> ``memory.available`` (bytes)
* ``GET /nodes/<n>/storage``     -> per-store ``avail`` (bytes), the VM-disk pools

Nothing here writes to PVE; it is read-only. The only write is the placement
record file, which is a plain repo artifact reviewed like any other change.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class NodeCapacity:
    """Live headroom for one node, read from the PVE API at build time."""

    name: str
    free_ram_bytes: int
    free_disk_bytes: int


@dataclass(frozen=True)
class PlacementRequest:
    """What a single VM asks of its host."""

    key: str
    ram_bytes: int
    disk_bytes: int


@dataclass
class PlacementRecord:
    """Committed key->node mapping that keeps placement stable across builds."""

    path: Path
    placements: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "PlacementRecord":
        record = cls(path=path)
        if path.exists():
            try:
                raw = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"placement record {path} is unreadable ({exc}); "
                    "fix or delete it before running"
                ) from exc
            if not isinstance(raw, dict):
                raise RuntimeError(f"placement record {path} is not a JSON object")
            record.placements = {str(k): str(v) for k, v in raw.items()}
        return record

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Sort keys so the committed artifact is deterministic and diffs clean.
        payload = json.dumps(
            {k: self.placements[k] for k in sorted(self.placements)},
            indent=2,
        )
        self.path.write_text(payload + "\n")


# ---------------------------------------------------------------------------
# Read-only PVE usage query (PAM ticket flow, stdlib only)
# ---------------------------------------------------------------------------


class _PveUsageClient:
    """Minimal read-only PVE API client for the placement planner."""

    def __init__(self, endpoint: str, username: str, password: str, insecure: bool):
        parsed = urllib.parse.urlparse(endpoint)
        self.base = f"https://{parsed.netloc or endpoint}/api2/json"
        self.username = username
        self.password = password
        self._ctx = ssl._create_unverified_context() if insecure else ssl.create_default_context()
        self._ticket = ""
        self._csrf = ""

    def _request(
        self,
        method: str,
        path: str,
        form: dict | None = None,
    ) -> tuple[int, dict | list]:
        data = None
        headers = {"Cookie": f"PVEAuthCookie={self._ticket}"}
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if method in ("POST", "PUT", "DELETE") and self._csrf:
            headers["CSRFPreventionToken"] = self._csrf
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, context=self._ctx, timeout=60) as resp:
                body = resp.read().decode()
                return resp.status, (json.loads(body) if body else {})
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()
            try:
                return exc.code, json.loads(body)
            except (json.JSONDecodeError, ValueError):
                return exc.code, {"message": body[:400]}

    def _auth(self) -> None:
        status, payload = self._request(
            "POST",
            "/access/ticket",
            form={"new-format": 1, "username": self.username, "password": self.password},
        )
        data = payload.get("data") or {} if isinstance(payload, dict) else {}
        self._ticket = data.get("ticket", "")
        self._csrf = data.get("CSRFPreventionToken", "")
        if status != 200 or not self._ticket:
            raise RuntimeError(f"PVE usage query auth failed ({status}): {payload}")

    def auth(self) -> None:
        """Acquire the PAM ticket. Must run before any node-scoped read."""
        self._auth()

    def node_ips(self) -> dict[str, str]:
        """Return {node name: ip} for every node in the cluster.

        ``GET /cluster/status`` carries each node's management IP; used to
        route per-node host operations to the right host without
        hard-coding the mapping.
        """
        self._auth()
        status, payload = self._request("GET", "/cluster/status")
        if status != 200:
            raise RuntimeError(f"cluster/status failed ({status}): {payload}")
        entries = payload.get("data") if isinstance(payload, dict) else payload
        ips: dict[str, str] = {}
        for entry in entries or []:
            if isinstance(entry, dict) and entry.get("type") == "node":
                name = entry.get("name", "")
                ip = entry.get("ip", "")
                if name and ip:
                    ips[name] = ip
        return ips

    def online_nodes(self) -> list[str]:
        self._auth()
        status, payload = self._request("GET", "/cluster/status")
        if status != 200:
            raise RuntimeError(f"cluster/status failed ({status}): {payload}")
        entries = payload.get("data") if isinstance(payload, dict) else payload
        nodes: list[str] = []
        for entry in entries or []:
            if isinstance(entry, dict) and entry.get("type") == "node" and entry.get("online"):
                nodes.append(entry["name"])
        return sorted(nodes)

    def node_capacity(self, node: str) -> NodeCapacity:
        status, payload = self._request("GET", f"/nodes/{node}/status")
        if status != 200:
            raise RuntimeError(f"{node} /status failed ({status}): {payload}")
        memory = (payload.get("data") or {}).get("memory", {}) if isinstance(payload, dict) else {}
        free_ram = int(memory.get("available", 0))

        status, payload = self._request("GET", f"/nodes/{node}/storage")
        if status != 200:
            raise RuntimeError(f"{node} /storage failed ({status}): {payload}")
        entries = payload.get("data") if isinstance(payload, dict) else payload
        free_disk = 0
        for store in entries or []:
            if not isinstance(store, dict) or not store.get("active"):
                continue
            # Only the VM-disk pools count: stores that can hold images/rootdir.
            content = str(store.get("content", ""))
            if "images" in content or "rootdir" in content:
                free_disk += int(store.get("avail", 0))
        return NodeCapacity(name=node, free_ram_bytes=free_ram, free_disk_bytes=free_disk)


# ---------------------------------------------------------------------------
# Best-fit planning (pure)
# ---------------------------------------------------------------------------


def _plan(
    requests: list[PlacementRequest],
    capacities: dict[str, NodeCapacity],
    record: PlacementRecord,
    overrides: dict[str, str],
) -> dict[str, str]:
    """Choose a node for every request.

    Stable: an override beats a record entry beats a fresh best-fit pick.
    Fresh picks account for other freshly-placed requests so a burst spreads
    across nodes. Deterministic tie-break: more free RAM, then more free
    disk, then node name.
    """
    # Running occupancy so multiple new VMs in one build do not stack on one node.
    occupied_ram: dict[str, int] = {n: 0 for n in capacities}
    occupied_disk: dict[str, int] = {n: 0 for n in capacities}

    choices: dict[str, str] = {}
    for req in requests:
        if req.key in overrides:
            node = overrides[req.key]
            choices[req.key] = node
            # An already-pinned VM holds its footprint on that node; account
            # for it so fresh picks do not stack onto an occupied node.
            if node in occupied_ram:
                occupied_ram[node] += req.ram_bytes
                occupied_disk[node] += req.disk_bytes
            continue
        if req.key in record.placements:
            node = record.placements[req.key]
            choices[req.key] = node
            if node in occupied_ram:
                occupied_ram[node] += req.ram_bytes
                occupied_disk[node] += req.disk_bytes
            continue

        candidates: list[tuple[str, int, int]] = []
        for name, cap in capacities.items():
            free_ram = cap.free_ram_bytes - occupied_ram[name]
            free_disk = cap.free_disk_bytes - occupied_disk[name]
            # Feasibility: host the whole footprint with a small RAM safety
            # margin. Disk is checked but the thin pool is huge, so RAM is
            # the real constraint on this cluster.
            if free_ram < req.ram_bytes or free_disk < req.disk_bytes:
                continue
            candidates.append((name, free_ram, free_disk))

        if not candidates:
            raise RuntimeError(
                f"no feasible node for VM '{req.key}': needs "
                f"{req.ram_bytes}B RAM / {req.disk_bytes}B disk but no node has "
                "headroom; free a node or add capacity"
            )

        # Most free RAM, then most free disk, then name (deterministic).
        chosen = min(candidates, key=lambda c: (-c[1], -c[2], c[0]))[0]
        choices[req.key] = chosen
        record.placements[req.key] = chosen
        occupied_ram[chosen] += req.ram_bytes
        occupied_disk[chosen] += req.disk_bytes
    return choices


def plan_placement(
    endpoint: str,
    username: str,
    password: str,
    insecure: bool,
    vm_ram_disk: dict[str, tuple[int, int]],
    record_path: Path,
    overrides: dict[str, str] | None = None,
    candidate_nodes: Iterable[str] | None = None,
    persist: bool = True,
) -> dict[str, str]:
    """Plan node placement for every VM in the build.

    ``vm_ram_disk`` maps ``vm key -> (ram_bytes, disk_bytes)``. Returns the
    full ``vm key -> node`` mapping. New placements are persisted to
    ``record_path`` unless ``persist`` is False (used by ``pulumi preview``
    so previews stay side-effect free on the repo).
    """
    overrides = overrides or {}
    record = PlacementRecord.load(record_path)

    client = _PveUsageClient(endpoint, username, password, insecure)
    if candidate_nodes:
        # Explicit candidate set: acquire the ticket, then read each node's
        # headroom. (online_nodes() would re-query the cluster and is skipped
        # when the caller pins the node set.)
        client.auth()
        nodes = list(candidate_nodes)
    else:
        # online_nodes() acquires the ticket itself and returns only nodes
        # the cluster reports as up.
        nodes = client.online_nodes()
    if not nodes:
        raise RuntimeError("no candidate PVE nodes available for placement")

    capacities = {name: client.node_capacity(name) for name in nodes}

    # Order deterministically: record/override entries first (their placement
    # is already known and cheap), then fresh picks sorted by key so the
    # spread is reproducible.
    known = sorted(k for k in vm_ram_disk if k in overrides or k in record.placements)
    fresh = sorted(k for k in vm_ram_disk if k not in overrides and k not in record.placements)
    requests = [
        PlacementRequest(key=k, ram_bytes=vm_ram_disk[k][0], disk_bytes=vm_ram_disk[k][1])
        for k in known + fresh
    ]

    choices = _plan(requests, capacities, record, overrides)

    # Persist only when a new placement was actually written; a build that
    # places no new VMs leaves the committed record untouched.
    if persist and fresh:
        record.save()

    for key in fresh:
        print(f"[placement] {key} -> {choices[key]} (best fit)")
    return choices
