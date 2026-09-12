import os
import sys
from pathlib import Path

import pulumi
import pulumi.runtime
import pulumi_proxmoxve as proxmox

from modules.pve_cleanup import CleanupSettings, clean_orphans
from modules.placement import plan_placement
from modules.provider import ProviderSettings, build_provider
from modules.sdn import SdnSettings, apply_sdn, build_internal_lan
from modules.sdn_fdb import SdnFdbSettings, ensure_sdn_fdb
from modules.vm_definitions import build_vm_specs
from modules.vm_factory import VmCommonSettings, create_vm

def _get_required_value(config: pulumi.Config, key: str, env_name: str) -> str:
    config_value = config.get(key)
    if config_value:
        return config_value

    env_value = os.getenv(env_name)
    if env_value:
        return env_value

    raise ValueError(
        f"Missing required value for '{key}'. Set config or env var {env_name}."
    )

def _get_bool_value(config: pulumi.Config, key: str, env_name: str, default: bool) -> bool:
    config_value = config.get_bool(key)
    if config_value is not None:
        return config_value

    env_value = os.getenv(env_name)
    if env_value is None:
        return default

    return env_value.lower() in {"1", "true", "yes", "on"}

proxmox_cfg = pulumi.Config("proxmox")
deployment_cfg = pulumi.Config("deployment")

# --- 3-node cluster: every node carries its own image; VMs are
# placed cluster-wide by the best-fit planner, not pinned to a host. ---
node_names = list(deployment_cfg.get_object("nodeNames") or ["pve01", "pve02", "pve03"])
management_node = deployment_cfg.get("managementNode") or node_names[0]

provider_settings = ProviderSettings(
    endpoint=_get_required_value(proxmox_cfg, "endpoint", "PROXMOX_VE_ENDPOINT"),
    username=_get_required_value(proxmox_cfg, "username", "PROXMOX_VE_USERNAME"),
    password=_get_required_value(proxmox_cfg, "password", "PROXMOX_VE_PASSWORD"),
    insecure=_get_bool_value(
        proxmox_cfg,
        "insecure",
        "PROXMOX_VE_INSECURE",
        True,
    ),
    node_name=management_node,
)

phase_limit = deployment_cfg.get_int("phaseLimit") or 2
external_bridge = deployment_cfg.get("externalBridge") or "vmbr0"
# Per-node storage (identical names across the cluster): VM disks + cloud-init
# LVs live on the lvmthin store; the boot image lives on the dir store
# (PVE 9.2 lvmthin rejects uploads).
datastore_id = deployment_cfg.get("datastoreId") or "local-lvm"
cloud_init_datastore_id = deployment_cfg.get("cloudInitDatastoreId") or "local-lvm"
image_datastore_id = deployment_cfg.get("imageDatastoreId") or "local"
template_vm_id = deployment_cfg.get_int("templateVmId")

common_settings = VmCommonSettings(
    # Fallback host when a VM has no planned placement (should not happen when
    # the planner runs); the planner's choice wins via create_vm(node_name=...).
    template_node_name=management_node,
    template_vm_id=template_vm_id,
    datastore_id=datastore_id,
    cloud_init_datastore_id=cloud_init_datastore_id,
    vm_username=deployment_cfg.get("vmUsername") or "devops",
    vm_ssh_public_key=_get_required_value(
        deployment_cfg,
        "vmSshPublicKey",
        "VM_SSH_PUBLIC_KEY",
    ),
    vm_user_password=deployment_cfg.get_secret("vmUserPassword") or os.getenv("VM_USER_PASSWORD"),
    vm_domain=deployment_cfg.get("vmDomain") or "dev.lo",
    disk_file_format=deployment_cfg.get("diskFileFormat"),
    disk_cache=deployment_cfg.get("diskCache") or "writeback",
)

provider = build_provider(provider_settings)
depends_on_resources: list[pulumi.Resource] = []

# --- Cluster-wide internal LAN (PVE SDN vxlan zone + vnet + apply step). ---
# The vnet's id is the bridge name every VM's internal NIC attaches to.
sdn_settings = SdnSettings(
    endpoint=provider_settings.endpoint,
    username=provider_settings.username,
    password=provider_settings.password,
    insecure=provider_settings.insecure,
    zone_id=deployment_cfg.get("sdnZoneId") or "labvx",
    vnet_id=deployment_cfg.get("sdnVnetId") or "vlab",
    vxlan_tag=int(deployment_cfg.get("vxlanTag") or 42),
    node_names=tuple(node_names),
    peers=tuple(
        deployment_cfg.get_object("sdnPeers")
        or ["192.168.1.21", "192.168.1.22", "192.168.1.23"]
    ),
)
internal_bridge = sdn_settings.vnet_id

# Build the SDN zone + vnet and fire the cluster-wide apply so the bridge is
# up on every node before any VM attaches to it.
sdn_zone, sdn_vnet = build_internal_lan(sdn_settings, provider)
depends_on_resources.append(sdn_vnet)

# Now that the internal attach name is known, build the VM specs and pick the
# deployment set.
vm_specs = build_vm_specs(external_bridge=external_bridge, internal_bridge=internal_bridge)
selected_vm_keys = deployment_cfg.get_object("selectedVmKeys")
if selected_vm_keys:
    requested = set(selected_vm_keys)
    deployment_set = [vm for key, vm in vm_specs.items() if key in requested]
else:
    deployment_set = [vm for vm in vm_specs.values() if vm.phase <= phase_limit]

if not deployment_set:
    raise ValueError("No VMs selected for deployment. Check deployment config values.")

# --- Per-node boot image: each node downloads its own cloud image into its
# `local` import store so ANY node can host ANY VM (PVE 9.2 lvmthin rejects
# uploads, so the image lands on the dir store, one per node). ---
boot_image_by_node: dict[str, "pulumi.Input[str]"] = {}
if template_vm_id is None:
    image_url = deployment_cfg.get("baseImageUrl") or (
        "https://cloud-images.ubuntu.com/releases/noble/release-20260814/"
        "ubuntu-24.04-server-cloudimg-amd64.img"
    )
    image_name = deployment_cfg.get("baseImageFileName") or "noble-server-cloudimg-amd64.qcow2"
    image_checksum = deployment_cfg.get("baseImageChecksum") or (
        "6e40c07ae715f744f84af0bec76415cc1987dd115b4b8de437818561f01a3733"
    )
    for node in node_names:
        image = proxmox.download.File(
            resource_name=f"ubuntu2404-cloud-image-{node}",
            content_type="import",
            datastore_id=image_datastore_id,
            node_name=node,
            url=image_url,
            file_name=image_name,
            checksum=image_checksum,
            checksum_algorithm="sha256",
            overwrite=False,
            overwrite_unmanaged=True,
            verify=not provider_settings.insecure,
            opts=pulumi.ResourceOptions(
                provider=provider,
                delete_before_replace=True,
                aliases=[
                    pulumi.Alias(type_="proxmoxve:download/fileLegacy:FileLegacy"),
                ],
            ),
        )
        boot_image_by_node[node] = image.id
        depends_on_resources.append(image)

# --- Best-fit cluster-wide placement. ---
# Query live per-node headroom, honor the committed placement record + any
# overrides, and choose a host node for every VM in this build. New choices
# are persisted so later builds never re-move an existing VM.
record_path = Path(
    deployment_cfg.get("placementRecordPath")
    or str(Path(__file__).parent / ".placement.json")
)
overrides = dict(deployment_cfg.get_object("placementOverrides") or {})
vm_ram_disk = {vm.key: common_settings.vm_footprint_bytes(vm) for vm in deployment_set}
placements = plan_placement(
    endpoint=provider_settings.endpoint,
    username=provider_settings.username,
    password=provider_settings.password,
    insecure=provider_settings.insecure,
    vm_ram_disk=vm_ram_disk,
    record_path=record_path,
    overrides=overrides,
    candidate_nodes=node_names,
    persist=not pulumi.runtime.is_dry_run(),
)

# --- Create the VMs, each on its planned node with that node's image. ---
created = {}
vm_resources: list[proxmox.VmLegacy] = []
for spec in deployment_set:
    resource = create_vm(
        spec=spec,
        common=common_settings,
        provider=provider,
        node_name=placements[spec.key],
        boot_image_by_node=boot_image_by_node or None,
        depends_on=depends_on_resources,
    )
    vm_resources.append(resource)
    created[spec.key] = {
        "hostname": spec.hostname,
        "vm_id": spec.vm_id,
        "phase": spec.phase,
        "node": placements[spec.key],
        "ip_addresses": [nic.ipv4_cidr for nic in spec.nics],
        "resource_name": resource._name,
    }

# --- Durable SDN vxlan overlay: static remote-FDB ``dst`` bindings. ---
# PVE 9.2 materializes the vnet bridge + vxlan device from the SDN interfaces
# file but has NO daemon to program the kernel remote-FDB from the
# ``vxlan_remoteip`` lines, so the cross-node overlay is dead until the dst
# entries are applied -- and a manual apply is runtime-only (wiped on reboot
# and on every ``pve-sdn-commit``). ensure_sdn_fdb encodes the durable data
# file + ``if-up.d`` hook so the overlay self-heals on boot and on every SDN
# commit, reading each SDN VM's PVE-assigned MAC from PVE's own config (the
# cold-boot MAC, not the transient live tap).
#
# Both imperative SDN writes -- the cluster-wide ``apply_sdn`` reload and
# ``ensure_sdn_fdb`` -- are deferred into a post-creation callback so they run
# only AFTER the provider has actually created the zone, vnet, and VMs.
# Running them at registration time (as before) would, on a first-from-scratch
# up, reload an empty SDN config and install FDB data files with no VM MAC
# lines, leaving inter-VM routing dead until a second up. A callback over
# pulumi.Output.all([...]).apply() defers execution to the apply phase, after
# all the listed resources exist: the reload sees the real objects and MAC
# discovery finds the PVE-assigned MACs each VM carries at create time.
# vmid -> index of the vlab-attached NIC, derived from the specs so adding a
# VM is the only thing that needs updating.
sdn_nic_index: dict[int, int] = {}
for _spec in vm_specs.values():
    for _idx, _nic in enumerate(_spec.nics):
        if _nic.bridge == internal_bridge:
            sdn_nic_index[_spec.vm_id] = _idx
            break
fdb_settings = SdnFdbSettings(
    endpoint=provider_settings.endpoint,
    username=provider_settings.username,
    password=provider_settings.password,
    insecure=provider_settings.insecure,
    node_names=tuple(node_names),
    peers=sdn_settings.peers,
    sdn_nic_index=sdn_nic_index,
    node_ssh_hosts=dict(zip(node_names, sdn_settings.peers)),
)


def _post_create_sdn(args) -> None:
    # args is the payload of
    # pulumi.Output.all(sdn_vnet.id, *[v.id for v in vm_resources]).apply():
    # the post-creation point at which the zone/vnet/VMs exist.
    #
    # Both imperative writes must stay read-only on a preview. apply_sdn gates
    # itself on is_dry_run(); ensure_sdn_fdb takes it explicitly, so pass it
    # through -- otherwise a `pulumi preview` would run the real host-SSH work.
    apply_sdn(
        sdn_settings.endpoint,
        sdn_settings.username,
        sdn_settings.password,
        sdn_settings.insecure,
    )
    try:
        for _line in ensure_sdn_fdb(fdb_settings, dry_run=pulumi.runtime.is_dry_run()):
            print(_line, file=sys.stderr)
    except RuntimeError as exc:
        print(f"[sdn_fdb] durable overlay apply skipped: {exc}", file=sys.stderr)


# Register the callback; it is a no-op on preview (apply_sdn and
# ensure_sdn_fdb both gate on is_dry_run()). Pass the resources' .id outputs
# (ResourceOutput carries the resource dependency); passing raw resource
# instances would not track a dependency and the callback would not be
# ordered after creation.
pulumi.Output.all(sdn_vnet.id, *[v.id for v in vm_resources]).apply(_post_create_sdn)

# --- Preflight orphan cleanup across every node (real runs only, not preview). ---
if not pulumi.runtime.is_dry_run():
    cleanup_settings = CleanupSettings(
        endpoint=provider_settings.endpoint,
        username=provider_settings.username,
        password=provider_settings.password,
        node_names=tuple(node_names),
        datastore_ids=(datastore_id, cloud_init_datastore_id),
        insecure=provider_settings.insecure,
        fallback_password_file=os.getenv("PROXMOX_HOST_PASSWORD_FILE")
        or os.path.expanduser("~/.proxmoxpass"),
    )
    try:
        for _line in clean_orphans(cleanup_settings, apply=True):
            print(_line, file=sys.stderr)
    except RuntimeError as exc:
        print(f"[pve_cleanup] preflight skipped: {exc}", file=sys.stderr)

pulumi.export("managementNode", management_node)
pulumi.export("nodeNames", node_names)
pulumi.export("phaseLimit", phase_limit)
pulumi.export("templateVmId", template_vm_id)
pulumi.export("bootImageSource", "clone" if template_vm_id else "download")
pulumi.export("sdnZone", sdn_zone.id)
pulumi.export("sdnVnet", sdn_vnet.id)
pulumi.export("placements", placements)
pulumi.export("createdVms", created)
