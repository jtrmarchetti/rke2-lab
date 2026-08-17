=============
Observability
=============

Everything lands in Grafana at ``https://grafana.k8s.dev.lo``. Four components
feed it, all in the ``observability`` namespace, all reconciled by Flux.

.. list-table::
   :header-rows: 1
   :widths: 30 14 56

   * - Component
     - Version
     - Role
   * - kube-prometheus-stack
     - 88.3.0
     - Prometheus, Alertmanager, node-exporter, kube-state-metrics, Grafana
   * - Loki
     - 7.3.0
     - Logs. Chunks in Garage
   * - Tempo
     - 1.24.4
     - Traces, over OTLP. Chunks in Garage
   * - Alloy
     - 1.11.1
     - The collector: scrapes pod logs, receives OTLP spans, forwards both

Health
======

.. code-block:: console

   $ kubectl -n observability get pods
   $ flux get helmreleases -n observability

   # is Prometheus actually scraping
   $ kubectl -n observability port-forward svc/kube-prometheus-stack-prometheus 9090:9090
   # then http://localhost:9090/targets

   $ kubectl -n observability logs -l app.kubernetes.io/name=loki --tail=50
   $ kubectl -n observability logs -l app.kubernetes.io/name=alloy --tail=50

A healthy cluster shows around 45 scrape targets up, Loki returning labelled
pod logs, and an OTLP span pushed through Alloy readable in Tempo.

Grafana
=======

.. important::

   **Grafana's persistence is off, and its dashboards and datasources come from
   ConfigMaps in Git.** Anything created in the UI is lost at the next restart.
   That is why federated users get Admin or Viewer and never Editor — an edit
   permission that silently discards edits is worse than not having one.

   Add a dashboard the same way you add anything else: through
   :doc:`../tasks/adding-a-service`.

Sign-in is Keycloak (``grafana-admins`` → Admin, ``grafana-users`` → Viewer).
``allow_sign_up`` is ``true`` and has to be — no Grafana account exists for a
federated user until their first sign-in creates one. What keeps that safe is
``role_attribute_strict``, which refuses a user carrying no role at all.

The local ``admin`` account is kept on purpose: Grafana federating means
Grafana is unreachable when Keycloak will not start, and the cluster whose
metrics would explain why is this one. Its password is in ``kv/grafana`` and in
``GRAFANA_ADMIN_PASSWORD``.

Storage and retention
=====================

Prometheus (8 GiB), Alertmanager (1 GiB), Loki (5 GiB) and Tempo (2 GiB) each
hold a Longhorn PVC; Loki's and Tempo's long-term chunks go to Garage. These
are small volumes on a small cluster — if a component starts evicting data
sooner than you want, grow the claim
(:doc:`../tasks/storage-pvc`) rather than reducing what is collected, and check
worker memory before doing either.

.. note::

   The observability stack is the biggest single consumer of worker memory in
   the environment, and worker memory is the resource the hypervisor has least
   of. If the Proxmox host starts swapping, this is the first place to look.

Alerting
========

Alertmanager is deployed and reachable in-cluster. There is no external
notification path — the internal network cannot reach a mail server or a
webhook endpoint — so alerts are visible in Grafana and in Alertmanager's own
UI, and nowhere else. Treat that as a known limit rather than a fault.
