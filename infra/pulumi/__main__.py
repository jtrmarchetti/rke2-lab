import os
import sys

import pulumi
import pulumi.runtime
import pulumi_proxmoxve as proxmox

from modules.pve_cleanup import CleanupSettings, clean_orphans
from modules.provider import ProviderSettings, build_provider
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
    node_name=proxmox_cfg.get("nodeName") or "proxmox-rke2",
)

phase_limit = deployment_cfg.get_int("phaseLimit") or 2
external_bridge = deployment_cfg.get("externalBridge") or "vmbr0"
internal_bridge = deployment_cfg.get("internalBridge") or "vmbr1"
allow_shared_bridge = deployment_cfg.get_bool("allowSharedBridge") or False
manage_internal_bridge = deployment_cfg.get_bool("manageInternalBridge")
if manage_internal_bridge is None:
    manage_internal_bridge = True
template_vm_id = deployment_cfg.get_int("templateVmId")

if external_bridge == internal_bridge and not allow_shared_bridge:
    raise ValueError(
        "externalBridge and internalBridge must be different to match target network design. "
        "Set deployment:allowSharedBridge=true only for temporary bootstrap/testing."
    )

common_settings = VmCommonSettings(
    template_node_name=deployment_cfg.get("templateNodeName") or provider_settings.node_name,
    template_vm_id=template_vm_id,
    datastore_id=deployment_cfg.get("datastoreId") or "dev-lo-data",
    cloud_init_datastore_id=deployment_cfg.get("cloudInitDatastoreId") or "dev-lo-data",
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
image_datastore_id = deployment_cfg.get("imageDatastoreId") or "dev-lo-directory"

provider = build_provider(provider_settings)
vm_specs = build_vm_specs(external_bridge=external_bridge, internal_bridge=internal_bridge)
selected_vm_keys = deployment_cfg.get_object("selectedVmKeys")
boot_image_file_id = None
depends_on_resources: list[pulumi.Resource] = []

if manage_internal_bridge:
    internal_bridge_ports = deployment_cfg.get_object("internalBridgePorts")
    if internal_bridge_ports is None:
        internal_bridge_ports = []

    internal_bridge_resource = proxmox.network.linux.Bridge(
        resource_name="internal-network-bridge",
        node_name=provider_settings.node_name,
        name=internal_bridge,
        autostart=True,
        vlan_aware=False,
        comment="Managed by Pulumi: internal-only network bridge",
        ports=internal_bridge_ports,
        opts=pulumi.ResourceOptions(provider=provider),
    )
    depends_on_resources.append(internal_bridge_resource)

if common_settings.template_vm_id is None:
    image_url = deployment_cfg.get("baseImageUrl") or (
        "https://cloud-images.ubuntu.com/releases/noble/release-20260814/"
        "ubuntu-24.04-server-cloudimg-amd64.img"
    )
    image_name = deployment_cfg.get("baseImageFileName") or "noble-server-cloudimg-amd64.qcow2"
    image_checksum = deployment_cfg.get("baseImageChecksum") or (
        "6e40c07ae715f744f84af0bec76415cc1987dd115b4b8de437818561f01a3733"
    )

    boot_image = proxmox.download.File(
        resource_name="ubuntu2404-cloud-image",
        content_type="import",
        datastore_id=image_datastore_id,
        node_name=common_settings.template_node_name,
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
    boot_image_file_id = boot_image.id

if selected_vm_keys:
    requested = set(selected_vm_keys)
    deployment_set = [vm for key, vm in vm_specs.items() if key in requested]
else:
    deployment_set = [vm for vm in vm_specs.values() if vm.phase <= phase_limit]

if not deployment_set:
    raise ValueError("No VMs selected for deployment. Check deployment config values.")

if not pulumi.runtime.is_dry_run():
    cleanup_settings = CleanupSettings(
        endpoint=provider_settings.endpoint,
        username=provider_settings.username,
        password=provider_settings.password,
        node_name=provider_settings.node_name,
        datastore_ids=(common_settings.datastore_id,),
        insecure=provider_settings.insecure,
        fallback_password_file=os.getenv("PROXMOX_HOST_PASSWORD_FILE")
        or os.path.expanduser("~/.proxmoxpass"),
    )
    try:
        for _line in clean_orphans(cleanup_settings, apply=True):
            print(_line, file=sys.stderr)
    except RuntimeError as exc:
        print(f"[pve_cleanup] preflight skipped: {exc}", file=sys.stderr)

created = {}
for spec in deployment_set:
    resource = create_vm(
        spec=spec,
        common=common_settings,
        provider=provider,
        boot_image_file_id=boot_image_file_id,
        depends_on=depends_on_resources,
    )
    created[spec.key] = {
        "hostname": spec.hostname,
        "vm_id": spec.vm_id,
        "phase": spec.phase,
        "ip_addresses": [nic.ipv4_cidr for nic in spec.nics],
        "resource_name": resource._name,
    }

pulumi.export("proxmoxNode", provider_settings.node_name)
pulumi.export("phaseLimit", phase_limit)
pulumi.export("templateVmId", common_settings.template_vm_id)
pulumi.export("bootImageSource", "clone" if common_settings.template_vm_id else "download")
pulumi.export("manageInternalBridge", manage_internal_bridge)
pulumi.export("createdVms", created)
