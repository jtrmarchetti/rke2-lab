# Phase 3 Implementation Plan — GitLab Container Platform

## Scope

Deploy GitLab as a containerized workload on `repo01` and turn it into Tier 2 of the
artifact model: the container registry and package registry that Phases 4–6 pull from.

`repo01` already holds every artifact the cluster needs. Phase 3 does not download
anything new for the cluster; it stands up the service that redistributes what Tier 1
staged, over `dev.lo` names that Phase 2 made resolvable.

## Decision

| | |
| --- | --- |
| Primary path | GitLab CE Omnibus in a container on `repo01`, image loaded from the Apache artifact host, TLS from the FreeIPA CA, data on `/data1` |
| Fallback path | If TLS issuance cannot be made to work, fall back to plain HTTP with an insecure-registry entry on every consuming node, and keep the CA trust work as a Phase 4 prerequisite |

## Preconditions

- Phase 2 exit criteria hold: FreeIPA serves `dev.lo` authoritatively and its CA issues.
- `gitlab.dev.lo` and `registry.gitlab.dev.lo` resolve to `192.168.2.99`.
- The GitLab image tarball is staged on Apache with a published checksum.
- `GITLAB_ROOT_PASSWORD` is set in `~/.config/rke2lab/env.sh`.

---

## Review

Performed 2026-08-14 against the live environment.

| Check | Result |
| --- | --- |
| Tunnel to internal network | `repo01` and `core01` both reachable |
| `gitlab.dev.lo` | `192.168.2.99` from the controller |
| `registry.gitlab.dev.lo` | `192.168.2.99` from the controller |
| GitLab image staged | `gitlab-ce-19.2.2-ce.0.tar`, 1.4 GB, with `.sha256` and `.digest` |
| `/data1` headroom | 92 GB free of 98 GB |
| `repo01` sizing | **2 vCPU / 3.9 GiB, no swap — insufficient, see below** |
| Compose plugin on `repo01` | **`docker-compose-v2` not installed; only `docker.io`** |

Two items had drifted from what the phase assumed, and both are fixed in this phase.

### `repo01` was undersized

GitLab's requirements page puts a single-node install at 8 vCPU / 16 GB baseline and
8 GB as the constrained floor. `repo01` had 4 GiB and no swap, and it also carries
Apache, apt-cacher-ng, dnsmasq, chrony, and the WireGuard tunnel.

`repo01` is now **4 vCPU / 8 GiB**, changed in `TARGETS.md` and
`infra/pulumi/modules/vm_definitions.py` and applied through Pulumi. Proxmox applied
both in place — no reboot, and the tunnel never dropped. The memory-constrained
omnibus settings are applied on top rather than instead: 8 GiB is the floor for this
host, not headroom.

**This resize does not belong to Phase 3, and no longer happens here.** Phase 1
creates `repo01`, so its size is a Phase 1 spec; Phase 3 only *discovered* that
the spec was wrong. The correction lives in `TARGETS.md` and the Pulumi
definition, which Phase 1 applies, so a rebuild brings the host up already
correctly sized and Phase 3 changes no hardware at all. The record above is
kept because the discovery is what justifies the number. `repo01` now carries
10 GiB after a later uniform uplift across the estate — see `TARGETS.md`.

### Port 80 belongs to Apache

Apache serves the Tier 1 artifact root on `*:80` and every Phase 1–2 consumer has that
URL baked in, so GitLab cannot have port 80. GitLab publishes `443` only, and both
`gitlab.dev.lo` and `registry.gitlab.dev.lo` are served from it by separate nginx
server blocks selected on SNI. Host SSH owns port 22, so GitLab's SSH is published on
`2222` and `gitlab_shell_ssh_port` is set to match, which is what makes the clone URLs
GitLab prints correct.

---

## Research

| Question | Decision | Why |
| --- | --- | --- |
| GitLab version | `gitlab/gitlab-ce:19.2.2-ce.0`, already staged | Pinned to a release tag, not `latest`, so a rebuild stages the same GitLab the lab was built against |
| Deployment shape | Omnibus container, compose-managed | Same pattern as FreeIPA: the compose file is a role template so the container definition and the inventory driving it cannot drift |
| TLS | **FreeIPA CA**, one certificate per name | Closes the CA-trust item Phase 2 left open, which Phases 4–6 need anyway. Plain HTTP would push an insecure-registry exception onto every cluster node |
| Certificate issuance | CSR generated on `repo01`, signed on `core01` via `ipa cert-request --add` | Verified end to end before the role was written: `ipa host-add` then `ipa cert-request --principal HTTP/<name> --add --chain` yields a 2-year certificate with the correct SAN |
| `repo01` domain enrollment | **No.** Not enrolled | `ipa-client-install` has no option to leave `/etc/resolv.conf` alone — it would replace the split-DNS resolver `repo01` needs for upstream downloads — and it puts SSSD in the auth path of the only gateway into the internal network. Signing a CSR remotely needs neither |
| Certificate renewal | Re-run the play | Without enrollment there is no certmonger tracking. The certificate is valid two years, well beyond the life of this lab, and the role reissues when it is inside the renewal window |
| Ports | `443` and `2222` published; no `80` | Apache owns `80` and host SSH owns `22`. Both GitLab vhosts share `443` by SNI |
| Registry URL | `https://registry.gitlab.dev.lo` on 443 | A separate hostname rather than `gitlab.dev.lo:5050`, because the DNS record already exists and it avoids publishing another port |
| Memory tuning | Prometheus stack, exporters, and KAS off; Puma 2 workers; Sidekiq concurrency 10 | The documented memory-constrained settings, worth ~300 MB from the monitoring group alone. None of it is used in this lab |
| Container stop | `stop_grace_period: 5m` | The same lesson FreeIPA taught. Omnibus runs PostgreSQL; Docker's 10s default `SIGKILL`s it mid-write on every recreate |
| Data layout | `/data1/gitlab/{config,data,logs,backups}` | `/data1` is the 100 GB disk. The registry lives under `data/`, so registry growth cannot fill the 32 GB OS disk |
| Registry sizing | No separate volume | The full RKE2 image set is single-digit gigabytes against 92 GB free. Splitting it out would add a failure mode without removing one |
| Backup | `gitlab-backup create` to `/data1/backups` plus a copy of `/etc/gitlab` | Application data and the secrets that decrypt it are two separate backups; a restore needs both, and omnibus deliberately excludes `gitlab-secrets.json` from the tarball |
| Readiness gating | Transaction, not status | `docker ps` and `gitlab-ctl status` both report intent. The role gates on `/-/readiness`, a registry v2 API response, and an authenticated API call |

Still open:

- **Root password rotation.** `GITLAB_ROOT_PASSWORD` seeds the initial root account on
  first reconfigure only. Changing it later is a GitLab-side operation, not an
  inventory change.
- **Runner deployment.** No CI runner is registered. Phases 4–6 consume artifacts from
  GitLab but do not build in it, so this stays out of scope until something needs it.

---

## Implement

**1. Confirm `repo01`'s size** — no longer a resize. The spec belongs to Phase 1,
which creates the VM; `TARGETS.md` and the Pulumi VM definition carry it and
Phase 1 applies it. Check the host came up with what `TARGETS.md` says before
installing GitLab, and stop if it did not. The one-off in-place resize this step
used to perform is recorded in the Review above as the discovery that set the
number.

**2. Issue certificates** — the `ipa_service_cert` role, per name:

- Generate a key and CSR on `repo01`.
- Delegate to `core01`, create the host entry, and sign with `ipa cert-request`.
- Install the certificate, key, and CA chain on `repo01`.
- Skip entirely when a valid certificate outside the renewal window already exists.

**3. Deploy GitLab** — the `gitlab` role, rewritten from the Phase 1 guard role:

- Install `docker.io` and `docker-compose-v2` through the apt proxy.
- Create the data tree on `/data1`.
- Load the image from the Apache tarball and assert the tag matches.
- Render the compose file and the environment file holding the root password.
- Bring the container up and wait for the first reconfigure, which is slow.
- Gate on readiness before reporting success.

**4. Publish the CA** — write the FreeIPA CA certificate into the Apache artifact root
so consuming nodes in later phases can fetch it over Tier 1 before they trust Tier 2.

**5. Push the RKE2 content** — deferred. The RKE2 artifact set is not yet staged on
`repo01`; its versions are open research items in the Phase 4 and 5 plans, and
`group_vars/repo/artifacts.yml` records why guessing them would produce a manifest that
verifies against nothing. Phase 3 delivers the registries and the documented push path;
Phase 4 adds its manifest entries and pushes them.

---

## Test

**Service**

- GitLab answers `https://gitlab.dev.lo` with a certificate that validates against the
  FreeIPA CA — no `-k`.
- The registry answers `https://registry.gitlab.dev.lo/v2/` the same way.

**Function**

- Git clone and push over HTTPS.
- Registry push and pull.
- Package registry upload and download.
- An internal node pulls from the registry with no internet access.

**Operational**

- A second Ansible run reports no drift.
- The container survives a host reboot and comes back serving.
- A backup and restore is exercised once against real data.

**Verified on 2026-08-14**

| Check | Evidence |
| --- | --- |
| TLS on `gitlab.dev.lo` | `Verify return code: 0 (ok)`, issuer `CN=Certificate Authority,O=DEV.LO` |
| TLS on `registry.gitlab.dev.lo` | `Verify return code: 0 (ok)`, own certificate by SNI |
| Application serving | `/users/sign_in` 200; `/api/v4/version` 401 unauthenticated |
| Readiness | `/-/readiness` 200 from an allowlisted source |
| Git push and clone | HTTPS push then clone; content round-tripped |
| Registry push and pull | `Pushed`, digest returned, re-pulled after local delete |
| Package registry | Generic upload `201 Created`, download returned the content |
| **Internal node pull, no internet** | On `core01`: upstream registry unreachable, CA fetched over Tier 1 HTTP, `docker pull` succeeded |
| Idempotency | Second run: `ok=38 changed=0 failed=0` |
| Backup | Data tarball plus `gitlab-ctl backup-etc` config archive on `/data1` |
| Restore | Restored in place; readiness 200, commit message intact, image still pullable |
| Data on the right disk | `/data1` at 2.2 GB of 98 GB used |
| Reboot survival | `repo01` rebooted; GitLab returned to readiness unattended, `/data1` remounted, tunnel and NAT restored |

The tunnel takes longer to come back than the host does. Immediately after a
`repo01` reboot the WireGuard handshake succeeds while `192.168.2.0/24` is still
unreachable from the controller, which looks like broken forwarding and is not —
`iptables -t nat -S` and `net.ipv4.ip_forward` were both correct throughout.
Re-test before concluding anything.

---

## Risk Gates

| Gate | Condition | Action |
| --- | --- | --- |
| 1. Host capacity | `repo01` did not come up at the `TARGETS.md` size, or the Proxmox host has no headroom | Stop and fix it in Phase 1, where the VM is created. Do not resize from here; GitLab on 4 GiB will thrash and the failure will look like a GitLab bug |
| 2. Certificate issuance | The FreeIPA CA will not issue for the service names | Fall back to plain HTTP and carry the insecure-registry exception into Phase 4 explicitly |
| 3. First reconfigure | Omnibus reconfigure fails or loops | Read `/data1/gitlab/logs`; do not recreate the container without a grace period, which corrupts PostgreSQL |
| 4. Registry reachability | An internal node cannot pull from the registry | Do not start Phase 4; the whole air-gapped install depends on this path |

## The pull path for internal nodes

What a Phase 4–6 node must do to consume Tier 2. Verified end to end from
`core01`, which has no route to the internet.

**1. Trust the domain CA.** The bootstrap step, and the reason the CA is
published on Apache: plain HTTP over Tier 1 is the only path that does not
already require the trust it is delivering.

```bash
curl -sf http://192.168.2.99/gitlab/dev.lo-ca.crt \
  -o /usr/local/share/ca-certificates/dev.lo.crt
update-ca-certificates
```

Install it **before** the container runtime first starts. The runtime caches the
trust pool at start, so doing this afterwards needs a daemon restart — and on
`core01` a daemon restart is a DNS outage for the whole internal network.

`/etc/docker/certs.d/registry.gitlab.dev.lo/ca.crt` is not sufficient on its own:
it covers the registry host, but a push or pull authenticates against
`https://gitlab.dev.lo/jwt/auth`, which is validated against the system store.

**2. Resolve through `core01`.** `registry.gitlab.dev.lo` and `gitlab.dev.lo`
both answer `192.168.2.99` from the `dev.lo` authority.

**3. Pull.**

```bash
docker login registry.gitlab.dev.lo      # or containerd registry credentials
docker pull registry.gitlab.dev.lo/<group>/<project>:<tag>
```

For RKE2 in Phase 4 this becomes a containerd registry mirror pointed at
`registry.gitlab.dev.lo` in `registries.yaml`, with the same CA in the node
trust store. Credentials should be a GitLab deploy token rather than `root` —
`GITLAB_REGISTRY_USER` and `GITLAB_REGISTRY_TOKEN` are reserved in `env.sh` for
exactly that and are still unset, because the token has to be issued by the
running GitLab.

Binaries, charts, and tarballs come from the generic package registry:

```bash
curl --header "PRIVATE-TOKEN: <token>" \
  "https://gitlab.dev.lo/api/v4/projects/<id>/packages/generic/<name>/<ver>/<file>"
```

## Runbook

### Things that cost a failed run here

Each of these presented as a different problem than it was. They are recorded
because the next containerized service will meet the same shapes.

**A `0700` bind-mount root breaks PostgreSQL, and the role re-breaks it.**
Omnibus runs its services as non-root users inside the container — `gitlab-psql`,
`git`, `gitlab-www` — and every one of them must traverse the mount root to
reach its own subdirectory. At `0700 root:root` the log fills with
`could not access directory "/var/opt/gitlab/postgresql/data": Permission denied`
while the container reports `Up` and nginx answers 502. The worse half is that
the `file` module re-applies the mode on every run, so a role declaring `0700`
took down a GitLab that was already working. The data directories are `0755`,
which is what omnibus sets them to itself. This is the Phase 2 FreeIPA lesson in
a second service.

**A 404 from `/-/readiness` is an allowlist, not a missing endpoint.** GitLab
restricts the health endpoints to `127.0.0.0/8` and answers 404 — not 403 — to
everything else. Published ports mean the container never sees a loopback
source address; the request arrives from the compose network gateway, so even a
request made on the host itself is rejected. `gitlab_rails['monitoring_whitelist']`
carries the Docker range, the internal subnet, and the tunnel.

**`docker login` succeeding does not mean `docker push` will.** Docker's
per-registry `/etc/docker/certs.d/<host>/ca.crt` covers only the registry host,
but a registry push authenticates against `https://gitlab.dev.lo/jwt/auth` — a
different host, validated against the system trust store. The login succeeded
and the push failed with `certificate signed by unknown authority` pointing at
the token endpoint. The CA belongs in the system trust store, which is what
`ipa_service_cert_trust_ca` does and what every Phase 4 node will need. Docker
caches the trust pool at daemon start, so the daemon must be restarted after.

**`docker cp` into the FreeIPA container fails.** It runs `read_only`, which is
the shape upstream documents and a Phase 2 decision, and `docker cp` refuses
with `container rootfs is marked read-only` however writable the target path is.
`/tmp` is a tmpfs the container can write from inside, so the CSR arrives on
stdin instead.

**`systemctl restart docker` on `core01` takes down DNS for the whole internal
network.** FreeIPA runs as a container there, so restarting the daemon restarts
the domain — `named` included — and every internal host loses name resolution
until it finishes coming back, which is minutes, not seconds. It was done here
to make the daemon pick up the newly trusted CA. Restart the container, or
better, install the CA before the daemon ever starts, and treat any docker
restart on `core01` as a DNS outage.

**Do not run two playbooks against `repo01` at once.** A concurrent session
running `repo01.yml` rebooted the host in the middle of GitLab's first
reconfigure. It survived, because `restart: unless-stopped` and the grace period
did their job, but the symptom — SSH resets, 443 refused, the tunnel down — reads
like a crashed host rather than a reboot someone else asked for.
`journalctl -b -1 | grep "System is rebooting"` names the caller.

### Where to look when it does not come up

```bash
docker exec gitlab gitlab-ctl status          # a pid uptime of 0s is a crash loop
tail -30 /data1/gitlab/logs/postgresql/current
tail -30 /data1/gitlab/logs/puma/current
tail -30 /data1/gitlab/logs/nginx/gitlab_error.log
```

A 502 after a restart is usually just Puma preloading the application, which
takes minutes on this host — the registry answers 401 throughout, because it is
a separate process that does not wait for Rails. Read the pid age before
concluding anything: a **stable pid with rising uptime is booting**, and a **pid
that keeps resetting to `0s` is crash-looping**. Only the second is a problem.

A service whose pid age keeps resetting to `0s` is restarting, not running.
`gitlab-ctl status` reporting `run:` for it is not a contradiction — it is the
same "status reports intent, not health" trap the Phase 2 runbook describes.

### Web UI access

`https://gitlab.dev.lo`, user `root`, password `GITLAB_ROOT_PASSWORD` from
`~/.config/rke2lab/env.sh`. The certificate chains to the `dev.lo` CA, published
for consumers at `http://192.168.2.99/gitlab/dev.lo-ca.crt`.

## Deliverables at Phase 3 Completion

- GitLab serving Git, container registry, and package registry over `dev.lo` names.
- TLS from the FreeIPA CA, with the CA published for consumers.
- Backup procedure exercised.
- The documented pull path for internal nodes.

## Status

Delivered and verified, with one deliberate carry-over.

**Complete:** GitLab serving Git, the container registry, and the package
registry over `dev.lo` names, with certificates from the FreeIPA CA and the CA
published for consumers. Backup and restore exercised against real data, reboot
survival confirmed, and the roles idempotent.

**Carried into Phase 4:** step 5 of Implement — publishing the RKE2 artifact set
into GitLab. The artifacts are not yet staged on `repo01` and their versions are
open research items in the Phase 4 and 5 plans;
`group_vars/repo/artifacts.yml` records why recording a guessed version produces
a manifest that verifies against nothing. The registries and the pull path are
in place and proven, so Phase 4 adds its manifest entries, re-runs
`playbooks/repo01.yml` to stage them, and pushes.

This means the second Phase 3 exit criterion — "the complete RKE2 artifact set
published in GitLab and pullable from an internal node" — is met in mechanism
but not in content. The mechanism half is proven: an internal node with no
internet pulled an image from the registry over TLS.

**Also open:** a deploy token for node pulls. `GITLAB_REGISTRY_USER` and
`GITLAB_REGISTRY_TOKEN` are reserved in `env.sh` and still empty; the token has
to be issued by the running GitLab, and nodes should not pull as `root`.
