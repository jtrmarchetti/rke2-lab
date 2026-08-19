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
    # The CPU model QEMU presents to the guest.
    #
    # Proxmox defaults to kvm64, which advertises a Pentium 4-era feature set:
    # no SSE4.2, no POPCNT, and therefore not x86-64-v2. Every RHEL 9 and UBI 9
    # image requires x86-64-v2 and refuses to start without it, with the glibc
    # message "CPU does not support x86-64-v2" — which names the CPU when the
    # thing to change is the VM definition. Phase 6b met this on Keycloak.
    #
    # x86-64-v2-AES rather than `host`: it is the oldest model that satisfies
    # the requirement, so a guest stays migratable to any host in a future
    # cluster rather than being pinned to this machine's exact silicon.
    #
    # Changing this on an existing VM needs a full power cycle. A reboot from
    # inside the guest keeps the running QEMU process, and the CPU it presents
    # with it.
    cpu_type: str = "x86-64-v2-AES"


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
    # Proxmox disk cache mode. Deliberately "writeback" rather than the
    # provider default of none/direct I/O.
    #
    # etcd's write path is a serialised fsync per raft commit, so it is bound by
    # fsync latency and not by throughput. Measured on this lab's storage: 63
    # MB/s sequential, but ~26 ms per 4 KB fsync — roughly 38 sequential IOPS,
    # below etcd's documented 50 IOPS floor for even a light cluster. With
    # cache=none every etcd commit waits on the physical disk, and a three
    # member control plane cannot keep up: heartbeats miss, applies take
    # seconds, rke2-server dies, and the cluster loses quorum.
    #
    # writeback lets the Proxmox host page cache acknowledge the guest's fsync.
    # The cost is real and applies to every VM in this lab, not just the cluster
    # ones: a host crash or power failure can lose writes the guest believes are
    # durable, including GitLab's database. That is an acceptable trade for a
    # rebuildable dev environment and would not be acceptable in production.
    #
    # The physical host's backing storage is an SSD array, so fsync latency is
    # one order of magnitude below the ~26 ms measured earlier on the spinning
    # disk the lab started on. writeback stays the safer choice for the same
    # reasons as above, and it is the mode Proxmox documents as giving a good
    # balance between safety and speed for block-storage backings.
    disk_cache: str = "writeback"
    # IO threads move each disk's I/O off the vCPU threads and the main QEMU
    # event loop into a dedicated thread. On an SSD-backed storage the host's
    # I/O completes fast enough that the only thing left to serialise is the
    # QEMU user-space path, and giving it its own thread removes that
    # serialisation entirely. Proxmox recommends exactly this combination
    # (virtio-scsi-single + IO Thread) for performance and makes it the default
    # for newly created Linux VMs since 7.3.
    #
    # The trade is one extra host thread per disk per VM. With 8 VMs and 2-3
    # disks each that is at most ~24 threads on a lab host, which is
    # negligible.
    disk_io_thread: bool = True
    # virtio-scsi-single rather than the virtio-scsi-pci multi-controller
    # option: iothread is only valid with virtio-scsi-single or virtio-blk,
    # and single is what Proxmox uses for exactly this purpose.
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

        # When no template exists, import the first disk from a cloud image.
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
