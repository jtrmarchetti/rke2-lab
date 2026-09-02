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
    # The provider's SSH client is only used for the non-API upload paths
    # (PVE snippet files, which the REST API refuses on PVE 9.x). The default
    # `stream` upload mode pipes the file through an SSH shell session as the
    # host `root` user (using `sudo` where required); the PVE root@pam API
    # password IS the host root password, so it is inherited from the
    # provider block - no second credential is introduced.
    return proxmox.Provider(
        resource_name="proxmox-provider",
        endpoint=settings.endpoint,
        username=settings.username,
        password=settings.password,
        insecure=settings.insecure,
        ssh=proxmox.ProviderSshArgs(username="root"),
    )
