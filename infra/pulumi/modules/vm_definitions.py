from .vm_factory import VmNicSpec, VmSpec


def build_vm_specs(external_bridge: str, internal_bridge: str) -> dict[str, VmSpec]:
    # vm_id values are reserved for deterministic addressing and easy state tracking.
    return {
        "repo01": VmSpec(
            key="repo01",
            phase=1,
            hostname="repo01.dev.lo",
            vm_id=2001,
            # GitLab (Phase 3) is the binding constraint on this host, not the
            # Phase 1 services. GitLab's own requirements call for far more;
            # 8 GiB was the smallest size that ran it alongside Apache,
            # apt-cacher-ng, dnsmasq, and the tunnel without swapping, and the
            # memory-constrained omnibus tuning is applied on top. 10 GiB is
            # that floor plus the 2 GiB uplift applied across every host.
            #
            # This host is created at this size in Phase 1. Phase 3 resized it
            # once, historically; it does not resize it on a rebuild.
            cpu_cores=4,
            cpu_sockets=1,
            memory_mb=10240,
            disks_gb=[32, 100],
            dns_servers=["192.168.1.1"],
            nics=[
                VmNicSpec(
                    bridge=external_bridge,
                    ipv4_cidr="192.168.1.20/24",
                    ipv4_gateway="192.168.1.1",
                ),
                VmNicSpec(
                    bridge=internal_bridge,
                    ipv4_cidr="192.168.2.99/24",
                    ipv4_gateway=None,
                ),
            ],
            tags=["phase1", "repo"],
        ),
        "core01": VmSpec(
            key="core01",
            phase=2,
            hostname="core.dev.lo",
            vm_id=2002,
            cpu_cores=2,
            cpu_sockets=1,
            memory_mb=6144,
            disks_gb=[32, 100],
            dns_servers=["127.0.0.1"],
            nics=[
                VmNicSpec(
                    bridge=internal_bridge,
                    ipv4_cidr="192.168.2.4/24",
                    ipv4_gateway="192.168.2.99",
                )
            ],
            tags=["phase2", "core"],
        ),
        "kubecp01": VmSpec(
            key="kubecp01",
            phase=4,
            hostname="kubecp01.dev.lo",
            vm_id=2101,
            cpu_cores=2,
            cpu_sockets=1,
            memory_mb=6144,
            disks_gb=[32, 100],
            dns_servers=["192.168.2.4"],
            nics=[
                VmNicSpec(
                    bridge=internal_bridge,
                    ipv4_cidr="192.168.2.21/24",
                    ipv4_gateway="192.168.2.99",
                )
            ],
            tags=["phase4", "rke2", "control-plane"],
        ),
        "kubecp02": VmSpec(
            key="kubecp02",
            phase=4,
            hostname="kubecp02.dev.lo",
            vm_id=2102,
            cpu_cores=2,
            cpu_sockets=1,
            memory_mb=6144,
            disks_gb=[32, 100],
            dns_servers=["192.168.2.4"],
            nics=[
                VmNicSpec(
                    bridge=internal_bridge,
                    ipv4_cidr="192.168.2.22/24",
                    ipv4_gateway="192.168.2.99",
                )
            ],
            tags=["phase4", "rke2", "control-plane"],
        ),
        "kubecp03": VmSpec(
            key="kubecp03",
            phase=4,
            hostname="kubecp03.dev.lo",
            vm_id=2103,
            cpu_cores=2,
            cpu_sockets=1,
            memory_mb=6144,
            disks_gb=[32, 100],
            dns_servers=["192.168.2.4"],
            nics=[
                VmNicSpec(
                    bridge=internal_bridge,
                    ipv4_cidr="192.168.2.23/24",
                    ipv4_gateway="192.168.2.99",
                )
            ],
            tags=["phase4", "rke2", "control-plane"],
        ),
        "kubewk01": VmSpec(
            key="kubewk01",
            phase=5,
            hostname="kubewk01.dev.lo",
            vm_id=2201,
            cpu_cores=4,
            cpu_sockets=1,
            memory_mb=10240,
            disks_gb=[32, 100, 100],
            dns_servers=["192.168.2.4"],
            nics=[
                VmNicSpec(
                    bridge=internal_bridge,
                    ipv4_cidr="192.168.2.31/24",
                    ipv4_gateway="192.168.2.99",
                )
            ],
            tags=["phase5", "rke2", "worker"],
        ),
        "kubewk02": VmSpec(
            key="kubewk02",
            phase=5,
            hostname="kubewk02.dev.lo",
            vm_id=2202,
            cpu_cores=4,
            cpu_sockets=1,
            memory_mb=10240,
            disks_gb=[32, 100, 100],
            dns_servers=["192.168.2.4"],
            nics=[
                VmNicSpec(
                    bridge=internal_bridge,
                    ipv4_cidr="192.168.2.32/24",
                    ipv4_gateway="192.168.2.99",
                )
            ],
            tags=["phase5", "rke2", "worker"],
        ),
        "kubewk03": VmSpec(
            key="kubewk03",
            phase=5,
            hostname="kubewk03.dev.lo",
            vm_id=2203,
            cpu_cores=4,
            cpu_sockets=1,
            memory_mb=10240,
            disks_gb=[32, 100, 100],
            dns_servers=["192.168.2.4"],
            nics=[
                VmNicSpec(
                    bridge=internal_bridge,
                    ipv4_cidr="192.168.2.33/24",
                    ipv4_gateway="192.168.2.99",
                )
            ],
            tags=["phase5", "rke2", "worker"],
        ),
    }
