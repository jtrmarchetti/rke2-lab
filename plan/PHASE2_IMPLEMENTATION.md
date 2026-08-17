# Phase 2 Implementation Plan — Core Server (Identity and DNS)

## Scope

Establish the identity and DNS authority for `dev.lo` by creating the `core01` VM and
running the FreeIPA stack, which provides LDAP, DNS, NTP, and certificate services for
the internal environment.

`core01` sits on the internal network with no internet access. Every package and image
it needs comes from `repo01` — Ubuntu packages through the APT proxy, the FreeIPA image
and install files from the Apache artifact host.

## Decision

| | |
| --- | --- |
| Primary path | Use the existing Pulumi VM model for `core01`, enabled through the deployment config; run FreeIPA as a container with inventory-driven configuration |
| Fallback path | If the domain cannot be bootstrapped cleanly, keep the VM provisioning path and defer the full identity install while preserving the core host baseline |

## Preconditions

- `repo01` is operating as the internal network route and proxy point.
- The controller reaches the internal subnet through the WireGuard tunnel.
- The FreeIPA container image and install files are staged on the Apache artifact host,
  with published checksums.
- The `dev.lo` domain is reserved for internal-use DNS and identity services.

## Repository Layout

```text
ansible/
  inventory/
    hosts.yml
    group_vars/
      core/
        main.yml
  playbooks/
    core01.yml
    preflight_secrets.yml
  roles/
    freeipa_server/
      defaults/main.yml
      meta/argument_specs.yml
      meta/main.yml
      tasks/main.yml
      vars/main.yml
  files/
    freeipa_server/
      docker-compose.freeipa.yml.j2
      freeipa.env.j2
```

The compose file is a template under `ansible/files/freeipa_server/` rather than a
standalone file, so the container definition and the inventory that drives it cannot
drift apart. The passwords reach it
through a `0600` env file written beside it on `core01`.

---

## Review

- Confirm the Phase 1 exit criteria still hold: tunnel up, APT proxy working, Apache
  serving artifacts, internal nodes still with no internet route.
- Confirm the FreeIPA artifacts are staged and their checksums verify.
- Reconcile the `core01` spec across `TARGETS.md` and
  `infra/pulumi/modules/vm_definitions.py`: `192.168.2.4/24`, gateway `192.168.2.99`
  (`repo01`'s internal address), 2 vCPU, 6 GiB RAM, 32 GB OS disk, 100 GB `/data1`.
- Confirm `core01` is defined with DNS `127.0.0.1`. It is its own resolver once
  FreeIPA is running; before that it resolves nothing, which is why the role writes an
  `/etc/hosts` entry for its own name and installs with `--no-host-dns`.
- Confirm the FreeIPA passwords are exported; `core01.yml` preflights them and stops
  before touching the host if they are missing.

---

## Research

Resolved against the upstream `freeipa-container` documentation
(`https://github.com/freeipa/freeipa-container`) and the image's own entrypoint
(`/usr/local/sbin/init`, `/usr/sbin/ipa-server-configure-first`). Every item below cost
a failed install before it was looked up; the deployment is now the shape upstream
documents rather than an inferred one.

| Question | Decision | Why |
| --- | --- | --- |
| Privileged? | **No.** `privileged` removed | Upstream: "privileged setup is not supported and will not work". It is also a known cause of the install failing at its client stage with `No valid Negotiate header in server response` |
| Filesystem | `read_only: true` plus tmpfs `/run`, `/tmp` | The documented shape; the container writes only to `/data` and those tmpfs mounts |
| cgroup v2 on Ubuntu 24.04 | `cgroup: host` **and** `/sys/fs/cgroup:rw` bind mount | Upstream's documented cgroup v2 flags. Rootful Docker mounts `/sys/fs/cgroup` read-only for any unprivileged container, and systemd cannot start without write access. Verified on the host: an unprivileged private cgroup namespace yields `ro`, and `CAP_SYS_ADMIN` does not change it |
| RAM detection | `--skip-mem-check` | The host cgroup namespace shows the container the root cgroup, which has no `memory.max` or `memory.current`. `check_available_memory` reads exactly those and aborts with "Unable to determine the amount of available RAM". The call is guarded by `if not options.skip_mem_check` (`install.py:352`), and this VM's 4 GiB is well over the 1.2 GB floor |
| Install options | Container **command args** | The entrypoint writes each arg to `/run/ipa/ipa-server-install-options` and feeds it to the installer through `xargs`. `IPA_SERVER_INSTALL_OPTS` also exists, but only applies on a first install; args are unconditional. One option per list entry — the entrypoint quotes each with `printf %q` |
| Hostname | **No `--hostname`** | It sets `_host_name_overridden`, which makes the installer run `hostnamectl set-hostname`. That needs `CAP_SYS_ADMIN` and kills every unprivileged install at exactly that line. The container inherits the host name through host networking |
| Data directory mode | `0755`, not `0750` | The container symlinks `/etc/krb5.conf` and `/etc/ipa` into `/data` and runs services as non-root (`ipaapi`). Without the traverse bit, `kinit` fails with "Permission denied while initializing Kerberos 5 library" and every API login returns HTTP 500, while `ipactl` still reports RUNNING |
| Container stop | `stop_grace_period: 5m` | Docker's 10s default kills 389-ds mid-write. The database then returns `BDB0060 PANIC: fatal region error detected` and refuses to start, and `ipactl` still reports the service RUNNING. Every recreate of the container is a database shutdown |
| Host resolver | systemd-resolved stopped and masked; static `/etc/resolv.conf` at `127.0.0.1` | resolved holds `127.0.0.53:53` and `127.0.0.54:53`, so `named` cannot own port 53, and its stub answers SERVFAIL for `dev.lo`. Matches the resolver column for `core01` in `TARGETS.md` |
| Image tag match | Verified in the role | The role asserts the expected tag is present after loading, so a mismatched tarball fails there instead of turning into a registry pull an air-gapped host cannot make |
| GitLab record set | `gitlab.dev.lo` and `registry.gitlab.dev.lo`, both `192.168.2.99` | Minimal and deterministic; both are repo01's internal address. Published from inventory, not from role logic |

Still open:

- **NTP source.** The role installs with `--no-ntp` because there is no upstream pool on
  this network. Decide whether `core01` should serve its own clock to internal clients
  and set `freeipa_server_ntp` accordingly. Unblocked but unanswered.
- **Backup and restore** of the FreeIPA data on `/data1`, which the Test section
  requires exercising once.
- **CA trust distribution.** Nothing outside the domain trusts the new CA, so API calls
  currently pass `validate_certs: false`. Phase 3 and the controller-side validation
  path both want the CA in a trust store.

Already decided:

- **No DNS forwarders.** `freeipa_server_forwarders_enabled` is false, so the installer
  runs with `--no-forwarders`. Nothing on the internal network can reach an upstream
  resolver, and forwarding would make every non-`dev.lo` lookup hang until it times
  out.
- **Secrets come from the environment**, never the repo. See `SECRETS.md`.

---

## Implement

**1. Enable Phase 2 in the Pulumi deployment configuration**

- Set `deployment:phaseLimit: 2` in the stack config.
- Keep networking and disk values aligned with the existing `core01` VM definition.

**2. Create the `core01` VM**

- Use the current `core01` spec.
- Verify the host receives `192.168.2.4/24` with gateway `192.168.2.99`.

**3. Apply the base host role**

- Set the hostname to `core.dev.lo`.
- Point APT at the `repo01` proxy.
- Install baseline packages, time synchronization, and baseline hardening.

**3a. Keep the container daemons off the OS disk**

- Apply `container_storage` between `data_volume` and the container runtime, so
  Docker and containerd are configured to write to `/data1` before either is
  installed. See Phase 1, which owns the role and records why it exists.
- On a rebuilt `core01` this moves nothing: it writes the configuration and the
  daemons start on the data volume.

Applied retrospectively on 2026-08-15. `core01` carried the same fault as
`repo01` at a tenth the scale — 1.2 GB of `/var/lib/containerd` and a few
hundred megabytes of `/var/lib/docker` on the OS disk — and the root filesystem
went from 13% to 9%. It was never close to full, which is exactly why it would
have gone unnoticed until it was.

FreeIPA was stopped gracefully through its compose project, and verified after
the move with a transaction rather than a status: `dig` against `192.168.2.4`
from an internal node resolved both `gitlab.dev.lo` and `kubecp01.dev.lo`.

**4. Install the container runtime**

- Install `docker.io` and `docker-compose-v2` through the `repo01` apt proxy.

**5. Load the FreeIPA image**

- Download the image tarball from Apache, verify its checksum, and load it locally.
- Do not pull from any public registry — `core01` has no path to one.

**6. Bootstrap the domain and services**

- Write the `/etc/hosts` entry and set the host name first: the installer resolves its
  own name before FreeIPA is answering DNS, and there is no other resolver to ask.
- Render the compose file and env file, then bring the container up.
- Wait for the bootstrap to finish. `docker compose up` returns long before
  `ipactl status` reports the services running, so the role polls until it does.
- Persist data under `/data1/freeipa`.

**7. Publish GitLab service DNS names**

- Add `gitlab.dev.lo` and `registry.gitlab.dev.lo`.
- Keep the record set minimal and deterministic.

**8. Point resolvers at `core01`**

- Configure internal hosts and the controller-side validation path to use
  `core.dev.lo` as the primary DNS server.

---

## Test

**Infrastructure**

- `core01` is created and reachable at `192.168.2.4/24`.
- The host reboots cleanly and remains reachable via Ansible.

**Artifact path**

- Every package and image on `core01` came from `repo01`; confirm no upstream fetch
  was attempted in the install logs.

**Identity and DNS**

- FreeIPA installs and configures with no manual intervention.
- `dev.lo` services are authoritative for local DNS.
- `getent hosts core.dev.lo` resolves through the new domain authority.
- `gitlab.dev.lo` and `registry.gitlab.dev.lo` resolve from both the controller and
  internal hosts.

**Operational**

- A second Ansible run reports no drift for the steady state.
- DNS and CA services return automatically after a restart.
- A FreeIPA backup and restore is exercised once.

**Verified on 2026-08-14**

| Check | Evidence |
| --- | --- |
| Services running | All nine report RUNNING on a clean install |
| Directory actually serving | `kinit admin` succeeds; API login returns HTTP 200 |
| Zone authoritative | `SOA dev.lo` → `core.dev.lo. hostmaster.dev.lo.` |
| `core.dev.lo` | `192.168.2.4` from controller, repo01, and core01 |
| `gitlab.dev.lo` | `192.168.2.99` |
| `registry.gitlab.dev.lo` | `192.168.2.99` |
| Idempotency | Second run: `ok=49 changed=0 failed=0` |
| Data on the right disk | `/data1` mounted on `/dev/sdb`, not the OS disk |

Not yet verified: reboot survival, and the backup/restore exercise.

---

## Risk Gates

| Gate | Condition | Action |
| --- | --- | --- |
| 1. VM preparation | `core01` does not join the internal network correctly | Stop and fix networking before the FreeIPA install |
| 2. Artifact availability | A required FreeIPA artifact is missing from Apache | Stage it on `repo01` first; never open an internet path from `core01` |
| 3. FreeIPA bootstrap | The domain bootstrap fails or leaves a partial CA state | Remove the partial configuration and retry from a clean host state |
| 4. DNS dependency | `dev.lo` records do not resolve from the controller and internal hosts | Do not start Phase 3 |

## Deliverables at Phase 2 Completion

- `core01` created and reachable on the internal network.
- Working FreeIPA identity, DNS, and certificate authority on `core.dev.lo`.
- Required GitLab DNS records published under `dev.lo`.
- Secrets sourced from the environment, with nothing sensitive in the repo.
- Validation notes and an operational runbook for rebuild or recovery.

---

## Runbook

### Readiness is not liveness

`ipactl status` reports what systemd last knew about each unit. A directory server
that died after startup still reads `Directory Service: RUNNING` while every
authentication against it fails. Treat these as the real signals instead, in this
order — they are what `roles/freeipa_server/tasks/readiness.yml` checks, cheapest
first, so a failure names the lowest broken layer:

```bash
ss -lntup | grep -E ':(389|636|88|443) '          # directory actually listening
dig +short SOA dev.lo @192.168.2.4                # zone answering authoritatively
docker exec freeipa-server kinit admin            # KDC + directory behind it
curl -sk -o /dev/null -w '%{http_code}\n' \
  -X POST https://core.dev.lo/ipa/session/login_password \
  -H 'Referer: https://core.dev.lo/ipa' \
  --data-urlencode user=admin --data-urlencode "password=$FREEIPA_ADMIN_PASSWORD"
```

The playbook performs all four and fails if any does not pass, so a phase that runs
after this one never starts against a half-ready domain.

### Web UI access

`https://core.dev.lo/ipa/ui/`, user `admin`, password `FREEIPA_ADMIN_PASSWORD` from
`~/.config/rke2lab/env.sh`. The certificate is issued by the `dev.lo` CA, which
nothing trusts yet, so a browser warning is expected until CA trust is distributed.

### Diagnosing a failed install

The container's own logs are usually empty on failure; the useful evidence is on the
data volume, which survives the container:

```bash
tail -40 /data1/freeipa/var/log/ipaserver-install.log     # the install itself
tail -40 /data1/freeipa/var/log/dirsrv/slapd-DEV-LO/errors # database health
docker exec freeipa-server tail -40 /var/log/httpd/error_log
```

### Rebuild from clean

The domain holds no state worth recovering until it has users, enrolled hosts, or
issued certificates. Before that point a rebuild is faster and safer than repair —
particularly for a panicked database, where recovery leaves an identity store of
uncertain integrity:

```bash
ansible core01 -i inventory/hosts.yml -b -m shell \
  -a 'docker rm -f freeipa-server; rm -rf /data1/freeipa'
ansible-playbook -i inventory/hosts.yml playbooks/core01.yml
```

A partial install must be cleared this way, not retried over: the entrypoint keys off
`/data/build-id` and `/etc/ipa/ca.crt` and will take a start-or-upgrade path instead
of installing.

### Recovering a corrupt directory database

`BDB0060 PANIC: fatal region error detected` in the dirsrv errors log means 389-ds was
killed mid-write — historically by a container recreate exceeding Docker's 10 second
stop timeout, which `stop_grace_period` now prevents. If it happens once the domain
holds real data, restore from backup rather than rebuilding; if it does not, rebuild
from clean as above.

### Stale negative DNS caching

A name queried before it was published stays NXDOMAIN in a caching resolver for the
zone's negative TTL (3600s here). After publishing records, clear the cache on any
host that queried early — `systemctl restart dnsmasq` on the controller — or verify
against the authority directly with `dig @192.168.2.4`.
