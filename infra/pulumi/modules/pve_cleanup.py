"""Preflight cleanup of orphan Proxmox logical volumes.

Root cause this module exists for:
-----------------------------------
Proxmox creates a small ``vm-<vmid>-cloudinit`` thin logical volume in the
cloud-init datastore *before* the VM config exists. When a failed
``qmcreate`` task is interrupted between that allocation and the rest of the
VM creation, the cloud-init LV persists with no VM to own it. PVE 9.x has no
REST endpoint that lists or deletes individual thin-LV entries (``/disks/lvm``
returns only the pool tree), and Pulumi's ``VmLegacy`` delete path only runs
when a VM resource is actually tracked, so the orphan blocks every subsequent
``qmcreate`` for that VMID:

    qmcreate failed: unable to create VM 2XXX - lvcreate
    'pve/vm-2XXX-cloudinit' error: Logical volume already exists

Detection + removal:
--------------------
The storage content endpoint *does* work on PVE 9 and is what we use:

* ``GET /nodes/<node>/qemu``                          -> which VMs exist
* ``GET /nodes/<node>/storage/<ds>/content``           -> which volumes exist
* ``DELETE /nodes/<node>/storage/<ds>/content/<volid>``

The delete is self-protecting: PVE refuses to free a volume whose VM is
running ("Logical volume in use"), so a genuine volume belonging to a live VM
is never touched. Only LVs whose VMID is absent from the node's VM list are
removed - which is exactly the orphan set.

Auth is the PAM ticket flow (PVE ``root@pam`` rejects basic-auth cookies, so
plain username/password HTTP auth 401s): POST ``/access/ticket`` with the
same credentials the provider uses, then send the ticket as a cookie and the
CSRF token on writes.

Host fallback (PVE 9.2 API 501):
----------------------------------
On PVE 9.2 the content DELETE endpoint returns HTTP 501 ("Unexpected
content for method 'DELETE'"), so the API delete above cannot complete the
cleanup and a manual host ``lvremove`` is required. To keep ``pulumi up``
self-healing, when the API delete fails and a fallback password file is
configured (``fallback_password_file``, same file as the PAM ticket
fallback), the volume is instead removed over root password-SSH on the
PVE host using ``pvesm free <datastore>:<volid>``: pvesm maps the storage
volid to the correct LVM backend (no VG hardcoding) and, like the API
path, refuses to free a volume in use by a running VM. pexpect is
imported lazily inside the fallback, so the module stays stdlib-only at
import time and the API path works even without pexpect installed.

The module is runnable standalone for diagnosis:

    python3 -m modules.pve_cleanup            # uses PROXMOX_VE_* env vars
    python3 -m modules.pve_cleanup --apply    # actually remove orphans
"""

from __future__ import annotations

import json
import os
import re
import shlex
import ssl
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class CleanupSettings:
    endpoint: str
    username: str
    password: str
    node_name: str
    datastore_ids: tuple[str, ...]
    insecure: bool = True
    fallback_password_file: str | None = None


def _read_fallback_password(settings: CleanupSettings) -> str:
    """Read the PVE host root SSH password from the configured fallback file.

    Raises RuntimeError if the file is unset, unreadable, or empty. The
    returned value is never printed; callers redact it from any captured
    output.
    """
    if not settings.fallback_password_file:
        raise RuntimeError("no fallback password file configured")
    try:
        with open(settings.fallback_password_file) as fh:
            password = fh.read().strip()
    except OSError as exc:
        raise RuntimeError(f"fallback password file unreadable: {exc}") from exc
    if not password:
        raise RuntimeError("fallback password file is empty")
    return password


class PveClient:
    """Minimal PVE API client (ticket flow, stdlib only)."""

    def __init__(self, settings: CleanupSettings):
        self.settings = settings
        host = urllib.parse.urlparse(settings.endpoint).netloc or settings.endpoint
        self.host = host
        self._ctx = ssl.create_default_context()
        if settings.insecure:
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

    @property
    def ssh_host(self) -> str:
        """SSH target host: the hostname part of settings.endpoint.

        ``urlparse`` strips any trailing slash, so ``https://192.168.1.16:8006/``
        yields ``192.168.1.16`` (no port: the fallback uses the standard
        22/SSH port, not the API port).
        """
        hostname = urllib.parse.urlparse(self.settings.endpoint).hostname
        return hostname or self.host

    def _try_ticket(self, username: str, password: str) -> tuple[str, str]:
        """POST /access/ticket; returns (ticket, csrf) or raises RuntimeError."""
        form = {
            "new-format": 1,
            "username": username,
            "password": password,
        }
        status, payload = self._request("POST", "/api2/json/access/ticket", "", "", form)
        data = payload.get("data") or {} if isinstance(payload, dict) else {}
        ticket = data.get("ticket", "")
        if status != 200 or not ticket:
            raise RuntimeError(f"Proxmox ticket auth failed ({status}): {payload}")
        return ticket, data.get("CSRFPreventionToken", "")

    def auth(self) -> tuple[str, str]:
        """Return (ticket, csrf). Tries the provider credentials first, then the
        host-password fallback file if it is configured and the provider creds
        no longer authenticate against PAM."""
        try:
            return self._try_ticket(self.settings.username, self.settings.password)
        except RuntimeError:
            if not self.settings.fallback_password_file:
                raise
            print(
                "[pve_cleanup] provider credentials rejected by PAM; "
                "falling back to host password file"
            )
            password = _read_fallback_password(self.settings)
            return self._try_ticket(self.settings.username, password)

    def _request(
        self,
        method: str,
        path: str,
        ticket: str,
        csrf: str = "",
        form: dict | None = None,
    ) -> tuple[int, dict | list]:
        url = f"https://{self.host}{path}"
        data = None
        headers = {"Cookie": f"PVEAuthCookie={ticket}"}
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if method in ("POST", "PUT", "DELETE") and csrf:
            headers["CSRFPreventionToken"] = csrf
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
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

    def node_vmids(self, ticket: str) -> set[int]:
        status, payload = self._request(
            "GET", f"/api2/json/nodes/{self.settings.node_name}/qemu", ticket
        )
        if status != 200:
            raise RuntimeError(f"Failed to list VMs on node: HTTP {status}: {payload}")
        entries = payload.get("data", payload) if isinstance(payload, dict) else payload
        return {v["vmid"] for v in entries if isinstance(v, dict) and "vmid" in v}

    def node_content(self, ticket: str, datastore: str) -> list[dict]:
        status, payload = self._request(
            "GET",
            f"/api2/json/nodes/{self.settings.node_name}/storage/{datastore}/content",
            ticket,
        )
        if status != 200:
            raise RuntimeError(
                f"Failed to list storage '{datastore}' content: HTTP {status}: {payload}"
            )
        entries = payload.get("data", payload) if isinstance(payload, dict) else payload
        return [c for c in entries if c.get("volid", "").startswith(f"{datastore}:")]

    def delete_content(
        self, ticket: str, csrf: str, datastore: str, volume: str
    ) -> str:
        """Delete a storage volume; returns '' on success, an error string otherwise."""
        status, payload = self._request(
            "DELETE",
            f"/api2/json/nodes/{self.settings.node_name}/storage/{datastore}/content/{volume}",
            ticket,
            csrf,
            {"stopvm": 0},
        )
        if status != 200:
            return f"HTTP {status}: {payload}"
        upid = str(payload.get("data", ""))
        if not upid:
            return "no task UPID returned"
        outcome = self.wait_task(ticket, upid)
        return outcome

    def wait_task(self, ticket: str, upid: str) -> str:
        for _ in range(60):
            status, payload = self._request(
                "GET", f"/api2/json/nodes/{self.settings.node_name}/tasks/{upid}/status", ticket
            )
            if status != 200:
                return f"task poll HTTP {status}: {payload}"
            data = payload.get("data", payload) if isinstance(payload, dict) else {}
            if data.get("status") == "stopped":
                exit_status = data.get("exitstatus")
                return "" if exit_status in ("OK", 0, "0") else str(exit_status)
        return "task still running after poll window"


def find_orphans(client: PveClient, ticket: str) -> list[tuple[str, str]]:
    """Return (datastore, volume_name) pairs of content whose VMID no VM owns.

    Only the cloud-init / disk image volumes (``vm-<id>-*``) are considered:
    they are the only things PVE allocates per VMID on this stack.
    """
    existing = client.node_vmids(ticket)
    orphans: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for datastore in client.settings.datastore_ids:
        try:
            content = client.node_content(ticket, datastore)
        except RuntimeError:
            continue
        for entry in content:
            vmid = entry.get("vmid")
            if vmid is None:
                volume = entry.get("volid", "").split(":", 1)[-1]
                if not volume.startswith("vm-") or volume == "vm-":
                    continue
                try:
                    vmid = int(volume.split("-", 2)[1])
                except (IndexError, ValueError):
                    continue
            if vmid in existing:
                continue
            volume = entry.get("volid", "").split(":", 1)[-1]
            key = (datastore, volume)
            if key not in seen:
                seen.add(key)
                orphans.append(key)
    return orphans


_SSH_CMD_TIMEOUT_S = 30
_SSH_EXIT_MARKER = "PVE_CLEANUP_SSH_EXIT="
_SSH_CLIENT_OPTS = [
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "BatchMode=no",
    "-o", "ConnectTimeout=10",
]


def _remove_volume_via_ssh(client: PveClient, settings: CleanupSettings, label: str) -> str:
    """Remove ``label`` (``<datastore>:<volume>``) on the PVE host over root
    password-SSH: ``pvesm free <label>``. Returns '' on success, an error
    string otherwise.

    pexpect is imported here (lazily) so the module stays stdlib-only at
    import time; if pexpect is not installed an ImportError propagates to
    the caller, which leaves the API error as the surfaced result.
    """
    import pexpect

    password = _read_fallback_password(settings)
    host = client.ssh_host
    cmd = f"pvesm free {shlex.quote(label)} 2>&1; echo {_SSH_EXIT_MARKER}$?"
    remote = f"bash -lc {shlex.quote(cmd)}"
    child = pexpect.spawn(
        "ssh", _SSH_CLIENT_OPTS + [f"root@{host}", remote],
        encoding="utf-8",
        timeout=_SSH_CMD_TIMEOUT_S,
    )
    output_parts: list[str] = []
    timed_out = False
    try:
        idx = child.expect(["assword:", pexpect.TIMEOUT, pexpect.EOF])
        output_parts.append(child.before or "")
        if idx == 0:
            child.sendline(password)
            idx = child.expect([pexpect.TIMEOUT, pexpect.EOF])
            output_parts.append(child.before or "")
            timed_out = idx == 0
        else:
            timed_out = idx == 1
    except pexpect.TIMEOUT:
        timed_out = True
    finally:
        child.close()

    out = "\n".join(output_parts).replace(password, "<redacted>")
    if timed_out:
        tail = " ".join(out.split())[-400:]
        return f"ssh to {host} timed out after {_SSH_CMD_TIMEOUT_S}s" + (f": {tail}" if tail else "")
    markers = re.findall(re.escape(_SSH_EXIT_MARKER) + r"(\d+)", out)
    if markers:
        code = int(markers[-1])
        if code == 0:
            return ""
        body = " ".join(out.split())
        return f"pvesm free {label} exited {code}: {body[-400:]}"
    body = " ".join(out.split())
    ssh_exit = child.exitstatus
    if ssh_exit is not None and ssh_exit != 0:
        return f"ssh to {host} failed (exit {ssh_exit}): {body[-400:]}"
    return f"ssh to {host} returned no remote exit status: {body[-400:]}"


def clean_orphans(settings: CleanupSettings, apply: bool = False) -> list[str]:
    """Remove orphan per-VM storage content. Returns a list of human-readable
    result lines (for logging) and performs deletion only when ``apply`` is set.

    On apply, each volume is first deleted via the API; any failure (in
    particular PVE 9.2's 501 on DELETE /content) is retried via the host
    SSH fallback when ``fallback_password_file`` is configured.
    """
    client = PveClient(settings)
    ticket, csrf = client.auth()
    orphans = find_orphans(client, ticket)
    lines = [f"Proxmox preflight: {len(orphans)} orphan storage volume(s) found on node '{settings.node_name}'."]
    if not orphans:
        return lines

    api_errors: dict[str, str] = {}
    if apply:
        for datastore, volume in orphans:
            label = f"{datastore}:{volume}"
            api_errors[label] = client.delete_content(ticket, csrf, datastore, volume)
        failed = [label for label, error in api_errors.items() if error]
        if failed and settings.fallback_password_file:
            print(
                f"[pve_cleanup] host fallback: ssh to {client.ssh_host} "
                f"for {len(failed)} volume(s)"
            )
            for label in failed:
                try:
                    error = _remove_volume_via_ssh(client, settings, label)
                except ImportError:
                    print(
                        "[pve_cleanup] host fallback unavailable: "
                        "pexpect is not installed"
                    )
                    break
                if error:
                    api_errors[label] = f"API: {api_errors[label]}; host fallback: {error}"
                else:
                    api_errors[label] = ""

    for datastore, volume in orphans:
        label = f"{datastore}:{volume}"
        if not apply:
            lines.append(f"  would remove {label}")
        elif api_errors[label]:
            lines.append(f"  could not remove {label}: {api_errors[label]}")
        else:
            lines.append(f"  removed {label}")
    return lines


def run_standalone(argv: list[str]) -> int:
    """CLI entry: ``python3 -m modules.pve_cleanup [--apply]``.

    Credentials come from the same environment variables the provider reads
    (PROXMOX_VE_ENDPOINT / PROXMOX_VE_USERNAME / PROXMOX_VE_PASSWORD); the PVE
    host's root SSH password file (PROXMOX_HOST_PASSWORD_FILE, default
    ~/.proxmoxpass) is used as an auth fallback when the provider credentials
    have drifted.
    """
    apply = "--apply" in argv
    endpoint = os.getenv("PROXMOX_VE_ENDPOINT", "")
    username = os.getenv("PROXMOX_VE_USERNAME", "root@pam")
    password = os.getenv("PROXMOX_VE_PASSWORD", "")
    if not endpoint or not password:
        print("PROXMOX_VE_ENDPOINT and PROXMOX_VE_PASSWORD must be set")
        return 2
    fallback = os.getenv("PROXMOX_HOST_PASSWORD_FILE") or os.path.expanduser("~/.proxmoxpass")
    settings = CleanupSettings(
        endpoint=endpoint,
        username=username,
        password=password,
        node_name=os.getenv("PROXMOX_VE_NODE", "proxmox-kube"),
        datastore_ids=(os.getenv("PROXMOX_VE_DATASTORE", "local-lvm"),),
        fallback_password_file=fallback,
    )
    try:
        for line in clean_orphans(settings, apply=apply):
            print(line)
    except RuntimeError as exc:
        print(f"preflight cleanup failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run_standalone(sys.argv[1:]))
