===========================
The environment at a glance
===========================

Everything in ``dev.lo`` runs as a virtual machine on a single Proxmox
hypervisor. Eight VMs, all Ubuntu 24.04, all root-only — there is no
unprivileged login account anywhere in the estate.

Hosts
=====

.. list-table::
   :header-rows: 1
   :widths: 14 20 8 8 50

   * - Host
     - FQDN
     - vCPU
     - RAM
     - What it does
   * - ``repo01``
     - ``repo01.dev.lo``
     - 4
     - 10 GiB
     - The only host with internet access. WireGuard gateway, SOCKS5 proxy,
       APT caching proxy, Apache artifact host, GitLab
   * - ``core01``
     - ``core.dev.lo``
     - 2
     - 6 GiB
     - FreeIPA: LDAP, DNS, NTP and the ``dev.lo`` certificate authority
   * - ``kubecp01-03``
     - ``kubecp0N.dev.lo``
     - 2
     - 6 GiB
     - RKE2 control plane and etcd. Tainted, so no workload schedules here
   * - ``kubewk01-03``
     - ``kubewk0N.dev.lo``
     - 4
     - 10 GiB
     - RKE2 workers. Every workload and every Longhorn replica lives here

Addresses are fixed. Control plane nodes are ``192.168.2.21-23``, workers
``192.168.2.31-33``, ``core01`` is ``192.168.2.4`` and ``repo01`` is
``192.168.2.99`` internally and ``192.168.1.20`` externally.

.. warning::

   The eight VMs are allocated 64 GiB on a hypervisor with 62.8 GiB. This works
   only because Proxmox hands out guest memory on demand. **Do not enable
   ballooning or memory reservations**, and watch swap on the *hypervisor*
   rather than in the guests — zero host swap is the health signal.

Networks
========

.. list-table::
   :header-rows: 1
   :widths: 26 24 50

   * - Network
     - CIDR
     - Internet
   * - External / lab
     - ``192.168.1.0/24``
     - Yes, via ``192.168.1.1``
   * - Internal
     - ``192.168.2.0/24``
     - **None**
   * - Controller tunnel
     - ``10.66.66.0/30``
     - Management only
   * - Cluster LoadBalancer pool
     - ``192.168.2.40-52``
     - Internal only

``repo01`` is the only dual-homed host, and it is deliberately **not** a
default gateway to the internet for internal hosts. Internal nodes can reach
the services ``repo01`` publishes and nothing else.

Two addresses in the LoadBalancer pool are pinned because something outside the
cluster is configured to reach them: ``192.168.2.40`` is the cluster's own DNS,
and ``192.168.2.41`` is the ingress every ``k8s.dev.lo`` hostname resolves to.

Names
=====

There are two zones and one delegation between them:

``dev.lo``
   Owned by FreeIPA on ``core01``, which is authoritative and has **no
   forwarders** — there is no upstream resolver reachable from the internal
   network, so forwarding would only make every non-``dev.lo`` lookup hang.
   Hosts, ``gitlab.dev.lo`` and ``registry.gitlab.dev.lo`` live here.

``k8s.dev.lo``
   Owned by the cluster. FreeIPA forwards the whole subdomain to a second
   CoreDNS running in the ``cluster-dns`` namespace on ``192.168.2.40``, and
   holds no records inside it. That is the point: a name a GitOps-managed
   service needs should not require a second manual step in the domain.

   Single-label names (``grafana.k8s.dev.lo``) resolve to the ingress address.
   Two-label names (``garage.garage.k8s.dev.lo``) resolve to that
   LoadBalancer Service's own address.

Certificates
============

FreeIPA is the root CA for the domain. It signed an intermediate,
``k8s-ca.dev.lo``, which lives in the cluster as a cert-manager
``ClusterIssuer`` named ``k8s-ca`` and issues every certificate under
``k8s.dev.lo`` automatically. A browser that trusts the FreeIPA CA trusts
everything in the environment; see :doc:`access`.

How a change reaches the cluster
================================

.. code-block:: text

   ansible/files/gitops_source/cluster-state/   (Jinja templates, in this repo)
        |  ansible-playbook playbooks/gitops.yml
        v
   GitLab: platform/cluster-state                (rendered, sealed, committed)
        |  Flux GitRepository + Kustomizations
        v
   The cluster                                   (reconciled continuously)

Nothing in that chain is edited in the middle. Editing GitLab's copy is
overwritten by the next render, by design.

The automation controller
=========================

The machine this repository lives on, outside the Proxmox environment. It runs
Pulumi (the VMs) and Ansible (everything else), and reaches the internal
network through a point-to-point WireGuard tunnel terminated on ``repo01``.
Lose the controller and you rebuild it from ``bootstrap/`` plus a backup of
``~/.config/rke2lab/``; see ``plan/CONTROLLER.md``.
