==========================================
Adding a GitOps-managed service
==========================================

Every service in this cluster follows the same pipeline. Nothing is applied by
hand, nothing is edited in GitLab, and nothing pulls from the internet — which
means adding a service is mostly about getting its *artifacts* into the
environment before its manifests ask for them.

The whole path, in order:

.. code-block:: text

   1. artifacts    images + chart into the manifest, mirrored to GitLab
   2. mirror rules  only if the image's upstream host is not already rewritten
   3. manifests    a directory under apps/ in the GitOps source tree
   4. secrets      authored into env.sh, written to OpenBao, read by ExternalSecret
   5. identity     an entry in inventory_keycloak_applications, if it federates
   6. render       gitops.yml -e gitops_source_push=false, read the diff
   7. push         gitops.yml, then flux reconcile
   8. document     add it to this guide

1. Artifacts
============

Add every image and the chart to ``ansible/inventory/group_vars/repo/artifacts.yml``.

Images are ``type: mirror`` — copied registry to registry by skopeo, never
written to disk, and always ``retention: transit``:

.. code-block:: yaml

   - name: myapp
     phase: 6
     retention: transit
     type: mirror
     image: quay.io/vendor/myapp:1.4.2
     digest: >-
       sha256:...

The digest is the integrity control, not a note. Get it with
``skopeo inspect docker://quay.io/vendor/myapp:1.4.2``.

.. important::

   Mirror with ``--all --preserve-digests``. A single-platform copy with
   rewritten manifests **404s** the moment a chart pins an image by digest, and
   the failure surfaces as a pull error naming an image that plainly exists.

The chart is ``type: file``, staged onto Apache and then pushed as an OCI
artifact:

.. code-block:: yaml

   - name: myapp-chart
     phase: 6
     retention: transit
     type: file
     url: https://vendor.example/charts/myapp-1.4.2.tgz
     sha256: ...
     dest: charts/myapp-1.4.2.tgz

Then add it to ``inventory_rke2_publish_chart_sets`` in
``group_vars/repo/main.yml``. ``name`` and ``version`` must equal what the
chart's own ``Chart.yaml`` declares — Helm derives the push target from the
tarball, so a mismatch pushes correctly and is then looked for under a name
nothing holds.

.. code-block:: console

   $ ansible-playbook playbooks/cluster_services.yml

2. Mirror rules
===============

A node can only pull what its ``registries.yaml`` rewrites. The list of
rewrites, ``inventory_rke2_node_registry_mirrors`` in
``group_vars/all/main.yml``, uses a **host-level catch-all**: one entry per
upstream host whose whole namespace tree is redirected to the mirror, not one
per namespace.

.. code-block:: yaml

   - upstream: quay.io
     pattern: "^(.*)"
     replacement: rke2/images/$1

So the question when onboarding an image is not *which namespace* it is in but
*which host* it is pulled from. If that host already has a catch-all entry —
docker.io, ghcr.io, gcr.io, quay.io, mcr.microsoft.com, registry.redhat.io,
cgr.dev, public.ecr.aws, icr.io, nvcr.io, and the others in the list — there
is nothing to do. The rewrite is already there and the image lands at
``registry.gitlab.dev.lo/rke2/images/<everything after the host>``.

Only when the image comes from a host **not** in the list do you add an entry
to ``group_vars/all/main.yml``:

.. code-block:: yaml

   - upstream: registry.example.com
     pattern: "^(.*)"
     replacement: rke2/images/$1

.. warning::

   Because the rules are per-host, a new namespace under an already-listed
   host costs nothing — no edit, no restart. Only adding a **new host** changes
   ``registries.yaml``, and RKE2 regenerates containerd's ``hosts.toml`` from
   that file at service start, so that specific case costs a rolling restart of
   the nodes. That is the one-time cost per host, paid once, not per namespace.

   Docker Hub official images are ``docker.io/library/*`` by the time
   containerd resolves them; the ``docker.io`` catch-all already covers them.

3. Manifests
============

Create ``ansible/files/gitops_source/cluster-state/apps/myapp/`` with a
``kustomization.yaml.j2`` and the resources, and add the directory to
``apps/kustomization.yaml.j2``. Copy the shape of an existing app — ``garage``
is a good StatefulSet example, ``keycloak`` a good one for a service with a
database and OIDC.

The parts that are specific to this environment:

**Chart source.** Every HelmRelease installs from
``oci://registry.gitlab.dev.lo/rke2/charts`` via the shared ``HelmRepository``
in ``flux-system``. Never a public chart repository.

**Storage.** ``storageClassName: longhorn`` (two replicas) for anything whose
loss matters; ``longhorn-single`` only for caches and scratch.

**Ingress.** Traefik plus a certificate, and DNS follows automatically because
the cluster's CoreDNS answers every single-label name under ``k8s.dev.lo`` with
the ingress address:

.. code-block:: yaml

   metadata:
     annotations:
       cert-manager.io/cluster-issuer: k8s-ca
   spec:
     ingressClassName: traefik
     tls:
       - hosts: [myapp.{{ gitops_source_cluster_domain }}]
         secretName: myapp-tls
     rules:
       - host: myapp.{{ gitops_source_cluster_domain }}

**Versions stay literal in the manifest.** The rendered tree takes environment
identity as variables — domain, realm, addresses — and nothing else. A chart
version hoisted into ``group_vars`` turns a readable manifest into a variable
lookup with no reader.

4. Secrets
==========

Never a plain Secret in Git. Three steps:

#. Author the value into ``~/.config/rke2lab/env.sh`` (single quotes — a
   double-quoted ``$`` is silently expanded away), and add its name to
   ``bootstrap/env.sh.example``.
#. Add a KV write to the ``openbao_secrets`` role so the vault holds it.
#. Give the workload an ``ExternalSecret`` pointing at that path through the
   ``openbao`` ClusterSecretStore.

Sealing into Git is for exactly three things — the registry credential, the
cluster CA, and the unseal keys — because each is needed *before* the vault can
be reached. A fourth needs an argument for why it is in one of those positions.

If the service needs object storage, create the bucket and key in Garage first
and put the key into the vault; see :doc:`../components/object-storage`.

5. Identity
===========

If the service speaks OIDC, add an entry to
``inventory_keycloak_applications`` in ``group_vars/controller/main.yml``:

.. code-block:: yaml

   - name: myapp
     description: What it is
     base_url: https://myapp.k8s.dev.lo
     redirect_uris:
       - https://myapp.k8s.dev.lo/oauth/callback
     client_secret: "{{ lookup('env', 'OIDC_CLIENT_SECRET_MYAPP') }}"

That one entry drives three roles: ``ipa_sso_groups`` creates
``myapp-admins`` and ``myapp-users`` in FreeIPA, ``keycloak_clients`` creates
the client with ``admin`` and ``user`` roles and binds the groups to them, and
the workload reads the secret from the vault.

Redirect URIs are exact, never wildcarded. Author the client secret rather than
reading one back from Keycloak — that is what keeps the cold-rebuild ordering
free of a second pass.

If the service has **no** authentication of its own, put an oauth2-proxy in
front of it in reverse-proxy mode; ``apps/longhorn-auth`` is the working
example. Its cookie secret must be exactly 16, 24 or 32 bytes, or it refuses to
start with an AES key size error that never mentions cookies.

.. code-block:: console

   $ ansible-playbook playbooks/cluster_init.yml

6. Render and review
====================

.. code-block:: console

   $ cd ansible
   $ ansible-playbook playbooks/gitops.yml -e gitops_source_push=false

This renders the tree, seals into it, prints the diff and stops before the
commit. Read the diff. It is the only way to see what Flux is about to be told
without telling it.

Then render the chart and read what it produces — several faults in this
environment came from a Helm value being accepted where it meant nothing and
reported as success:

.. code-block:: console

   $ helm template myapp ./myapp-1.4.2.tgz -f values.yaml | less

7. Push and reconcile
=====================

.. code-block:: console

   $ ansible-playbook playbooks/gitops.yml
   $ flux reconcile source git flux-system
   $ flux reconcile kustomization apps --with-source
   $ flux get helmreleases -A
   $ kubectl -n myapp get pods

8. Document it
==============

Add the service to :doc:`managing-services`, to
:doc:`../reference/service-urls`, and — if it is a new technology rather than
another workload — give it a page under :doc:`../components/index`. Add any
credential it introduces to :doc:`rotating-credentials`. See
:doc:`../reference/maintaining-this-guide`.
