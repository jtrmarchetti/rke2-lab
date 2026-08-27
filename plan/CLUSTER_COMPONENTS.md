# Cluster Components

This file records **which component was chosen and why**. How each one is
operated — health checks, failure modes, URLs, rotation — is the
documentation site's, in `docs/source/components/` and the sysadmin section.
Adding, removing or replacing anything in this file obliges a matching change
there, and a matching entry in the developer section's
`docs/source/developer/infrastructure-design` when the decision itself is the
new fact; a component chosen and never documented for whoever has to run it is
half a decision.

## Node Managed

### Core
- CNI: Cilium — installed in Phase 4 as RKE2's packaged chart (`cni: cilium`),
  not in Phase 5. A node without a CNI never reaches `Ready`, so the control
  plane could not have been declared healthy without it.
- DNS: **CoreDNS, twice.** RKE2's own for `*.svc.cluster.local`, and a second
  deployment in `cluster-dns` that is authoritative for `k8s.dev.lo` on a
  LoadBalancer address FreeIPA forwards to. ExternalDNS was never adopted and
  is not needed: the second CoreDNS answers every single-label name under the
  subdomain with the ingress address, so a GitOps-managed name needs no record
  created anywhere. Corrected 2026-08-17; this line read "CoreDNS + (maybe
  ExternalDNS)" for the whole build.
- GitOps Engine: Flux CD
- Ingress: **Traefik v3** — `v3.7.8`, RKE2's packaged chart, selected in Phase 5
  with `ingress-controller: traefik`.

  This entry has now been wrong twice in opposite directions, which is worth
  keeping rather than tidying away. It originally read "Traefik v3 (RKE2
  default)". Phase 4 corrected that: RKE2's packaged default was
  `ingress-nginx` and Traefik was the K3s default. Phase 5 then found that
  ingress-nginx **reached end of life in March 2026** and that RKE2 makes
  Traefik the default for new clusters at v1.36.

  So the original answer was right and its stated reason was wrong, and the
  correction was right about the reason and wrong about what to do. The lesson
  is the one `OVERVIEW.md` already records about reading vendor documentation
  rather than reasoning from what a default used to be.

## GitOps Managed

### Core
- Certificate Management: cert-manager
- CSI: **Longhorn** — chosen in Phase 5, because the disks had to be prepared
  for one or the other and Ceph and Longhorn want opposite things from a disk.
  Lighter on the workers — 4 vCPU / 8 GiB when the choice was made, 10 GiB
  since the uniform uplift in `TARGETS.md`, neither of which is enough to carry
  Ceph's per-node OSD, MON and MGR alongside Phase 6's observability stack —
  and lighter on storage already at etcd's fsync floor. Rook's usual advantage
  of bringing object storage with it is redundant here because Garage is
  already selected for that below.

  Phase 5 formats and mounts each worker's third disk at `/var/lib/longhorn`.
  Longhorn writes into three fixed 107.4 GB virtual disks, which caps its total
  contribution to the thin pool at 322 GB no matter how many volumes exist — so
  the pool cannot be filled by Longhorn, which is less alarming than Phases 4
  and 5 assumed.

  **Two replicas, not the default three.** On a three-node cluster, three
  replicas puts a copy on every node and leaves Longhorn nowhere to rebuild when
  one fails, so a node loss means degraded until it returns. Two replicas
  tolerates the same single node loss, keeps a spare node to rebuild onto
  automatically, gives **147 GB usable instead of 98 GB**, and costs one fewer
  fsync per write on storage at etcd's floor. Three buys nothing here.

  A one-replica class exists alongside it for data that is genuinely
  reconstructible — caches, scratch — and is not used for anything whose loss
  matters. See `PHASE6_IMPLEMENTATION.md`.
- Load Balancing: **kube-vip**, not Cilium LB-IPAM. Corrected 2026-08-17 —
  the plan named Cilium and 6b chose otherwise, for two reasons that still
  hold: Cilium's L2 announcements require kube-proxy replacement, which this
  cluster does not run and which cannot be enabled on a live cluster without
  taking service networking down, and they hold a lease per service with a
  two-second renew deadline — a steady stream of etcd writes on storage
  measured at 32-50 fsync/s. kube-vip was already here holding the API VIP;
  the LoadBalancer half is a second DaemonSet of the same image plus a cloud
  provider handing out `192.168.2.40-52`. See `PHASE6_IMPLEMENTATION.md`.
- Object Storage: Garage
- Secrets Management: **OpenBao + External Secrets Operator**, with **Sealed
  Secrets** retained for bootstrap material only. Settled in Phase 6.

  The question was whether something combined the two — rotation and
  external-service secrets without the unseal ritual. Half of that is not
  available: every auto-unseal path is either a cloud KMS this network cannot
  reach or a Transit seal backed by a second instance that must itself be
  unsealed by hand. No product removes the ritual in an air-gapped lab.

  The other half is the standard pattern, and it is a division of labour rather
  than a merge. Sealed Secrets holds what must exist *before* a secret store
  does — the credential ESO uses to reach OpenBao — which is the one problem an
  external store cannot solve for itself. OpenBao holds runtime secrets with
  rotation, leases, and an audit trail, and serves services outside the cluster
  too. ESO syncs one into the other.

  **Sealed Secrets also holds OpenBao's unseal keys**, so an in-cluster unsealer
  loop can unseal after any restart and the cluster recovers from nothing but
  its Git repository and its sealing key — no operator, no controller. Flux
  already reconciles everything else from Git; this is what stops the vault
  being the one exception.

  The cost is that it collapses two threat models into one: whoever can read the
  sealing key can read every secret OpenBao holds. The sealing key therefore
  becomes the most valuable secret in the lab, so `env.sh` keeps an offline
  break-glass copy of the unseal keys — two copies that fail in different ways,
  deliberately. Note that the SealedSecret protects the copy in Git, not the
  copy in etcd.

  **OpenBao runs in the cluster**, and running it outside was considered and
  rejected. The trust-domain separation is largely illusory here: every VM is
  root-only, the controller reaches all of them as root and already holds every
  secret, and Proxmox can read every guest disk, so an attacker able to take the
  cluster steps over a VM boundary. In-cluster keeps GitOps management, Flux
  reconciliation, and self-contained recovery, and lets ESO authenticate with
  the Kubernetes auth method — pod ServiceAccount JWTs, no static credential
  stored anywhere.

  Unattended unseal in a fully offline environment always terminates in one of
  three places: a hardware root of trust, a human at every boot, or a static key
  stored somewhere. Cloud KMS is the first, rented. With no HSM this design
  takes the third knowingly, and keeps the first reachable: **SoftHSM via
  PKCS#11 is the documented next step and a hardware HSM an upgrade from it**,
  with seven implementation requirements in `PHASE6_IMPLEMENTATION.md` that keep
  both migrations to a config change rather than a redesign. PKCS#11 in open
  source is now the strongest reason to prefer OpenBao over Vault, which gates
  HSM seals behind Enterprise.

  OpenBao rather than Vault because Vault is now BUSL and OpenBao is the Linux
  Foundation's MPL-2.0 fork, API-compatible and supported by ESO as a
  first-class provider. Same reasoning that put GitLab CE here rather than EE.

  Note the storage cost: OpenBao's Raft backend fsyncs per commit — etcd's write
  pattern, on storage already at etcd's floor — so it runs as a **single node**
  rather than a three-node HA cluster. It does **not** run on the one-replica
  StorageClass: this sentence said it did until 2026-08-17, and it was the
  tempting wrong answer rather than the decision. `longhorn-single` is the
  fastest storage here and the vault is the thing most bothered by slow
  storage, but one replica means a single node loss destroys every runtime
  secret in the cluster. OpenBao is on the two-replica `longhorn` class, which
  is what the manifests, the storage class's own comment and
  `PHASE6_IMPLEMENTATION.md`'s risk gate all say.
- Service Mesh: Cilium (sidecarless)
- SSO: **Keycloak**, federated to FreeIPA and now the front door to every
  service that has one. Applied and verified on 2026-08-17.

  The model is two groups per application in FreeIPA — `<app>-admins` and
  `<app>-users` — mapped to two Keycloak client roles, `admin` and `user`.
  Granting someone access to Grafana is `ipa group-add-member grafana-users
  --users alice` and nothing else: no Keycloak console, no per-service user
  list. The directory stays the authority for who someone *is* and who is in
  what group; Keycloak holds only the sentence FreeIPA cannot express, which is
  that members of `grafana-admins` are administrators of Grafana.

  Two tiers rather than a per-service role model, because the model has to be
  statable. Each service then decides what the two mean in its own terms:
  Grafana turns them into Admin and Viewer, OpenBao into two policies,
  oauth2-proxy into permission to reach Longhorn at all.

  **Applications live in the `dev-lo` realm, not `master`.** Master administers
  every other realm and holds Keycloak's own local administrator; federated end
  users and application clients do not belong in it. That local administrator
  staying local is deliberate — a Keycloak whose administrators were themselves
  federated would be unadministrable exactly when FreeIPA is what is broken.

  Four services federate, and they divide into three cases:

  - **Grafana, OpenBao and GitLab** speak OIDC natively and were configured.
  - **Longhorn** has no authentication of any kind — not a weak default, none —
    so an **oauth2-proxy** stands in front of it in full reverse-proxy mode.
    It is the only component single sign-on adds to the cluster.
  - **Garage** cannot federate and is not listed above. It speaks S3 and an
    admin bearer token; there is no OIDC in it to configure, and its keys stay
    in the vault.

  Every client secret is **authored rather than generated**, which is what
  keeps this from adding an ordering constraint: no workload waits for Keycloak
  to exist. Every service that could keep a local break-glass account kept one.
  Both are set out in `SECRETS.md`.

  One limitation, recorded because it is invisible from the directory's side:
  **GitLab CE cannot map an OIDC claim to administrator** — that is an
  Enterprise feature. `gitlab-admins` is real in FreeIPA and real in Keycloak,
  and GitLab is the one consumer that ignores it; administrator rights there
  are still granted in GitLab, by root.

### Observability
- Metrics: Prometheus + kube-prometheus-stack
- Logging: Loki + Alloy (Grafana Labs)
- Tracing & Visualization: Grafana + Tempo + OpenTelemetry Collector
