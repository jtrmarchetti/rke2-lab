===========================
The environment at a glance
===========================

Everything in ``dev.lo`` runs as a virtual machine on a three-node Proxmox
cluster. Eight VMs, all Ubuntu 24.04, all root-only — there is no
unprivileged login account anywhere in the estate.

Hypervisor
==========

Proxmox 9.2 runs as a **three-node cluster on dedicated hardware**:
``pve01``, ``pve02``, ``pve03``, each an i7-9700T with 62.6 GiB of RAM and
NVMe-backed ``local-lvm`` thin storage. The internal LAN the lab VMs live on
is a cluster-wide vxlan overlay, not a per-node bridge. The automation
controller is **not** one of these — it sits outside the Proxmox
environment entirely and drives it over a WireGuard tunnel. The overlay,
VM placement and per-VM specs live in :doc:`reference/proxmox`; keep this
page as the operator's summary.

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
     - 4
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

   **Do not enable ballooning or memory reservations**, and watch swap on the
   *hypervisor nodes* rather than in the guests — zero host swap is the
   health signal. Per-node placement keeps every node under commit; the
   headroom per node is set out in :doc:`reference/proxmox`.

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
``~/.config/rke2lab/``; :doc:`developer/automation-design` carries its
dependency manifest and cold-start order.
