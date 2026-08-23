from dataclasses import dataclass

import pulumi
import pulumi_proxmoxve as proxmox

@dataclass(frozen=True)
class VmNicSpec:
    bridge: str
    ipv4_cidr: str
    ipv4_gateway: str | None = None
    model: str = "virtio"

@dataclass(frozen=True)
class VmSpec:
    key: str
    phase: int
    hostname: str
    vm_id: int
    cpu_cores: int
    cpu_sockets: int
    memory_mb: int
    disks_gb: list[int]
    dns_servers: list[str]
    nics: list[VmNicSpec]
    tags: list[str]
    cpu_type: str = "host"

@dataclass(frozen=True)
class VmCommonSettings:
    template_node_name: str
    template_vm_id: int | None
    datastore_id: str
    cloud_init_datastore_id: str
    vm_username: str
    vm_ssh_public_key: str
    vm_user_password: str | None
    vm_domain: str
    disk_file_format: str | None
    disk_cache: str = "writeback"
    disk_io_thread: bool = True
    scsi_hardware: str = "virtio-scsi-single"

def _disk_args(
    common: VmCommonSettings,
    disks_gb: list[int],
    boot_image_file_id: pulumi.Input[str] | None,
) -> list[proxmox.VmLegacyDiskArgs]:
    disk_args: list[proxmox.VmLegacyDiskArgs] = []
    for idx, disk_gb in enumerate(disks_gb):
        disk_kwargs = {
            "interface": f"scsi{idx}",
            "datastore_id": common.datastore_id,
            "size": disk_gb,
        }

        if common.disk_file_format:
            disk_kwargs["file_format"] = common.disk_file_format

        if common.disk_cache:
            disk_kwargs["cache"] = common.disk_cache

        if common.disk_io_thread:
            disk_kwargs["iothread"] = True

        if idx == 0 and common.template_vm_id is None and boot_image_file_id is not None:
            disk_kwargs["import_from"] = boot_image_file_id

        disk_args.append(proxmox.VmLegacyDiskArgs(**disk_kwargs))
    return disk_args

def _network_args(nics: list[VmNicSpec]) -> list[proxmox.VmLegacyNetworkDeviceArgs]:
    network_args: list[proxmox.VmLegacyNetworkDeviceArgs] = []
    for nic in nics:
        network_args.append(
            proxmox.VmLegacyNetworkDeviceArgs(
                bridge=nic.bridge,
                model=nic.model,
            )
        )
    return network_args

def _ip_config_args(nics: list[VmNicSpec]) -> list[proxmox.VmLegacyInitializationIpConfigArgs]:
    ip_args: list[proxmox.VmLegacyInitializationIpConfigArgs] = []
    for nic in nics:
        ip_args.append(
            proxmox.VmLegacyInitializationIpConfigArgs(
                ipv4=proxmox.VmLegacyInitializationIpConfigIpv4Args(
                    address=nic.ipv4_cidr,
                    gateway=nic.ipv4_gateway,
                )
            )
        )
    return ip_args

def create_vm(
    spec: VmSpec,
    common: VmCommonSettings,
    provider: proxmox.Provider,
    boot_image_file_id: pulumi.Input[str] | None = None,
    depends_on: list[pulumi.Resource] | None = None,
) -> proxmox.VmLegacy:
    clone_args = None
    if common.template_vm_id is not None:
        clone_args = proxmox.VmLegacyCloneArgs(
            node_name=common.template_node_name,
            vm_id=common.template_vm_id,
            full=True,
        )

    vm = proxmox.VmLegacy(
        resource_name=spec.key,
        node_name=common.template_node_name,
        vm_id=spec.vm_id,
        name=spec.hostname,
        tags=spec.tags,
        on_boot=True,
        stop_on_destroy=True,
        cdrom=proxmox.VmLegacyCdromArgs(file_id="none"),
        serial_devices=[{}],
        operating_system=proxmox.VmLegacyOperatingSystemArgs(type="l26"),
        scsi_hardware=common.scsi_hardware,
        cpu=proxmox.VmLegacyCpuArgs(
            cores=spec.cpu_cores,
            sockets=spec.cpu_sockets,
            type=spec.cpu_type,
        ),
        memory=proxmox.VmLegacyMemoryArgs(
            dedicated=spec.memory_mb,
        ),
        clone=clone_args,
        disks=_disk_args(common, spec.disks_gb, boot_image_file_id),
        network_devices=_network_args(spec.nics),
        initialization=proxmox.VmLegacyInitializationArgs(
            type="nocloud",
            datastore_id=common.cloud_init_datastore_id,
            dns=proxmox.VmLegacyInitializationDnsArgs(
                domain=common.vm_domain,
                servers=spec.dns_servers,
            ),
            ip_configs=_ip_config_args(spec.nics),
            user_account=proxmox.VmLegacyInitializationUserAccountArgs(
                username=common.vm_username,
                password=common.vm_user_password,
                keys=[common.vm_ssh_public_key],
            ),
        ),
        opts=pulumi.ResourceOptions(provider=provider, depends_on=depends_on),
    )
    return vm
