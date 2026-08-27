=================================
Secrets and certificates
=================================

Four components, one division of labour:

**OpenBao** holds every secret a *running workload* reads, with versions,
policies and an audit trail. **External Secrets Operator (ESO)** syncs those
into Kubernetes Secrets. **Sealed Secrets** holds only what must exist *before*
the vault can be reached at all. **cert-manager** issues every certificate in
the cluster.

OpenBao
=======

OpenBao 2.6.2, single-node with a Raft backend, in the ``openbao`` namespace.
Single-node deliberately: Raft fsyncs per commit — etcd's write pattern, on
storage already at etcd's floor — so a three-node HA cluster would cost more
than it buys here. It runs on the two-replica ``longhorn`` class, never on
``longhorn-single``.

.. code-block:: console

   $ kubectl -n openbao get pods
   $ kubectl -n openbao exec openbao-0 -- bao status
   $ export BAO_ADDR=https://bao.k8s.dev.lo

``bao status`` exits 0 when open, **2 when sealed**, and 1 when it is not
initialised or unreachable. Only the second is normal after a restart.

Sealing and unsealing
---------------------

OpenBao comes up **sealed** after every restart — a pod eviction, a node
reboot, a chart upgrade — and stays sealed until a threshold of key shares is
presented. The ``openbao-unsealer`` Deployment does that automatically: the
vault's own image running a shell loop that checks status every 15 seconds and
applies keys when the answer is "sealed".

.. code-block:: console

   $ kubectl -n openbao get deploy openbao-unsealer
   $ kubectl -n openbao logs deploy/openbao-unsealer --tail=20

There are five key shares and a threshold of three. Three are sealed into Git
for the unsealer; all five are in ``OPENBAO_UNSEAL_KEYS`` in ``env.sh`` as an
offline break-glass copy. Two copies that fail in different ways, deliberately.

By hand, if the loop is what is broken:

.. code-block:: console

   $ kubectl -n openbao exec -it openbao-0 -- bao operator unseal   # three times

Signing in
----------

``bao login -method=oidc`` opens a browser and listens on ``localhost:8250``;
the UI at ``https://bao.k8s.dev.lo`` works too. Group membership decides the
policy: ``sso-admins`` reads and writes ``kv``, ``sso-users`` reads.

.. important::

   **Policies attach at login.** A session opened before a group binding
   changed carries none of the new access until the next sign-in — which looks
   exactly like a broken policy. Sign out and back in before reaching for the
   policy.

   Neither OIDC policy can seal, unseal, rekey or change an auth method. An
   administrator who signed in through Keycloak cannot lock the vault or grant
   themselves another way in. The **root token** in ``env.sh`` still carries
   the ``root`` policy, and it is the break-glass path when Keycloak is down —
   which matters because the vault holds Keycloak's own database password.

What is in it
-------------

.. list-table::
   :header-rows: 1
   :widths: 26 34 40

   * - Path
     - Holds
     - Read by
   * - ``kv/keycloak``
     - ``admin-password``, ``db-password``
     - ``keycloak-secrets``
   * - ``kv/garage-cluster``
     - ``admin-token``, ``metrics-token``, ``rpc-secret``
     - ``garage-secrets``
   * - ``kv/garage``
     - ``access_key``, ``secret_key``
     - ``garage-s3`` (Loki, Tempo)
   * - ``kv/grafana``
     - ``username``, ``password``
     - ``grafana-admin``
   * - ``kv/oidc-grafana``
     - ``client-secret``
     - ``grafana-oidc``
   * - ``kv/oidc-longhorn``
     - ``client-secret``, ``cookie-secret``
     - ``longhorn-auth``

Values are written by the ``openbao_secrets`` role from ``env.sh``, and it
reads each entry before writing it — KV v2 keeps versions, and a role that
wrote unconditionally would push real history out of the retained versions on
every run.

External Secrets
================

ESO 2.9.0 authenticates to OpenBao with the Kubernetes auth method — pod
ServiceAccount JWTs, no static credential stored anywhere — through one
``ClusterSecretStore`` named ``openbao``. Each workload declares an
``ExternalSecret``; ESO materialises a normal Kubernetes Secret from it and
refreshes hourly.

.. code-block:: console

   $ kubectl get clustersecretstore
   $ kubectl get externalsecret -A
   $ kubectl -n observability describe externalsecret grafana-oidc

``SecretSynced`` is healthy. A failing one names the vault path it could not
read, and the usual cause is that nothing has written that path yet.

Sealed Secrets
==============

Three secrets are sealed into Git, and only three, because each is needed
before the vault can be reached at all:

* the **registry credential**, which is how Flux pulls the charts that deploy
  OpenBao and ESO in the first place;
* the **cluster CA key pair**, which cert-manager needs before anything has a
  certificate — including the vault's own ingress;
* the vault's **unseal keys**, which are what make it readable at all.

.. code-block:: console

   $ kubectl get sealedsecret -A
   $ kubectl -n kube-system logs -l name=sealed-secrets-controller --tail=50

.. danger::

   The **sealing key** is the most valuable secret in the lab: it decrypts every
   SealedSecret in Git, including OpenBao's unseal keys. It is backed up to
   ``~/.config/rke2lab/sealed-secrets-key.yaml`` on the controller, and the
   backup has been restore-tested destructively. The controller **rotates by
   adding a key every 30 days**, so back up with the label selector — which
   captures the whole set — and re-run it after any rotation *or restore*:

   .. code-block:: console

      $ umask 077
      $ kubectl -n kube-system get secrets \
          -l sealedsecrets.bitnami.com/sealed-secrets-key -o yaml \
          > ~/.config/rke2lab/sealed-secrets-key.yaml

cert-manager
============

cert-manager v1.21.1, with one ``ClusterIssuer`` named ``k8s-ca``: an
intermediate CA signed by FreeIPA, whose key pair lives on the controller at
``~/.config/rke2lab/k8s-ca/`` and reaches the cluster as a SealedSecret. No
ACME, and no exported FreeIPA key.

.. code-block:: console

   $ kubectl get clusterissuer
   $ kubectl get certificate -A
   $ kubectl -n keycloak describe certificate keycloak-tls
   $ kubectl get certificaterequest,order -A

Every Ingress that carries ``cert-manager.io/cluster-issuer: k8s-ca`` gets its
certificate automatically and renewed automatically. A ``Certificate`` stuck
``False`` is nearly always the issuer being unhealthy, not the workload.
