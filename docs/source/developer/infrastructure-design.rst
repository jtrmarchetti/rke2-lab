============================
Infrastructure design
============================

The decisions that shape the estate, and where each one is owned. This page
does **not** restate the design record — it points to it and names the four
models every new service must fit.

The design record
=================

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Document
     - Owns
   * - ``plan/OVERVIEW.md``
     - Architecture, the artifact model, cross-phase rules
   * - ``plan/TARGETS.md``
     - Per-VM CPU, RAM, disks, addresses
   * - ``plan/CLUSTER_COMPONENTS.md``
     - Which component was chosen for each job, and why
   * - ``plan/PHASE<N>_IMPLEMENTATION.md``
     - What each build phase did and what it learned by failing

:doc:`../orientation` is the operator's summary of the same ground: the eight
VMs, the three networks, the two DNS zones, the certificate chain.

The four models a new service must fit
======================================

**The artifact model.** Nothing inside the internal network reaches the
internet; ``repo01`` is the only host that does, and every image, chart and
package enters the estate as an artifact through it — Tier 1 (Apache + APT
proxy) before GitLab exists, Tier 2 (GitLab's registries) after. A service you
add must arrive as a mirrored artifact: :doc:`adding-a-service` step 1.

**The DNS model.** Two zones with a delegation between them. ``dev.lo`` is
FreeIPA's, authoritative, with no forwarders. ``k8s.dev.lo`` is the
cluster's: FreeIPA forwards the whole subdomain to CoreDNS in-cluster and holds
no records inside it, so a single-label service name needs **no second manual
step** — it resolves to the ingress address automatically.

**The identity model.** FreeIPA is the authority for who a user is; Keycloak
holds only the mapping FreeIPA cannot express (``<app>-admins`` ⇒ admin of the
service). Two FreeIPA groups per application, mapped to Keycloak client roles.
A service that federates is an entry in
``inventory_keycloak_applications`` — see :doc:`adding-a-service` step 5.

**The secret model.** One value, one author: ``~/.config/rke2lab/env.sh``,
pushed outward by the playbook that owns it. A service's secret is written to
OpenBao and read by the workload through an ExternalSecret — never a plain
Secret in Git. :doc:`../components/secrets` is the layer map.

Why the estate is shaped this way
=================================

Three constraints do the explaining. One Proxmox hypervisor with no memory
headroom (the eight VMs are allocated more RAM than it has — do not enable
ballooning). One hypervisor whose storage is at etcd's fsync floor (two
replicas, one Longhorn disk per worker, nothing more). And the artifact
model, which exists because a lab that can never reach the internet still has
to be rebuildable from a bare checkout.

.. note::

   A default is a fact with an expiry date: when you touch a component, check
   it is still alive upstream, not merely still the default.
