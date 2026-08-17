==============================
Platform hosts: repo01, core01
==============================

The two VMs outside the cluster. Everything in the cluster depends on both, and
neither is managed by Flux — they are configured by Ansible and run their
services as containers or systemd units.

repo01 — artifacts, packages, GitLab, the way in
================================================

The only host with internet access, and the single point through which every
artifact enters the environment.

.. list-table::
   :header-rows: 1
   :widths: 22 20 58

   * - Service
     - Port
     - Purpose
   * - WireGuard
     - ``51820/udp``
     - The controller's tunnel into ``192.168.2.0/24``
   * - SOCKS5 proxy
     - ``1080``
     - Browser access from ``192.168.1.0/24``
   * - apt-cacher-ng
     - ``3142``
     - Ubuntu packages for internal hosts, cached on demand
   * - Apache
     - ``80``
     - ``/data1/artifacts`` — everything that is not an OS package
   * - GitLab
     - ``443``
     - Git, the container registry, the package registry

Check it:

.. code-block:: console

   $ systemctl status apache2 apt-cacher-ng wg-quick@wg0
   $ docker ps                     # gitlab
   $ docker exec gitlab gitlab-ctl status
   $ df -h / /data1

.. warning::

   **Watch ``/`` on this host.** It is 32 GB and everything that grows is
   supposed to be on ``/data1``. It reached 79% once, carrying container images
   already published to GitLab. When ``/`` fills, GitLab, the APT proxy, Apache
   *and* the tunnel gateway stop together — which is every path into the
   environment at once.

Artifacts are staged idempotently: a re-run downloads nothing it already has.
Anything whose final home is GitLab is *transit* and is deleted locally after
publishing, and the staging role decides whether to re-fetch by asking GitLab
what it holds — not by looking at the local disk.

GitLab
------

.. code-block:: console

   $ docker exec gitlab gitlab-ctl status
   $ docker exec gitlab gitlab-ctl tail            # all logs
   $ docker logs --tail 50 gitlab

GitLab federates to Keycloak but ``root`` keeps its password login, and that
is deliberate: GitLab is upstream of everything the cluster runs. GitLab CE
cannot map an OIDC claim to administrator — that is an Enterprise feature — so
``gitlab-admins`` exists, is issued in the token, and is ignored. Administrator
rights are granted in GitLab, by ``root``.

.. important::

   Stateful containers here run with an explicit stop grace period. Docker's
   default is 10 seconds, and a database ``SIGKILL``\ ed mid-write comes back
   corrupt while the service manager inside the container still reports it
   healthy. Never shorten it.

core01 — FreeIPA
================

Identity, DNS, NTP and the certificate authority for the domain, all in one
container.

.. code-block:: console

   $ docker exec freeipa-server ipactl status
   $ dig @192.168.2.4 gitlab.dev.lo
   $ docker exec -it freeipa-server bash -c 'echo "$PW" | kinit admin'

.. important::

   ``ipactl status`` reports the last known intent, not that the service works.
   Prove it with a transaction: resolve a name, get a Kerberos ticket, bind to
   LDAP. Every phase of this build gates on a transaction for that reason.

Things that will bite you:

**No forwarders, on purpose.** ``core01`` answers for ``dev.lo`` and nothing
else. A query for an internet name from an internal host is *supposed* to fail.

**Negative answers are cached for an hour.** A name queried before it existed
stays NXDOMAIN on every resolver that asked early, for the zone's negative TTL.
Verify against the authority with ``dig @192.168.2.4`` before believing a
resolution failure, then flush the resolver that lied to you.

**The zone ``k8s.dev.lo`` is forwarded, not held.** FreeIPA has no records for
anything inside it; the cluster's own CoreDNS on ``192.168.2.40`` answers. A
broken cluster therefore breaks every ``k8s.dev.lo`` name, including the ones
you would use to diagnose it.

**LDAP is LDAPS.** Keycloak binds ``ldaps://core.dev.lo:636`` — ``core``, not
``core01``, because that is the name the certificate is issued for and the only
one the domain resolves.
