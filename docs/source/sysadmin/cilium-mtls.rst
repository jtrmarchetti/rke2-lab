====================================================
Cilium service-to-service mTLS: rollout and rollback
====================================================

East-west traffic is encrypted and authenticated by Cilium, not by a separate
mesh: the estate's packaged ``rke2-cilium`` chart runs SPIRE in-cluster for
identities (a SPIFFE SVID per endpoint), performs an agent-to-agent mTLS
handshake on port ``4250``, and encrypts the pod network with IPsec. A
CiliumNetworkPolicy with ``authentication.mode: required`` on a path turns
the requirement on for that path; the pilot is the SSO path,
keycloak to its database on 5432. This page is the runbook: how the feature
is enabled, how it is proven on the running estate, and how each piece is
rolled back.

The TLS boundary
================

Two TLS layers, and they must not move into each other:

* **Edge TLS — Traefik.** The ``platform`` Gateway in ``kube-system``
  carries one listener per edge host, each ``tls.mode: Terminate`` with a
  certificate chaining to the domain CA. It is the *only* place application
  TLS is terminated.
* **Service-to-service mTLS — Cilium.** SVID-backed mutual authentication
  plus IPsec on the pod network. It authenticates and encrypts *between*
  endpoints; it does not terminate application traffic and issues no edge
  certificates.

Traefik and the Gateway are ordinary endpoints on the encrypted pod
network: they simply ride it. Nothing in the Cilium values may pull the
edge job in — the verify suite asserts exactly that
(:doc:`verify-suite`), and a mesh-CA or trust-bundle key appearing in the
Cilium values is drift, not a feature.

What the build enables
======================

Five pieces, all in this repository:

* ``rke2-cilium`` HelmChartConfig values — the chart override that turns
  mutual authentication (in-cluster SPIRE install, server pinned to a
  worker) and IPsec encryption on, and ships ``dnsProxy`` transparent mode
  (a hard requirement of running the L7 proxy together with IPsec). Rendered
  onto the control plane by the ``rke2_server`` role.
  ``ansible/files/rke2_server/cilium-config.yaml.j2``
* The ``cilium-ipsec-keys`` Secret in ``kube-system`` — one generated PSK
  line, applied to the API through the RKE2 static-manifest directory on
  the control plane (cluster-scoped, so every node's agent mounts it)
  before the IPsec datapath starts. The key itself lives on the
  controller under ``~/.config/rke2lab/``, never in GitLab.
  ``ansible/roles/cilium_ipsec_keygen``
* The pilot policy — a required-auth CNP on the SSO path, in the GitOps
  ``apps`` tree so Flux owns it.
  ``ansible/files/gitops_source/cluster-state/apps/cilium-mtls/``
* Three image mirrors (``spire-server``, ``spire-agent``, and the SPIRE
  init busybox **by digest**, not tag — the tag moved under the chart).
  ``ansible/inventory/group_vars/repo/artifacts.yml``
* The verify modules that pin the configuration surface and the data plane
  on every build: ``verify/test_mtls.py``,
  ``verify/test_mtls_dataplane.py``.

Enabling
========

The feature is not a runtime toggle; it is source. A build on a tree that
carries the pieces above lands all of them:

.. code-block:: console

   $ source ~/.config/rke2lab/env.sh
   $ cd ansible
   $ ansible-playbook playbooks/site.yml        # cold, or a warm build

The ``cilium_ipsec`` keygen play (imported at the top of the RKE2 control
plane phase) generates the key line on the first cold build and re-reads it
on every later one, so the encrypted pod network is stable across warm
builds. The rendered HelmChartConfig and the key Secret land in
``/data1/rancher/rke2/server/manifests/`` on the control plane; RKE2's
packaged helm-controller applies the chart, and Flux applies the ``apps``
tree with the pilot CNP.

Expanding enforcement to another service is adding a required-auth CNP to
the ``cilium-mtls`` tree and re-running ``playbooks/gitops.yml`` — the
review gate (:doc:`../components/gitops`) applies:

.. code-block:: console

   $ cd ansible
   $ ansible-playbook playbooks/gitops.yml -e gitops_source_push=false

Policies are additive here (the estate runs
``enableNonDefaultDenyPolicies: false``), so a new CNP adds the requirement
to one flow and nothing else.

Verifying
=========

From the controller, in the order the layers fail:

The SPIRE stack — without a healthy server no SVID is ever issued and
every required-auth policy fails closed:

.. code-block:: console

   $ kubectl -n cilium-spire get sts spire-server
   NAME           READY   AGE
   spire-server   1/1
   $ kubectl -n cilium-spire get ds spire-agent
   NAME           DESIRED   CURRENT   READY   ...
   spire-agent    6         6         6
   $ kubectl -n cilium-spire get pvc
   spire-data-spire-server-0   Bound ...   longhorn-single
   $ kubectl -n cilium-spire exec spire-server-0 -c spire-server \
       -- /opt/spire/bin/spire-server healthcheck
   Server is healthy.

Expected logs, the server:

.. code-block:: console

   $ kubectl -n cilium-spire logs spire-server-0 -c spire-server --tail=50

A healthy stream:

.. code-block:: console

   time="..." level=info msg="Agent attestation request completed"
       agent_id="spiffe://spiffe.cilium/spire/agent/..."

One expected noise class: the operator's ``BatchUpdateEntry`` call against a
not-yet-existing entry reads as an error (``record not found``). It is
chart-internal and requires no action; the attestation stream is the
healthy signal.

The agents — the identity half, and the handshake channel:

.. code-block:: console

   $ kubectl -n kube-system exec <cilium-agent pod> -c cilium-agent -- cilium status | grep Encryption
   Encryption:              IPsec
   $ kubectl -n kube-system logs <cilium-agent pod> -c cilium-agent --tail=500 | grep -iE "spire|NetworkPolicy" | tail

A healthy agent's tail:

.. code-block:: console

   level=info msg="Connecting to SPIRE Delegate API Client" ...
   level=info msg="Imported CiliumNetworkPolicy"
       ciliumNetworkPolicyName=cnp-mutual-auth-keycloak-db

At agent start the SPIRE delegate lines come in bursts with
``SPIRE admin socket ... does not exist`` retries until the node's
spire-agent sidecar is up — transient, then quiet. Persistent spire
errors in a steady agent, or a node where the agent never came up, is
the failure: that node cannot mint SVIDs for what it hosts and its
required-auth connections fail closed. Port ``4250`` must stay open on
every node (the estate has no host firewalls today; a ufw/nftables rule
is exactly what ``verify/test_mtls.py`` probes for).

The kernel — IPsec that is configured in the agent but not installed in
the kernel still leaves traffic clear, so the node's xfrm state is where
the claim is proven:

.. code-block:: console

   $ kubectl -n kube-system exec <cilium-agent pod> -c cilium-agent -- ip xfrm policy | grep -c "proto esp"

One running cilium-agent pod per node is a host-network view of that
node's xfrm state; every node reports ESP policies (the estate reports
roughly a dozen). The fallback channel, on kernels that do not expose
procfs, is ``cat /proc/net/xfrm_policy``.

The data plane — a live handshake, not just configuration:

.. code-block:: console

   $ source ~/.config/rke2lab/env.sh
   $ ~/.venvs/rke2lab/bin/python -m pytest verify/test_mtls_dataplane.py -v

In a throwaway namespace, two pods exchange a connection under a
required-auth CNP (admitted only after the agent-to-agent handshake), a
third pod outside the rule's ``fromEndpoints`` is refused — per-connection
enforcement, not a config flag — and the edge hosts are re-checked
against the domain CA at the byte level.

The edge boundary — the half the feature must not touch:

.. code-block:: console

   $ kubectl get gateway platform -n kube-system -o jsonpath='{.spec.listeners[*].tls.mode}'
   Terminate Terminate Terminate Terminate Terminate
   $ ~/.venvs/rke2lab/bin/python -m pytest verify/test_gateway.py verify/test_sso.py -v

And the configuration-surface gate that must hold on every warm build so
a drift or a rollback of any one piece fails fast:

.. code-block:: console

   $ ~/.venvs/rke2lab/bin/python -m pytest verify/test_mtls.py -v

A healthy estate answers all of the above the way
:doc:`verify-suite` describes; the mTLS modules self-skip while the
HelmChartConfig carries no ``authentication`` block (the feature is not
deployed yet) and enforce on every build after.

Failure signature: required-auth policies fail **closed**. A stuck
SPIRE server or a blocked ``4250`` surfaces as denied connections — on
the pilot, as the SSO path refusing to reach its database — never as
clear-text traffic.

Rolling back
============

The pieces decouple, and each roll-back step re-proves itself against the
boundary: the edge TLS is never a rollback target.

Unenforce one flow
-------------------

Two cases, because the pilot is the pinned baseline.

**A flow added beyond the pilot.** Delete that flow's CNP from the
``cilium-mtls`` tree (or its entry in the ``apps`` kustomization) and
re-run the GitOps play; Flux prunes the object:

.. code-block:: console

   $ cd ansible
   $ ansible-playbook playbooks/gitops.yml

The flow returns to default-allow — the pre-feature baseline, because the
estate's posture is additive. Identity and encryption for the *rest* of
the estate are untouched, and the pinned suites must stay green:

.. code-block:: console

   $ kubectl -n <ns> get cnp <name of the flow CNP>    # NotFound
   $ ~/.venvs/rke2lab/bin/python -m pytest verify/test_mtls.py verify/test_gateway.py -v

**The pilot itself.** The verify module that pins it
(``test_pilot_mtls_policy_enforced_on_sso_path`` in ``test_mtls.py``)
asserts its existence, so unenforcing the pilot is a source revert that
must include that pin; a rollback that leaves the pin in place is a red
suite, and the red is the signal that the baseline moved. Revert the
``cilium-mtls`` tree (plus the test module's pin) in one source change,
then prove only the boundary:

.. code-block:: console

   $ kubectl -n keycloak get cnp cnp-mutual-auth-keycloak-db    # NotFound
   $ ~/.venvs/rke2lab/bin/python -m pytest verify/test_gateway.py verify/test_sso.py -v

Retire the feature entirely
---------------------------

Revert the source (the authentication, encryption and dnsProxy blocks in
``cilium-config.yaml.j2``, the keygen import, the ``cilium-mtls`` tree;
the mirror entries may stay — unreferenced transit-retained images are
harmless), then re-render:

.. code-block:: console

   $ cd ansible
   $ ansible-playbook playbooks/kubecp.yml

Then drop the key Secret: delete the rendered static manifest on the three
servers (``/data1/rancher/rke2/server/manifests/cilium-ipsec-keys-secret.yaml``),
delete the Secret from the API, and restart ``rke2-server`` on
the servers one at a time — the control-plane procedure in
:doc:`maintenance`, including the etcd check. The packaged helm-controller
re-installs the chart without the mTLS blocks; the in-cluster SPIRE stack
uninstalls with it.

Three things a rollback must **not** do:

* **Delete the spire-server PVC.** Deleting it re-generates the mesh CA
  and re-issues every SVID; it is not part of a routine rollback, and
  keeping it costs nothing.
* **Touch Traefik or the platform Gateway.** They keep terminating edge
  TLS throughout; ``verify/test_gateway.py`` proves it after every step.
* **Delete the controller's key file.** A re-enable re-uses the recorded
  key; the encrypted pod network then comes back byte-identical.

Confirm: the HelmChartConfig carries no ``authentication`` block (the
verify modules skip again), ``cilium status`` reports
``Encryption: Disabled``, and the SSO path still serves — it is back to
plain L3/L4, which is exactly the state before the feature.

Rotating the key
================

A deliberate operator act, like the Sealed Secrets sealing key. Delete
the recorded key file (and its ``env.sh`` line) and, for a rotation that
retires the old key, bump ``inventory_cilium_ipsec_keygen_key_id`` so the
new line re-keys every node pair:

.. code-block:: console

   $ source ~/.config/rke2lab/env.sh
   $ cd ansible
   $ ansible-playbook playbooks/kubecp.yml    # keygen regenerates; the manifest re-renders

Then restart ``rke2-server`` on the three servers one at a time (the
control-plane procedure in :doc:`maintenance`, including the etcd check) —
a static manifest is applied to the API at boot, so the re-rendered Secret
lands only on that restart. The agents themselves need no manual restart:
the chart's ``keyWatcher`` picks up the new Secret and re-keys the
datapath. See :doc:`rotating-credentials` for the key material's home.
