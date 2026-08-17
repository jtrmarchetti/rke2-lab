from dataclasses import dataclass

import pulumi_proxmoxve as proxmox


@dataclass(frozen=True)
class ProviderSettings:
    endpoint: str
    username: str
    password: str
    insecure: bool
    node_name: str


def build_provider(settings: ProviderSettings) -> proxmox.Provider:
    return proxmox.Provider(
        resource_name="proxmox-provider",
        endpoint=settings.endpoint,
        username=settings.username,
        password=settings.password,
        insecure=settings.insecure,
    )
