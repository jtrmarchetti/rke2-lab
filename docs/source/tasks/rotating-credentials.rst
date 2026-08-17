=========================================
Rotating passwords, keys and certificates
=========================================

Almost every credential in this environment is authored in
``~/.config/rke2lab/env.sh`` and pushed outward by a playbook. Rotation is
therefore nearly always the same two steps — change the value, re-run the
playbook that owns it — and the table below says which playbook that is.

.. important::

   ``env.sh`` is mode ``0600`` in a mode ``0700`` directory, and every value is
   **single-quoted**. The superseded file used double quotes, and a ``$`` inside
   a password was expanded as an undefined shell variable and silently dropped
   — FreeIPA would have received a password five characters shorter than the
   one written down.

   Re-source it after every edit. ``lookup('env')`` reads the environment the
   ``ansible-playbook`` process started with, so an unsourced change is
   invisible to the run that is supposed to apply it.

The table
=========

.. list-table::
   :header-rows: 1
   :widths: 26 30 44

   * - Credential
     - Change it in
     - Then run
   * - WireGuard keys
     - ``env.sh`` (private keys only)
     - ``playbooks/tunnel_controller_access.yml``
   * - FreeIPA admin / directory manager
     - FreeIPA, then ``env.sh``
     - ``playbooks/core01.yml``
   * - GitLab ``root``
     - ``env.sh``
     - ``playbooks/gitlab.yml``
   * - ``RKE2_TOKEN``
     - ``env.sh``
     - Cluster rebuild — see below
   * - Keycloak admin / DB password
     - ``env.sh``
     - ``playbooks/cluster_init.yml``
   * - Grafana admin
     - ``env.sh``
     - ``playbooks/cluster_init.yml``
   * - Garage cluster tokens
     - ``env.sh``
     - ``playbooks/cluster_init.yml``, then restart Garage
   * - Garage S3 keys
     - Garage, then ``env.sh``
     - ``playbooks/cluster_init.yml`` (a second time — see below)
   * - OIDC client secrets (4)
     - ``env.sh``
     - ``playbooks/cluster_init.yml``
   * - oauth2-proxy cookie secret
     - ``env.sh``
     - ``playbooks/cluster_init.yml``
   * - ``rke2-nodes`` deploy token
     - Delete the recorded file
     - ``playbooks/cluster_services.yml``, then ``kubecp.yml``/``kubewk.yml``
   * - Flux deploy token
     - Delete the recorded file
     - ``playbooks/gitops.yml``
   * - Sealed Secrets sealing key
     - Rotates itself every 30 days
     - Re-take the backup
   * - Cluster intermediate CA
     - ``ipa_sub_ca`` output directory
     - ``playbooks/cluster_init.yml`` + ``gitops.yml``
   * - Service certificates
     - Nothing — cert-manager renews them
     - —

WireGuard
=========

.. code-block:: console

   $ wg genkey                       # once per end
   # put the PRIVATE key in env.sh; the public key is derived by automation
   $ source ~/.config/rke2lab/env.sh
   $ ansible-playbook playbooks/tunnel_controller_access.yml

Rotating breaks the tunnel until both ends are re-applied, so run this from a
path that does not depend on the tunnel. There is no public key to paste
anywhere — both are derived from the private keys, which is what removed the
step that used to fail silently.

OIDC client secrets
===================

Four values, one per federated service: ``OIDC_CLIENT_SECRET_GRAFANA``,
``_LONGHORN``, ``_OPENBAO``, ``_GITLAB``.

.. code-block:: console

   $ ${EDITOR:-vi} ~/.config/rke2lab/env.sh
   $ source ~/.config/rke2lab/env.sh
   $ cd ansible && ansible-playbook playbooks/cluster_init.yml

One run rewrites both ends — the vault entry (or the role's own configuration,
for OpenBao and GitLab) and the Keycloak client — because the secret is
authored rather than generated. The workloads pick it up from ESO within the
refresh interval; restart them if you want it immediately.

The cookie secret for the Longhorn proxy is different in kind: rotating
``OAUTH2_PROXY_COOKIE_SECRET`` signs everyone out and does nothing else. It
must be exactly 16, 24 or 32 bytes.

Vault credentials
=================

Changing a value in ``env.sh`` and re-running ``cluster_init.yml`` writes a
**new version** into OpenBao's KV v2 engine; the old versions stay for history.
The role reads before writing, so re-running without a change writes nothing.

Rotating the **root token** or the **unseal keys** is a different operation —
``bao operator generate-root`` and ``bao operator rekey`` — and both must be
followed by updating ``env.sh`` *and* re-running ``gitops.yml`` so the sealed
copy the unsealer reads matches. Do not rotate one without the other: the
unsealer failing is only discovered at the next restart.

Deploy tokens
=============

GitLab discloses each of these exactly once, so they are recorded rather than
re-derivable:

.. code-block:: console

   # the registry token cluster nodes pull with
   $ ssh root@192.168.2.99 rm /data1/gitlab/rke2-deploy-token.yml
   $ ansible-playbook playbooks/cluster_services.yml
   $ ansible-playbook playbooks/kubecp.yml playbooks/kubewk.yml

   # the token Flux reads the cluster-state repository with
   $ rm ~/.config/rke2lab/flux-deploy-token.yml
   $ ansible-playbook playbooks/gitops.yml

Certificates
============

Everything under ``k8s.dev.lo`` is issued by cert-manager from the ``k8s-ca``
ClusterIssuer and renewed automatically. You rotate a certificate by deleting
its Secret and letting cert-manager reissue:

.. code-block:: console

   $ kubectl -n <ns> delete secret <name>-tls
   $ kubectl -n <ns> get certificate -w

The **intermediate CA** itself is signed by FreeIPA and lives on the controller
at ``~/.config/rke2lab/k8s-ca/``. Reissuing it means re-running ``ipa_sub_ca``
and then ``gitops.yml``, which reseals it into the repository; every leaf
certificate is then reissued by cert-manager and every client that trusts the
FreeIPA root keeps working, because the root did not change.

Host and service certificates on ``repo01`` and ``core01`` come from FreeIPA
through the ``ipa_service_cert`` role; re-running that host's playbook reissues
them.

The RKE2 cluster token
======================

``RKE2_TOKEN`` is the shared join secret, the same value on every server and
worker. Changing it does not rotate anything on a running cluster — a node with
the wrong value is simply rejected at registration. Treat it as a rebuild-time
value.

After any rotation
==================

Re-take the Sealed Secrets backup if the sealing key was involved, and check
that the break-glass paths still work. A break-glass path that has never been
exercised since the change that could have broken it is a claim, not a fact —
all four were re-tested on 2026-08-17 for exactly that reason.
