==================
Getting access
==================

Four kinds of access, and they are independent of each other. Losing one does
not cost you the others, which matters when you are diagnosing why something is
unreachable.

The tunnel
==========

All Ansible, ``kubectl`` and API traffic from the controller reaches
``192.168.2.0/24`` through a point-to-point WireGuard tunnel to ``repo01``.

.. code-block:: console

   $ sudo wg show
   $ ping -c1 192.168.2.99

.. important::

   A WireGuard peer with a stale key does not report an error. The interface
   comes up, ``wg show`` lists the peer, ``systemctl status`` is green — and
   ``latest handshake`` stays at ``0``, after which every playbook times out
   against an internal host with an error naming the host and never the tunnel.
   **Check the handshake, not the interface.**

To rebuild both ends:

.. code-block:: console

   $ cd ansible && ansible-playbook playbooks/tunnel_controller_access.yml

It configures the controller and the gateway in one pass and proves the path
carries traffic before it exits. Run it from a path that does not itself depend
on the tunnel.

The cluster API
===============

The kubeconfig holds cluster-admin client certificates and exists in exactly
two places: ``/etc/rancher/rke2/rke2.yaml`` on each server, and
``~/.kube/dev-lo.config`` on the controller.

.. code-block:: console

   $ export KUBECONFIG=~/.kube/dev-lo.config
   $ kubectl get nodes
   $ k9s          # a terminal UI over the same kubeconfig

The API is reached at ``https://kube.dev.lo:6443``, a virtual address
(``192.168.2.20``) that kube-vip floats across the three control plane nodes,
so losing one server does not cost you the API.

Web UIs from your workstation
=============================

Internal hosts have no route to your desk, so a browser reaches the web UIs
through the SOCKS5 proxy on ``repo01``. It listens on port ``1080`` and accepts
clients from ``192.168.1.0/24`` only.

.. code-block:: text

   SOCKS5 host: 192.168.1.20
   SOCKS5 port: 1080

Configure the browser to **resolve DNS through the proxy** (Firefox:
``network.proxy.socks_remote_dns = true``). Without that, your workstation
tries to resolve ``grafana.k8s.dev.lo`` locally, fails, and the failure looks
like the service being down.

Alternatively, from a host that already has the tunnel:

.. code-block:: console

   $ ssh -D 1080 root@192.168.1.20

.. note::

   ``repo01`` is dual-homed, so the proxy is configured with both of its
   interfaces and picks the source address per destination
   (``external.rotation: route`` in ``/etc/danted.conf``). If it is ever
   reduced to one interface, proxied requests to the internal network leave
   with the external address ``192.168.1.20`` and services that filter on
   source address answer ``403 Forbidden`` — the artifact host at
   ``http://192.168.2.99/`` allows the internal subnet and the tunnel only.
   The same URL keeps working over the tunnel, which is what makes the fault
   look like a browser problem. Check the source address in
   ``/var/log/apache2/artifact-host-access.log`` on ``repo01``.

Trusting the certificates
=========================

Install the domain root CA. That one certificate is enough for every internal
HTTPS name, including everything under ``k8s.dev.lo``:

.. code-block:: console

   $ curl -fsSL http://192.168.2.99/certs/dev.lo-ca.crt \
       | sudo tee /usr/local/share/ca-certificates/dev.lo-ca.crt >/dev/null
   $ sudo update-ca-certificates

It is fetched over plain HTTP on purpose — that is the only path that does not
already require the trust it is delivering. On the controller, this is what
``playbooks/controller.yml`` does for you.

Names under ``k8s.dev.lo`` are signed by the cluster's intermediate rather than
by the root directly, but cert-manager serves that intermediate alongside the
leaf, so the chain completes from the root alone. The intermediate is published
next to the root for the cases that need the certificate itself — pinning a CA
bundle, or inspecting a chain by hand:

.. list-table::
   :header-rows: 1
   :widths: 34 66

   * - Certificate
     - URL
   * - Domain root CA — install this one
     - ``http://192.168.2.99/certs/dev.lo-ca.crt``
   * - Cluster intermediate — reference only
     - ``http://192.168.2.99/certs/k8s-ca.dev.lo.crt``

Browse ``http://192.168.2.99/certs/`` to see what is published. Install them as
one file per certificate if you install both: ``update-ca-certificates``
ignores all but the first certificate in a bundle.

Firefox keeps its own trust store and does not read the system one. Import the
root under Settings → Privacy & Security → Certificates → View Certificates →
Authorities, ticking "Trust this CA to identify websites". Chrome and Edge use
the system store on Linux; on macOS and Windows they use the OS keychain or
certificate store.

.. note::

   A site that loads without warning is not proof the CA is trusted. Clicking
   through a warning once stores a permanent per-server exception, listed under
   the Servers tab of the same dialog, and that site then looks fine while
   every other name still warns. If one internal name is happy and the rest are
   not, check that tab before suspecting the certificates.

.. note::

   Ansible's own Python may verify against a different trust store than
   ``update-ca-certificates`` writes. Roles that talk to internal HTTPS
   endpoints pass ``/etc/ssl/certs/ca-certificates.crt`` explicitly for that
   reason; if a ``uri`` task fails verification while ``curl`` is happy, this
   is why.

Signing in
==========

Keycloak at ``https://sso.k8s.dev.lo`` is the front door for Grafana, OpenBao,
GitLab and the Longhorn UI. Your account is a FreeIPA account: Keycloak
federates the directory and holds no users of its own.

Access is granted in FreeIPA, not in Keycloak, and never in the service:

.. code-block:: console

   # on core01
   $ docker exec -it freeipa-server bash
   $ kinit admin
   $ ipa group-add-member grafana-users --users alice

Every application has exactly two groups, ``<app>-admins`` and ``<app>-users``,
which Keycloak maps to two client roles named ``admin`` and ``user``. What the
two mean is each service's own business — see :doc:`tasks/managing-services`.

Break-glass
===========

Single sign-on adds one new way for the lab to become unreachable, so every
service that could keep a local account did:

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Service
     - If Keycloak is down
   * - OpenBao
     - The root token still works (``OPENBAO_ROOT_TOKEN`` in ``env.sh``)
   * - GitLab
     - ``root`` still signs in with its password; the local form stays on the
       page deliberately
   * - Grafana
     - The local ``admin`` account still signs in
   * - Keycloak
     - The ``admin`` account in the **master** realm is local and unfederated
   * - Longhorn UI
     - **Unreachable.** The proxy is the only front door — an accepted
       regression. The CSI driver, the volumes and ``kubectl`` are unaffected

All four working paths were re-tested on 2026-08-17.
