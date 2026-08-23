# Secrets Handling

No secret value lives inside this repository. Everything sensitive is stored outside
the project tree and pulled in at run time through the environment.

## Location

```text
~/.config/rke2lab/               # mode 0700
  env.sh                         # mode 0600 — every secret, as shell exports
  proxmox-lab.env.bak            # verbatim copy of the superseded file
  sealed-secrets-key.yaml        # mode 0600 — the Sealed Secrets sealing key
  flux-deploy-token.yml          # mode 0600 — Flux's read-only repository token
  k8s-ca/                        # mode 0700 — the cluster intermediate CA
```

`~/.config/proxmox-lab.env`, which previously held a subset of these values, is now a
one-line shim that sources `env.sh`. Existing shell profiles and the Pulumi README
instructions keep working, and there is only one file to edit.

The repo references these values only through `lookup('env', ...)` in Ansible, or
through the Pulumi CLI reading its own environment.

`bootstrap/env.sh.example` is the template: every variable name below, with no
values, and a note on which of them automation writes rather than a human. It
exists because the first rule left a gap — the names lived in this document, so
a controller rebuilt from a fresh clone knew that secrets were needed but not
which ones, and found out one preflight failure at a time. A template with no
values in it does not violate the rule; a missing one made the rule expensive.

```bash
install -d -m 0700 ~/.config/rke2lab
install -m 0600 bootstrap/env.sh.example ~/.config/rke2lab/env.sh
```

## Usage

Source the file once per shell before running any automation:

```bash
source ~/.config/rke2lab/env.sh
```

Every playbook runs a preflight assertion first, so a missing or empty variable fails
immediately with the name of what is missing, rather than silently skipping tasks
whose `when:` guards test for a non-empty value.

**Quote values with single quotes.** The superseded file used double quotes, so the
`$Fls3` inside the directory manager password was expanded as an undefined shell
variable and silently dropped — FreeIPA would have received a password five characters
shorter than the one written down. Single quotes prevent `$`, `` ` ``, and `\` from
being interpreted.

## What lives where

| Value | Stored in | Referenced by |
| --- | --- | --- |
| `PROXMOX_VE_*` | `env.sh` | Pulumi provider |
| `PULUMI_CONFIG_PASSPHRASE` | `env.sh` | Pulumi stack config decryption |
| `VM_USERNAME`, `VM_USER_PASSWORD`, `VM_SSH_PUBLIC_KEY` | `env.sh` | cloud-init, Ansible connection |
| `WIREGUARD_CONTROLLER_PRIVATE_KEY` | `env.sh` | `group_vars/controller/main.yml` |
| `WIREGUARD_REPO_PRIVATE_KEY` | `env.sh` | `group_vars/repo/main.yml`, and `group_vars/controller/main.yml` since 2026-08-17 — the controller reads it only to derive the gateway's public key, which is what removed the pasted copy of that key from inventory |
| `FREEIPA_ADMIN_PASSWORD`, `FREEIPA_DIR_MANAGER_PASSWORD` | `env.sh` | `freeipa_server` role, compose env file |
| `FREEIPA_ADMIN_PASSWORD` (again) | `env.sh` | `ipa_service_cert` role, to sign CSRs on `core01` |
| `GITLAB_ROOT_PASSWORD` | `env.sh` | `gitlab` role compose env file; `rke2_publish` role, to create the GitLab group, projects, and deploy token and to push images |
| `RKE2_TOKEN` | `env.sh` | `rke2_server` and `rke2_agent` roles — the shared cluster join secret, the same value on servers and workers. A node with the wrong value is rejected at registration |
| `GITLAB_REGISTRY_USER`, `GITLAB_REGISTRY_TOKEN` | `env.sh`, **optional** | `rke2_publish` role. Set both to use a deploy token issued by hand; left empty, the role mints one and records it on `repo01` |
| WireGuard **public** keys | **nowhere, since 2026-08-17** | Derived from the private keys by `roles/controller_tunnel` at run time. They were never secret, and that was never the problem: a derived value pasted into two `group_vars` files is a copy nothing keeps in agreement with the key it came from |
| `KEYCLOAK_ADMIN_PASSWORD`, `KEYCLOAK_DB_PASSWORD`, `GRAFANA_ADMIN_PASSWORD` | `env.sh`, **added 2026-08-16** | `openbao_secrets` role, which writes them into the vault. They existed before only as ciphertext in GitLab, which meant the plaintext was recoverable solely by decrypting the thing it was supposed to be the source of. Recovered with the sealing key backup and written here; see below. Sealed into Git until **2026-08-16**, when they moved to OpenBao |
| `GARAGE_ADMIN_TOKEN`, `GARAGE_METRICS_TOKEN`, `GARAGE_RPC_SECRET` | `env.sh` | `openbao_secrets` role, written into the vault at `kv/garage-cluster`. Authored, not generated — Garage's config names them, so they have to exist before Garage does. Sealed into Git until **2026-08-16** |
| `GARAGE_S3_ACCESS_KEY`, `GARAGE_S3_SECRET_KEY` | `env.sh` | Written by the `garage_init` role, not supplied by hand, and copied into the vault at `kv/garage` by `openbao_secrets` on the **next** run — see below |
| ~~`GITLAB_FLUX_TOKEN`~~ | **retired 2026-08-16** | Was a personal access token created by hand for Flux to read the cluster-state repository. Replaced by a project deploy token the `gitops_bootstrap` role creates, scoped to one project and to reads. It was the last hand-made secret in the reconcile path, and nothing in the repository would have recreated it on a rebuild |
| `OIDC_CLIENT_SECRET_GRAFANA`, `OIDC_CLIENT_SECRET_LONGHORN`, `OIDC_CLIENT_SECRET_OPENBAO`, `OIDC_CLIENT_SECRET_GITLAB` | `env.sh`, **added 2026-08-17** | One OIDC client secret per federated service. **Authored, not generated** — see "Why the client secrets are authored" below, which is the whole reason single sign-on adds no ordering constraint anywhere. The first two are written into the vault by `openbao_secrets` and reach their workloads through an ExternalSecret; the other two never enter the cluster, because `openbao_oidc` stores OpenBao's inside the vault's own auth configuration and the `gitlab` role writes GitLab's into `gitlab.rb` on repo01 |
| `OAUTH2_PROXY_COOKIE_SECRET` | `env.sh`, **added 2026-08-17** | Not an OIDC credential: the key the oauth2-proxy in front of the Longhorn UI encrypts its own session cookie with. Rotating it signs everyone out and does nothing else. **Exactly 16, 24 or 32 bytes** — oauth2-proxy refuses to start on any other length with an AES key size error that never mentions cookies, so `openbao_secrets` asserts the length where the variable still has a name |
| `OPENBAO_UNSEAL_KEYS`, `OPENBAO_ROOT_TOKEN` | `env.sh`, **Phase 6b, present since 2026-08-16** | Written by the `openbao_init` role, not supplied by hand. Five shares; the three that make up the threshold are also sealed into Git for the unsealer, and the other two exist only here. This is the **offline break-glass copy** — the automation's copy is a SealedSecret in Git, read by the in-cluster unsealer loop. Two copies on purpose: the SealedSecret is useless if the Sealed Secrets sealing key is lost, and this one is what recovers OpenBao when that happens |

## What lives in OpenBao

Every secret a *running workload* reads is in the vault, not in Git. This was
the design from the start — `PHASES.md` settled it during planning — but until
**2026-08-16** the code did not implement it: OpenBao and External Secrets were
deployed, configured and empty, nothing wrote a value into the KV engine, and
six runtime secrets were sealed into the cluster-state repository instead. The
one entry that did reach a workload through the vault, Garage's S3 key, had
been written by hand during the 6c run and existed in no playbook.

The `openbao_secrets` role is what fills the vault. It runs on the controller in
`cluster_init.yml`, immediately after `openbao_config` has mounted the engine
and written the policy, and it reads every entry before writing it — KV v2 keeps
versions, so a role that wrote unconditionally would push real history out of
the retained versions on every play run.

| Vault path | Holds | Read by |
| --- | --- | --- |
| `kv/keycloak` | `admin-password`, `db-password` | `keycloak-secrets` in `keycloak` |
| `kv/garage-cluster` | `admin-token`, `metrics-token`, `rpc-secret` | `garage-secrets` in `garage` |
| `kv/grafana` | `username`, `password` | `grafana-admin` in `observability` |
| `kv/garage` | `access_key`, `secret_key` | `garage-s3` in `observability`, for Loki and Tempo |
| `kv/oidc-grafana` | `client-secret` | `grafana-oidc` in `observability` |
| `kv/oidc-longhorn` | `client-secret`, `cookie-secret` | `longhorn-auth` in `longhorn-system` |

Garage takes two paths rather than one because the halves have different
origins: `garage-cluster` is authored by hand and is what Garage needs in order
to start, and `garage` is minted by Garage once it has started. One path with
two writers would mean whichever wrote last dropped the other's keys.

**`kv/garage` is written a run late, and that is structural.** `lookup('env')`
reads the environment the `ansible-playbook` process started with, so the
credentials `garage_init` appends to `env.sh` are on disk but not visible to
the run that put them there. Re-source `env.sh` and run `cluster_init.yml`
again. `OPENBAO_UNSEAL_KEYS` has exactly the same shape in `gitops.yml` — and
note that `site.yml`'s second `gitops.yml` pass does **not** resolve it for that
reason, because the constraint is the process environment rather than the order
of plays.

Only two of the four OIDC client secrets are here, and the split is the rule
this table is built on rather than an omission. The vault holds what a *running
workload* reads. Grafana and the Longhorn proxy read theirs through External
Secrets, so they belong here. OpenBao's lives inside OpenBao's own auth method
configuration and GitLab's in `gitlab.rb` on repo01 — both are written directly
by the role that configures them, and putting a copy in the vault would create
a second place each could drift from.

## Why the client secrets are authored

Every OIDC client secret in this lab is written by hand into `env.sh` and then
*set* on the Keycloak client, rather than generated by Keycloak and read back.
That inverts what most guides do, and it is what keeps single sign-on from
adding a single ordering constraint to a cold rebuild.

Generated would work like this: Keycloak mints the secret, Ansible reads it,
writes it into the vault, and External Secrets syncs it to the workload. Which
means the vault entry cannot exist until Keycloak is running — and Keycloak is
deployed by Flux, from a repository GitLab serves, after OpenBao. So on a cold
rebuild Grafana and the Longhorn proxy come up first and sit waiting on a
Secret that cannot exist yet. Every federated service would need a second pass,
the way `kv/garage` does, and unlike Garage's key nothing would force it.

Authored removes the cycle rather than sequencing around it. The value exists
before either end does. `openbao_secrets` writes it on a cold rebuild's first
pass, so each workload starts holding a credential for a client that does not
exist yet; `keycloak_clients` sets that same value on the client whenever
Keycloak is reachable; and the two begin working together with no restart and
no second run. It is the argument `GARAGE_RPC_SECRET` already carries above — a
credential named by two configurations has to exist before either of them.

The cost is honest and small: four more values to author, and no rotation for
free. Rotating one means changing `env.sh` and re-running `cluster_init.yml`,
which rewrites both ends.

## Break-glass, per service

Single sign-on introduces one new way for the lab to become unreachable —
Keycloak not starting — and the arrangement below is what stops that from being
a single point of failure. Every one of these is deliberate, and none of them
is a leftover. **All four were re-tested on 2026-08-17**, after single sign-on
was enabled, because a break-glass path that has never been exercised since the
change that could have broken it is a claim rather than a fact.

| Service | If Keycloak is down | Where the credential lives |
| --- | --- | --- |
| **OpenBao** | Root token still works, and still carries the `root` policy — `openbao_oidc` asserts this before it finishes. Neither policy it writes can seal, unseal, rekey, or change an auth method, so an administrator who signed in through Keycloak cannot lock the vault or grant themselves a way back in | `OPENBAO_ROOT_TOKEN` in `env.sh` |
| **GitLab** | `root` still signs in with its password. The local form stays on the page — `omniauth_auto_sign_in_with_provider` is deliberately not set, because it would leave the form reachable only by appending `?auto_sign_in=false`, and a break-glass path nobody can remember is not one | `GITLAB_ROOT_PASSWORD` in `env.sh` |
| **Grafana** | The local `admin` account still signs in. Kept precisely because Grafana federating means Grafana is unreachable when Keycloak will not start — and the cluster whose metrics would explain why is this one | `kv/grafana` in the vault, and `GRAFANA_ADMIN_PASSWORD` in `env.sh` |
| **Keycloak itself** | The `admin` account in the **master** realm is local, unfederated and **permanent** — `keycloak_break_glass` removes the `is_temporary_admin` marker Keycloak stamps on a bootstrap account, and removes any federation provider that appears in master. This is why applications and federated users live in `dev-lo`, and why everyday administration is the FreeIPA group `keycloak-admins`, which holds `realm-admin` on `dev-lo` and the `admin` realm role in master. Master is federated only through a provider filtered to that group's members, so it holds Keycloak's administrators and not the domain. Note the consequence: a member of that group can disable this account — what it keeps is that it does not depend on FreeIPA | `kv/keycloak` in the vault, and `KEYCLOAK_ADMIN_PASSWORD` in `env.sh` |
| **Longhorn UI** | **Unreachable**, and this is the one accepted regression. The proxy is the only front door | — |

Longhorn is worth being explicit about rather than quietly listing. Its UI had
**no authentication at all** before this — an Ingress straight to
`longhorn-frontend`, so anyone who could resolve the name could detach a volume
— and it is the only service here with nothing to configure, because Longhorn
has no authentication to point at Keycloak. So the choice was between no
authentication and a proxy that can fail, and the proxy wins. Nothing that
matters depends on it: Longhorn's data plane, its CSI driver and every volume
keep working, `kubectl` is unaffected, and the UI is recoverable by pointing
the Ingress back at `longhorn-frontend`.

The circular dependency this design most had to avoid is OpenBao's, and it is
worth naming: **the vault holds Keycloak's own database password**, delivered
by External Secrets. A vault that could only be opened through Keycloak could
not be opened on the day Keycloak will not start — which is exactly the day
someone needs that password. Hence the root token, untouched, and policies that
cannot widen themselves.

**Three secrets are still sealed into Git, and only three.** Each is needed
before the vault can be reached at all, which is the test for belonging there:

- the **registry credential**, which is how Flux pulls the charts that deploy
  OpenBao and ESO in the first place;
- the **cluster CA key pair**, which cert-manager needs before anything in the
  cluster has a certificate — including the vault's own ingress;
- the vault's **unseal keys**, which are what make it readable at all.

Anything else belongs in OpenBao. A secret added to
`gitops_source/vars/main.yml` should come with an argument for why it is in one
of those three positions.

The Sealed Secrets **sealing key** is the most valuable secret in the lab. It
decrypts every SealedSecret in Git, including the OpenBao unseal keys Phase 6b
will seal, which makes holding it equivalent to holding every runtime secret in
the cluster. Losing it without the `env.sh` break-glass copy makes OpenBao
unrecoverable.

It arrived in **Phase 6a**, not 6b as this document previously said. The
controller generates its own key pair the first time it starts, so the key
existed the moment Flux reconciled Sealed Secrets — before anything had been
sealed with it. A plan that waits for 6b to think about the backup leaves it
unprotected for the whole of 6a.

It is the one secret here that is **not** a shell export, because it is not a
value any playbook reads from the environment — it is a Kubernetes object:

```text
~/.config/rke2lab/sealed-secrets-key.yaml    mode 0600
```

Written with:

```bash
umask 077
kubectl -n kube-system get secrets \
  -l sealedsecrets.bitnami.com/sealed-secrets-key -o yaml \
  > ~/.config/rke2lab/sealed-secrets-key.yaml
```

The label selector matters: the controller names each key with a random suffix
(`sealed-secrets-keyXXXXX`) and **rotates by creating an additional key every
30 days**, keeping the old ones to decrypt what they sealed. Backing up one
named Secret captures whichever key happened to be current; the selector
captures the whole set, which is what a restore actually needs. Re-run the
command after each rotation.

**The restore is now automated.** `gitops_bootstrap` puts the backup into the
cluster before it installs Flux, which is the only window that works: Flux
deploys the Sealed Secrets controller, and a controller that starts without a
key makes one of its own. It restores only when the cluster has no key at all,
because re-applying a backup over a live controller's keys would push it
backwards past its own rotations.

It also strips `resourceVersion`, `uid` and `creationTimestamp` from the dump
first. `resourceVersion` is an optimistic-concurrency precondition, so applying
the backup verbatim against a cluster that already has the keys fails with a
conflict on an object nobody has touched — which is what `kubectl apply -f` on
this file does by hand, and why the manual command below is not simply what the
role runs.

By hand, for a cluster the role is not driving:

```bash
kubectl apply -f ~/.config/rke2lab/sealed-secrets-key.yaml
kubectl -n kube-system delete pod -l name=sealed-secrets-controller
```

**This backup has been restore-tested, destructively, on 2026-08-16** — before
the OpenBao unseal keys were sealed with it, which is the only order in which
the test is cheap. The live key was deleted, the controller was restarted so it
generated a replacement, a previously sealed probe was confirmed
*undecryptable* by that replacement, the backup was applied, and the probe
decrypted again. Confirming the failure is half the test: a restore that is
never proven to be necessary proves nothing.

The test taught one thing the procedure above does not say. **Restoring leaves
two keys in play**, because the controller had already created its own when it
started without one, and it seals *new* secrets with the newest key it holds.
A backup taken before the test and never re-taken would therefore cover nothing
sealed afterwards. Re-run the backup command after any restore, not only after
a rotation — the file now holds both keys.

`infra/pulumi/Pulumi.dev.yaml` stays in the project directory, but it is gitignored and
its values are encrypted by Pulumi. `PULUMI_CONFIG_PASSPHRASE` — the thing that makes
those values readable — is the part kept outside the repo.

Note that `PROXMOX_VE_PASSWORD` in `env.sh` is **stale** and does not authenticate.
Pulumi works because it reads its own encrypted `proxmox:password` from the stack
config, so the drift is invisible until something talks to the Proxmox API directly.
`pulumi config get proxmox:password` is the working value; the two should be
reconciled.

## Secrets that live outside `env.sh`

Two credentials are deliberately not in `env.sh`, because GitLab will only ever
disclose each of them once.

The first is the `rke2-nodes` deploy token that cluster nodes pull with. The
`rke2_publish` role creates it and records it at `/data1/gitlab/rke2-deploy-token.yml`
on `repo01`, mode `0600`, and both `playbooks/kubecp.yml` and `playbooks/kubewk.yml`
read it from there to build each node's `registries.yaml`. There is exactly one copy,
it is outside the repository, and rotating it means deleting that file and re-running
the role. The `gitops_source` role reads the same file to build the registry
credential it seals into the cluster-state repository, so the token has exactly one
origin and one recording no matter how many things consume it.

The second is `flux-cluster-state`, the project deploy token Flux reads the
cluster-state repository with. The `gitops_bootstrap` role creates it, records it at
`~/.config/rke2lab/flux-deploy-token.yml` on the controller at mode `0600`, and
writes it into the cluster as the `flux-system` Secret alongside the domain CA.
Scoped to `read_repository` on one project: a cluster that leaks it leaks the
ability to read its own declared state and nothing else. Rotating it means deleting
that file and re-running `playbooks/gitops.yml`.

A third credential outside `env.sh` is the shared GitLab admin personal access
token, recorded at `/data1/gitlab/admin-token.yml` on `repo01` at mode `0600`
by the `gitlab_admin_token` role. It is the one credential the whole GitLab-
admin surface of the automation uses: `rke2_publish` creates the group, the
projects and the deploy token with it, and `gitops_source` and
`gitops_bootstrap` create their project setup and deploy token with it. The
mint signs into the instance's web UI as root and POSTs the token form in
a second or two, with the `gitlab-rails` runner kept only as the fallback
for a controller that has no root password. Before the shared role existed
each of the three roles minted its own copy per run and a full automation
paid for it three times. Now the
token is minted at most once a day: the role records the value (GitLab
discloses it exactly once), and every later inclusion proves the recorded
token still authenticates with a single `GET /user` before reusing it. A
replacement is minted only when the record file is missing or the proof
fails, and superseded tokens are never revoked — they outlive the run and
simply expire. That is the deliberate posture change: a live admin token now
persists on `repo01` between runs instead of being revoked at the end of
every role, and the bound on a leaked copy of the record file is the
token's one-day expiry rather than the end of the run. Rotating it means
deleting that file; the next run of any including role proves the missing
record and mints a fresh one.

The cluster's intermediate CA is a fourth thing outside `env.sh`, for a
different reason — it is a key pair rather than a value. `ipa_sub_ca` writes
it to `~/.config/rke2lab/k8s-ca/` at mode `0700` and `gitops_source` seals
it into the repository from there. The private key never leaves the
controller in plaintext.

The cluster's kubeconfig is the other credential that never enters the repository. It
holds cluster-admin client certificates, and it exists in exactly two places: on each
server at `/etc/rancher/rke2/rke2.yaml`, mode `0600` root-owned, and on the controller
at `~/.kube/dev-lo.config`, mode `0600`, written there by the `rke2_server` role. It is
not in `env.sh` because nothing reads it from the environment — `KUBECONFIG` names the
path, not the contents.

## Rotation

The operator-facing version of this section — one table of every credential,
where it is changed, and which playbook pushes the change outward — is
`docs/source/tasks/rotating-credentials.rst`. **A secret added to or removed
from this document is added to or removed from that page in the same change.**
A rotation procedure that exists only in the design record is one nobody will
find on the day they need it.

Both WireGuard private keys were previously committed in plaintext, so they exist in
git history and should be treated as exposed. To rotate:

```bash
# Generate a new private key. The public key is derived by automation.
wg genkey
```

For each end:

1. Put the new **private** key in `~/.config/rke2lab/env.sh`.
2. Re-run `ansible-playbook playbooks/tunnel_controller_access.yml`, which reconfigures
   both ends in one pass and validates the result.

**Step 2 used to be step 3, and the step between them is gone as of 2026-08-17.**
It was: derive the public key by hand and paste it into the *other* end's
`*_peer_public_key` in `group_vars`. Both public keys are now derived from the
private keys by `roles/controller_tunnel`, and neither appears in inventory at
all.

That manual step is worth recording rather than quietly deleting, because of how
it failed. A WireGuard peer with a stale public key does not report an error:
the interface comes up, `wg show` lists the peer, and `systemctl status` is
green. The only symptom is that `latest-handshakes` stays at 0 and every
playbook afterwards times out against an internal host — with an error naming
the host and saying nothing about the tunnel. The third play of
`tunnel_controller_access.yml` now checks exactly that, so a rotation that goes
wrong fails on the rotation.

Rotating breaks the tunnel until both ends are re-applied, so run the playbook from a
path that does not depend on the tunnel itself.

## Rules

- Never write a secret into `ansible/`, `infra/`, or `plan/`.
- Tasks that consume a secret set `no_log: true`.
- The repo `.gitignore` blocks common secret filenames as a backstop, not as the
  primary control — the primary control is that secrets are never in the tree.
