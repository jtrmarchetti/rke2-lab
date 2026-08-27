========
Versions
========

What is running, as of 2026-08-24. Every version here is pinned in a file, not
in prose — the file is named in the last column, and it is the thing to edit.
How to move a version is in :doc:`../developer/upgrades`; this page is the
table those edits point at.

Cluster
=======

.. list-table::
   :header-rows: 1
   :widths: 30 18 52

   * - Component
     - Version
     - Pinned in
   * - RKE2
     - ``1.36.3+rke2r1``
     - ``group_vars/kubecp|kubewk/main.yml``
   * - Cilium, CoreDNS, Traefik
     - RKE2's packaged charts
     - Follows the RKE2 release
   * - kube-vip
     - ``v1.2.3``
     - ``group_vars/kubecp/main.yml``

GitOps-managed
==============

.. list-table::
   :header-rows: 1
   :widths: 30 18 52

   * - Component
     - Chart
     - Pinned in
   * - cert-manager
     - ``v1.21.1``
     - ``cluster-state/infrastructure/controllers/cert-manager``
   * - Longhorn
     - ``1.12.1``
     - ``.../controllers/longhorn``
   * - OpenBao
     - ``0.29.2`` (app 2.6.2)
     - ``.../controllers/openbao``
   * - External Secrets
     - ``2.9.0``
     - ``.../controllers/external-secrets``
   * - Sealed Secrets
     - ``0.39.1``
     - ``.../controllers/sealed-secrets``
   * - Keycloak
     - ``26.7.2``
     - ``cluster-state/apps/keycloak``
   * - Garage
     - ``2.3.0``
     - ``cluster-state/apps/garage``
   * - kube-prometheus-stack
     - ``88.5.4``
     - ``cluster-state/apps/observability``
   * - Loki
     - ``7.3.0``
     - ``cluster-state/apps/observability``
   * - Tempo
     - ``1.24.4``
     - ``cluster-state/apps/observability``
   * - Alloy
     - ``1.12.0``
     - ``cluster-state/apps/observability``
   * - oauth2-proxy (Longhorn)
     - ``v7.15.4``
     - ``cluster-state/apps/longhorn-auth``
   * - PostgreSQL (Keycloak)
     - ``17.11-alpine``
     - ``cluster-state/apps/keycloak``

Every chart is also listed in ``inventory_rke2_publish_chart_sets``
(``group_vars/repo/main.yml``) and every image in
``group_vars/repo/artifacts.yml``. Those three places must agree: a version
bumped in the manifest and not in the chart set is pushed under one name and
looked for under another.

Controller and hosts
====================

.. list-table::
   :header-rows: 1
   :widths: 30 18 52

   * - Component
     - Version
     - Pinned in
   * - Ansible
     - ``14.3.1``
     - ``bootstrap/requirements-controller.txt``
   * - ansible-lint
     - ``26.8.0``
     - ``bootstrap/requirements-controller.txt``
   * - Pulumi CLI, k9s, Flux CLI, kubeseal
     - see manifest
     - ``group_vars/controller/artifacts.yml``
   * - Sphinx / furo (this guide)
     - ``9.1.0`` / ``2025.12.19``
     - ``docs/requirements.txt``
   * - Operating system
     - Ubuntu 24.04
     - Every host

.. note::

   **A default is a fact with an expiry date.** ``ingress-nginx`` was RKE2's
   packaged default and the plan reasoned from that months after the Kubernetes
   project retired it. When you touch a version here, check the component is
   still alive upstream — not merely that it is still the default.
