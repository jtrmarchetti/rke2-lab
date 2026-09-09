"""Cluster-wide internal LAN via PVE SDN: vxlan zone + vnet + apply step.

The vxlan zone spans every node and the vnet's bridge name is the VMs'
internal attach target (``virtio,bridge=<vnet-id>``). Creating the zone/vnet
resources alone only materializes on the committing node, so the imperative
``apply_sdn`` step fires PVE's first-class ``PUT /cluster/sdn/`` endpoint
(the GUI "Apply" button) to reload SDN + networking on ALL nodes. Verified
end-to-end in ``test_sdn``.

The apply step is skipped during ``pulumi preview`` (PULUMI_PREVIEW set by
the CLI) so previews stay side-effect free, and re-runs idempotently on
every real ``pulumi up`` (a re-apply just re-reloads networking).
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

import pulumi
import pulumi_proxmoxve as proxmox

TASK_TIMEOUT_S = 300


@dataclass(frozen=True)
class SdnSettings:
    endpoint: str
    username: str
    password: str
    insecure: bool = True
    zone_id: str = "labvx"
    vnet_id: str = "vlab"
    vxlan_tag: int = 42
    node_names: tuple[str, ...] = ("pve01", "pve02", "pve03")
    # Peer IP addresses for the vxlan fabric. Must match the nodes.
    peers: tuple[str, ...] = ("192.168.1.21", "192.168.1.22", "192.168.1.23")


def _apply_once(endpoint: str, username: str, password: str, insecure: bool) -> str | None:
    """PUT /cluster/sdn/ then wait for the reload task to finish.

    Auth flow (PAM ticket, same as ``modules/pve_cleanup.py``): POST
    /access/ticket -> ticket + CSRFPreventionToken; subsequent requests carry
    ``PVEAuthCookie=<ticket>`` and (for writes) the ``CSRFPreventionToken``
    header. The finished reload task is queryable only in the *cluster* task
    list (``GET /cluster/tasks``); a running entry has no ``status`` key and a
    finished entry gains ``status: "OK"``. ~20s observed live.
    """
    ctx = ssl._create_unverified_context() if insecure else ssl.create_default_context()
    host = urllib.parse.urlparse(endpoint).netloc or endpoint
    base = f"https://{host}/api2/json"

    def request(
        method: str, path: str, ticket: str, csrf: str = "", form: dict | None = None
    ) -> tuple[int, dict | list]:
        url = f"{base}{path}"
        data = None
        headers: dict[str, str] = {"Cookie": f"PVEAuthCookie={ticket}"}
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if method in ("POST", "PUT", "DELETE") and csrf:
            headers["CSRFPreventionToken"] = csrf
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
                body = resp.read().decode()
                return resp.status, (json.loads(body) if body else {})
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()
            try:
                return exc.code, json.loads(body)
            except (json.JSONDecodeError, ValueError):
                return exc.code, {"message": body[:400]}

    status, payload = request(
        "POST",
        "/access/ticket",
        "",
        form={"new-format": 1, "username": username, "password": password},
    )
    data = payload.get("data") or {} if isinstance(payload, dict) else {}
    ticket = data.get("ticket", "")
    if status != 200 or not ticket:
        raise RuntimeError(f"Proxmox ticket auth failed ({status}): {payload}")
    csrf = data.get("CSRFPreventionToken", "")

    status, payload = request("PUT", "/cluster/sdn/", ticket, csrf)
    if status != 200:
        raise RuntimeError(f"SDN apply failed ({status}): {payload}")
    upid = (payload.get("data") if isinstance(payload, dict) else None) or None
    if not upid:
        return None  # applied synchronously

    deadline = time.time() + TASK_TIMEOUT_S
    while time.time() < deadline:
        status, payload = request("GET", "/cluster/tasks", ticket)
        entries = payload.get("data") if isinstance(payload, dict) else None
        for entry in entries or []:
            if not isinstance(entry, dict) or entry.get("upid") != upid:
                continue
            tstatus = entry.get("status")
            if tstatus is None:
                break  # still running
            if tstatus == "OK":
                return upid
            raise RuntimeError(
                f"SDN apply task {upid} finished with {tstatus}: "
                f"{entry.get('statusmsg', '')}"
            )
        time.sleep(5)
    raise TimeoutError(f"SDN apply task {upid} not finished within {TASK_TIMEOUT_S}s")


def apply_sdn(endpoint: str, username: str, password: str, insecure: bool) -> None:
    """Fire the cluster-wide SDN apply. Skipped during ``pulumi preview``.

    Gated on ``pulumi.runtime.is_dry_run()`` (the authoritative signal for a
    preview/dry-run) rather than the PULUMI_PREVIEW env var, which the Pulumi
    CLI does not reliably export into the program process.
    """
    if pulumi.runtime.is_dry_run():
        pulumi.log.info("pve-sdn-apply: skipped (dry run / preview)")
        return
    upid = _apply_once(endpoint, username, password, insecure)
    pulumi.log.info(f"pve-sdn-apply: cluster-wide SDN apply done (task {upid})")


def build_internal_lan(
    settings: SdnSettings,
    provider: proxmox.Provider,
) -> tuple[proxmox.sdn.zone.vxlan.Vxlan, proxmox.sdn.vnet.Vnet]:
    """Register the vxlan zone + vnet resources (no imperative apply here).

    Returns (zone, vnet). The vnet's ``id`` is the bridge name VMs attach to.

    The cluster-wide ``apply_sdn`` reload is deliberately NOT fired here: this
    function runs at Pulumi *registration* time, i.e. before the provider has
    actually created the zone/vnet objects (or any VMs). Firing the reload now
    would reload an *empty* SDN config on a first-from-scratch up and the
    bridges would only materialize on a second up -- not "repeatable from
    scratch". The caller defers ``apply_sdn`` into a post-creation
    ``pulumi.Output.all([...]).apply()`` callback (see
    infra/pulumi/__main__.py), so the reload always sees the objects that are
    about to be applied.
    """
    zone = proxmox.sdn.zone.vxlan.Vxlan(
        resource_name="sdn-vxlan-zone",
        resource_id=settings.zone_id,
        peers=list(settings.peers),
        nodes=list(settings.node_names),
        opts=pulumi.ResourceOptions(provider=provider),
    )
    vnet = proxmox.sdn.vnet.Vnet(
        resource_name="sdn-vxlan-vnet",
        resource_id=settings.vnet_id,
        zone=zone.id,
        tag=settings.vxlan_tag,
        opts=pulumi.ResourceOptions(provider=provider, depends_on=[zone]),
    )
    return zone, vnet
