"""Durable static remote-FDB ``dst`` entries for the PVE SDN vxlan overlay.

Root cause this module exists for
---------------------------------
PVE 9.2 materializes the SDN vnet bridge (``vlab``) and the ``vxlan_vlab``
device from the generated ``/etc/network/interfaces.d/sdn`` (``vxlan-id`` +
``vxlan_remoteip`` lines) but has **no runtime daemon** to program the
kernel's remote-FDB ``dst`` bindings from those lines. Without a static
``bridge fdb append <mac> dev vxlan_vlab dst <peer-underlay>`` entry for each
remote VM MAC, cross-node encap is structurally impossible and the overlay is
dead until the FDB is applied by hand. A manual apply is runtime-only: it is
wiped on reboot *and* on every ``pve-sdn-commit`` (which re-runs
``ifreload`` and regenerates the interfaces file). That makes the build
non-repeatable.

What this module does
--------------------
On every PVE node, over root password-SSH (the same pexpect +
fallback-password mechanism as ``modules/pve_cleanup.py``):

1. Query PVE for the live VMID set on the node (``GET /nodes/<n>/qemu``,
   same client as pve_cleanup) and intersect it with the estate's SDN NIC
   index map to find this node's SDN taps (``/sys/class/net/tap<vmid>i<idx>``).
2. Read the taps' MACs on the node itself, so the overlay map always reflects
   the *live* MACs and survives VM recreation with no drift.
3. Write ``/etc/sdn-vlab-fdb/sdn-vlab-fdb.txt``: one ``<mac> <peer-underlay>``
   line per remote SDN VM MAC plus the multicast/broadcast ``dst`` lines, so a
   node's FDB always points at the underlay that hosts the remote VM.
   This path is deliberately **not** ``/etc/pve``: that tree is replicated
   cluster-wide by csync2 on PVE, so a per-node data file there clobbers the
   others (last write wins) and a clean node loses its remote targetings.
4. Install ``/etc/network/if-up.d/sdn-vlab-fdb`` and fire it now. The hook
   re-applies the data file's ``dst`` entries every time ``vxlan_vlab``
   comes up -- i.e. at boot and after every ``pve-sdn-commit`` -- so the
   overlay self-heals and the whole build is repeatable from scratch.

The apply is skipped during ``pulumi preview`` / dry-run. Verification is
advisory: a just-created tap that is not up yet is reported missing and is
left to the hook to heal on the next ``if-up``.
"""

from __future__ import annotations

import base64
import os
import re
import shlex
import sys
from dataclasses import dataclass, field

from .pve_cleanup import CleanupSettings, PveClient

# PVE host root SSH password file. Same default as the pve_cleanup fallback so
# the whole estate authenticates from one place.
DEFAULT_HOST_PASSWORD_FILE = "~/.proxmoxpass"

# Host-SSH knobs mirror modules/pve_cleanup.py so failures surface the same
# way (exit marker + redaction) and the module stays stdlib-only at import.
_SSH_CMD_TIMEOUT_S = 120
_SSH_EXIT_MARKER = "SDN_FDB_SSH_EXIT="
_SSH_CLIENT_OPTS = [
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "BatchMode=no",
    "-o", "ConnectTimeout=10",
]

# Well-known MACs that must also carry a per-peer dst so the overlay carries
# multicast and the broadcast domain across nodes.
_MULTICAST_VRRP_MAC = "33:33:00:00:01"
_BROADCAST_MAC = "ff:ff:ff:ff:ff:ff"


@dataclass(frozen=True)
class SdnFdbSettings:
    endpoint: str
    username: str
    password: str
    insecure: bool = True
    node_names: tuple[str, ...] = ("pve01", "pve02", "pve03")
    # Underlay (management) IP for each node, in node_names order. This is
    # the vxlan peer IP the ``dst`` FDB entry must point at.
    peers: tuple[str, ...] = ("192.168.1.21", "192.168.1.22", "192.168.1.23")
    # The vxlan device name (matches the auto/iface name PVE generated).
    vxlan_device: str = "vxlan_vlab"
    # Where the data file and the if-up.d hook live on the host. The data
    # file is per-node and MUST sit on a non-replicated path: /etc/pve is
    # synced cluster-wide by csync2, so a node-local file there gets clobbered
    # by the next node's write. /etc/sdn-vlab-fdb is node-local.
    data_file: str = "/etc/sdn-vlab-fdb/sdn-vlab-fdb.txt"
    data_dir: str = "/etc/sdn-vlab-fdb"
    hook_file: str = "/etc/network/if-up.d/sdn-vlab-fdb"
    # PVE host root SSH password file for the host-SSH path.
    fallback_password_file: str = DEFAULT_HOST_PASSWORD_FILE
    # Optional node-name -> SSH-host map; defaults to the node name (which
    # resolves from the operator host when it does).
    node_ssh_hosts: dict[str, str] = field(default_factory=dict)
    # vmid -> position of the vlab-attached NIC in the VM's NIC list. The PVE
    # tap device is /sys/class/net/tap<vmid>i<idx>. Only VMs in this map are
    # part of the SDN overlay; every other VMID on a node is ignored.
    sdn_nic_index: dict[int, int] = field(default_factory=dict)

    def underlay_for(self, node: str) -> str:
        idx = self.node_names.index(node)
        return self.peers[idx]

    def ssh_host_for(self, node: str) -> str:
        if self.node_ssh_hosts:
            return self.node_ssh_hosts.get(node, node)
        return node


def _read_fallback_password(settings: SdnFdbSettings) -> str:
    path = settings.fallback_password_file
    if not path:
        raise RuntimeError("no host password file configured")
    try:
        with open(os.path.expanduser(path)) as fh:
            password = fh.read().strip()
    except OSError as exc:
        raise RuntimeError(f"host password file unreadable: {path}") from exc
    if not password:
        raise RuntimeError(f"host password file is empty: {path}")
    return password


def _host_ssh(settings: SdnFdbSettings, node: str, remote: str) -> tuple[bool, str]:
    """Run ``remote`` on ``node`` over root password-SSH.

    Returns (ok, output). ``ok`` is True only when the remote shell printed a
    ``SDN_FDB_SSH_EXIT=0`` marker. The password is redacted from all output.
    Mirrors modules/pve_cleanup._remove_volume_via_ssh (pexpect + exit marker
    + redaction) so the two modules fail and log identically.
    """
    import pexpect

    password = _read_fallback_password(settings)
    host = settings.ssh_host_for(node)
    cmd = f"{remote} 2>&1; echo {_SSH_EXIT_MARKER}$?"
    remote_full = f"bash -lc {shlex.quote(cmd)}"
    child = pexpect.spawn(
        "ssh", _SSH_CLIENT_OPTS + [f"root@{host}", remote_full],
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
    markers = re.findall(re.escape(_SSH_EXIT_MARKER) + r"(\d+)", out)
    if markers:
        ok = int(markers[-1]) == 0
        return ok, out if ok else f"remote shell exit {markers[-1]}: {out[-400:]}"
    if timed_out:
        return False, f"ssh to {host} timed out after {_SSH_CMD_TIMEOUT_S}s"
    code = child.exitstatus
    return (code in (0, None)), out


def _discover_sdn_macs(client: PveClient, ticket: str, settings: SdnFdbSettings) -> dict[str, list[str]]:
    """Return {node: [mac, ...]} of the SDN-attached NIC MACs on each node.

    The MAC source is the PVE VM config (``GET /nodes/<n>/qemu/<vmid>/config``
    -> ``net<idx>``), which is the PVE-assigned MAC the VM carries at steady
    state after a cold boot. This is the authoritative, repeatable source: a
    live hypervisor tap is runtime-transient and drifts from this until the
    VM reboots, so encoding the live tap would break the from-scratch build.
    """
    macs: dict[str, list[str]] = {}
    for node in settings.node_names:
        try:
            vmids = client.node_vmids(ticket, node)
        except RuntimeError as exc:
            print(f"[sdn_fdb] {node}: VM list unavailable ({exc}); skipping", file=sys.stderr)
            macs[node] = []
            continue
        node_macs: list[str] = []
        for vmid in sorted(vmids & set(settings.sdn_nic_index)):
            nic_idx = settings.sdn_nic_index[vmid]
            mac = client.node_vm_mac(ticket, node, vmid, nic_idx)
            if mac:
                node_macs.append(mac)
        macs[node] = node_macs
    return macs


def _fdb_lines_for(node: str, settings: SdnFdbSettings, macs: dict[str, list[str]]) -> list[str]:
    """The FDB data-file lines for ``node``: every *remote* VM MAC plus the
    per-peer multicast/broadcast dsts, each mapped to the remote node's
    underlay IP.
    """
    lines: list[str] = []
    for remote in settings.node_names:
        if remote == node:
            continue
        peer = settings.underlay_for(remote)
        for mac in macs.get(remote, []):
            lines.append(f"{mac} {peer}")
        lines.append(f"{_MULTICAST_VRRP_MAC} {peer}")
        lines.append(f"{_BROADCAST_MAC} {peer}")
    return lines


def _hook_script(settings: SdnFdbSettings) -> str:
    """The if-up.d hook body: re-apply this node's FDB dst entries whenever
    the vxlan device (or its bridge) comes up. Idempotent and defensive: a
    missing data file or a not-yet-up interface just no-ops so it can never
    block network startup.
    """
    dev = settings.vxlan_device
    data = settings.data_file
    return f"""#!/bin/sh
# hook
# Durable SDN vxlan remote-FDB dst bindings (see modules/sdn_fdb.py).
# PVE 9.2 has no daemon to program the kernel FDB from vxlan_remoteip, so
# without this the cross-node overlay is dead until someone runs it by
# hand. Runs on every if-up of the vxlan device: boot and pve-sdn-commit.
if [ "${{IFACE:-$1}}" != "{dev}" ] && [ "${{IFACE:-$1}}" != "vlab" ]; then
    exit 0
fi
[ -r "{data}" ] || exit 0
# ``|| [ -n "$mac" ]`` guards a final line that lacks a trailing newline
# (a bare ``while read`` would silently drop it, losing that peer's dst).
while read -r mac peer _ || [ -n "$mac" ]; do
    [ -n "$mac" ] || continue
    case "$mac" in \\#*) continue ;; esac
    [ -n "$peer" ] || continue
    # static dst entry; append is idempotent (a duplicate is a no-op warning).
    bridge fdb append "$mac" dev {dev} dst "$peer" 2>/dev/null || true
done < "{data}"
exit 0
"""


def _install_node(settings: SdnFdbSettings, node: str, macs: dict[str, list[str]]) -> list[str]:
    """Write the data file + install + fire the hook on one node. Returns log
    lines. Also verifies which expected entries are actually present.
    """
    lines: list[str] = []
    fdb_lines = _fdb_lines_for(node, settings, macs)

    # Stage both files atomically (write to a temp, then move) so a
    # mid-write crash never leaves a half-installed hook. The file MUST end
    # with a newline: the hook reads it with ``while read``, which silently
    # drops a final line that is not newline-terminated. The last line is the
    # broadcast ``dst`` for the highest-indexed peer, so a missing newline
    # would deaden the overlay's broadcast route to that peer.
    data_body = "\n".join(fdb_lines) + "\n"
    hook_body = _hook_script(settings)
    # Base64 the payloads to dodge every layer of shell quoting.
    data_b64 = base64.b64encode(data_body.encode()).decode()
    hook_b64 = base64.b64encode(hook_body.encode()).decode()

    script = (
        f"set -e; "
        f"mkdir -p {settings.data_dir}; "
        f"echo {data_b64} | base64 -d > {settings.data_file}.new; "
        f"mv {settings.data_file}.new {settings.data_file}; "
        f"echo {hook_b64} | base64 -d > {settings.hook_file}.new; "
        f"chmod 755 {settings.hook_file}.new; "
        f"mv {settings.hook_file}.new {settings.hook_file}; "
        # Fire it now so the overlay is live without waiting for a reload.
        f"IFACE={settings.vxlan_device} {settings.hook_file} || true; "
        f"sleep 1; "
        f"echo FDB_COUNT=$(bridge fdb show dev {settings.vxlan_device} 2>/dev/null | wc -l)"
    )
    ok, out = _host_ssh(settings, node, script)
    count = re.search(r"FDB_COUNT=(\d+)", out)
    lines.append(
        f"{node}: {len(fdb_lines)} dst line(s); "
        + (f"FDB entries now {count.group(1)}" if count else f"apply FAILED: {out[-300:]}")
    )
    if not ok:
        lines.append(f"{node}: host-ssh reported failure: {out[-300:]}")

    # Advisory verification: is every expected VM-mac dst actually present?
    expected = [
        ln.split()[0]
        for ln in fdb_lines
        if ln.split()[0] not in (_MULTICAST_VRRP_MAC, _BROADCAST_MAC)
    ]
    if expected:
        probe = (
            f"bridge fdb show dev {settings.vxlan_device} 2>/dev/null > /tmp/.fdb_show; "
            + " ".join(
                f'grep -qi "^{m} " /tmp/.fdb_show && echo "{m} present" || echo "{m} missing"'
                for m in expected
            )
        )
        _ok2, out2 = _host_ssh(settings, node, probe)
        missing = [ln.split()[0] for ln in out2.splitlines() if "missing" in ln]
        if missing:
            lines.append(f"{node}: MISSING dst (hook will heal on next if-up): {', '.join(missing)}")
    return lines


def ensure_sdn_fdb(settings: SdnFdbSettings, dry_run: bool = False) -> list[str]:
    """Install + apply the durable SDN FDB overlay. Skipped on dry-run/preview.

    Returns a list of human-readable log lines (for pulumi.log / the build).
    """
    if dry_run:
        return ["sdn-fdb: skipped (dry run / preview)"]

    client = PveClient(
        CleanupSettings(
            endpoint=settings.endpoint,
            username=settings.username,
            password=settings.password,
            insecure=settings.insecure,
            node_names=settings.node_names,
            datastore_ids=(),
        )
    )
    ticket, _csrf = client.auth()

    macs = _discover_sdn_macs(client, ticket, settings)
    lines: list[str] = [
        "sdn-fdb: discovered MACs "
        + ", ".join(f"{n}={len(macs.get(n, []))}" for n in settings.node_names)
    ]
    for node in settings.node_names:
        lines.extend(_install_node(settings, node, macs))
    return lines


def build_settings_from_env() -> SdnFdbSettings:
    """Standalone entry settings from the same env vars the provider reads."""
    endpoint = os.getenv("PROXMOX_VE_ENDPOINT", "")
    username = os.getenv("PROXMOX_VE_USERNAME", "root@pam")
    password = os.getenv("PROXMOX_VE_PASSWORD", "")
    node_names = tuple(
        n.strip()
        for n in os.getenv("PROXMOX_VE_NODES", "pve01,pve02,pve03").split(",")
        if n.strip()
    )
    peers = tuple(
        p.strip()
        for p in os.getenv(
            "PROXMOX_VE_SDN_PEERS", "192.168.1.21,192.168.1.22,192.168.1.23"
        ).split(",")
        if p.strip()
    )
    # Standalone default: the 8 estate VMs and the index of their vlab NIC
    # (repo01 is nic index 1, everything else index 0).
    return SdnFdbSettings(
        endpoint=endpoint,
        username=username,
        password=password,
        insecure=os.getenv("PROXMOX_VE_INSECURE", "true").lower() in {"1", "true", "yes", "on"},
        node_names=node_names,
        peers=peers,
        fallback_password_file=os.getenv("PROXMOX_HOST_PASSWORD_FILE") or DEFAULT_HOST_PASSWORD_FILE,
        node_ssh_hosts={
            "pve01": "192.168.1.21",
            "pve02": "192.168.1.22",
            "pve03": "192.168.1.23",
        },
        sdn_nic_index={
            2001: 1, 2002: 0, 2101: 0, 2102: 0, 2103: 0, 2201: 0, 2202: 0, 2203: 0,
        },
    )


def run_standalone(argv: list[str]) -> int:
    """CLI entry: ``python3 -m modules.sdn_fdb [--apply]``. Without
    ``--apply`` it discovers + logs; with it, it installs the hook and fires
    the FDB. Uses PROXMOX_VE_* + PROXMOX_HOST_PASSWORD_FILE."""
    apply = "--apply" in argv
    settings = build_settings_from_env()
    lines = ensure_sdn_fdb(settings, dry_run=not apply)
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(run_standalone(sys.argv[1:]))
