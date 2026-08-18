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

A healthy cluster shows every scrape target up, Loki returning labelled pod
logs, and an OTLP span pushed through Alloy readable in Tempo.

.. note::

   **The control plane has to be told to expose its metrics.** RKE2 binds etcd,
   the scheduler, the controller manager and kube-proxy to ``127.0.0.1``, and
   every ServiceMonitor kube-prometheus-stack ships scrapes the node address —
   so out of the box the stack reports the whole control plane down on a
   cluster that is completely healthy, with ``etcdInsufficientMembers`` firing
   while all three members are ``Ready``.

   The fix is in Ansible, not in the chart: ``rke2_server_expose_metrics`` and
   ``rke2_agent_expose_metrics``, which add ``etcd-expose-metrics`` and the
   ``bind-address`` arguments to ``/etc/rancher/rke2/config.yaml``. Applying it
   restarts RKE2, so it is a rolling control plane operation
   (:doc:`../tasks/node-maintenance`).

   These listeners bind to every interface. The scheduler and controller
   manager sit behind authentication and refuse a scrape with no bearer token;
   etcd's port 2381 and kube-proxy's port 10249 do not. On the lab segment that
   is accepted; on a routable network it would need a host firewall.

Grafana
=======

.. important::

   **Grafana's persistence is off, and its dashboards and datasources come from
   ConfigMaps in Git.** Anything created in the UI is lost at the next restart.
   That is why federated users get Admin or Viewer and never Editor — an edit
   permission that silently discards edits is worse than not having one.

   Add a dashboard the same way you add anything else: through
   :doc:`../tasks/adding-a-service`.

.. important::

   **Datasource UIDs are pinned** — ``loki``, ``tempo``, ``prometheus``,
   ``alertmanager``. Dashboards in Git reference them by UID, and an unpinned
   datasource is assigned a generated one at first provisioning, so every
   dashboard would bind to a value that changes if Grafana is rebuilt.

   Adding a ``uid`` to a datasource that **already exists** does not migrate in
   place. Grafana's provisioning looks the datasource up by UID, does not find
   it, and aborts the whole file with ``Datasource provisioning error: data
   source not found`` — while the API refuses to delete it as read-only. The
   symptom is dashboards rendering "Datasource not found" on every panel.

   Because persistence is off, the fix is simply to restart Grafana, which
   rebuilds its database from the ConfigMaps:

   .. code-block:: console

      $ kubectl -n observability rollout restart deploy/kube-prometheus-stack-grafana

Sign-in is Keycloak (``grafana-admins`` → Admin, ``grafana-users`` → Viewer).
``allow_sign_up`` is ``true`` and has to be — no Grafana account exists for a
federated user until their first sign-in creates one. What keeps that safe is
``role_attribute_strict``, which refuses a user carrying no role at all.

The local ``admin`` account is kept on purpose: Grafana federating means
Grafana is unreachable when Keycloak will not start, and the cluster whose
metrics would explain why is this one. Its password is in ``kv/grafana`` and in
``GRAFANA_ADMIN_PASSWORD``.

Logs
====

Every pod's logs are collected — Alloy runs on all six nodes and reads the
container log files, labelling each line with its namespace, pod, container and
node. Loki keeps them for 168 hours, with chunks in Garage.

Two dashboards read them, both in Grafana's **Logs** folder:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Dashboard
     - Use it for
   * - Logs — cluster overview
     - Where the logs are coming from and which look like failures. Volume by
       namespace, by node and by pod, and every error-shaped line cluster-wide.
       Start here when something is wrong and it is not yet clear where.
   * - Logs — explorer
     - One namespace, pod or container at a time, with a search box. Where you
       land once the overview has told you where to look.

Anything the dashboards cannot express is a LogQL query away in **Explore**,
against the ``Loki`` datasource:

.. code-block:: text

   {namespace="keycloak"}                          all logs from a namespace
   {namespace="keycloak"} |= "error"               containing a string
   {pod=~"loki-.*"} |~ "(?i)(fatal|panic)"         case-insensitive regex
   sum by (namespace) (count_over_time({namespace=~".+"}[5m]))

.. warning::

   **There is no severity label.** Alloy ships each line with its labels and
   does not look inside it, so nothing parses ``level``. The error panels on
   both dashboards regex the line for ``error``, ``fatal``, ``panic`` and
   ``exception`` — a heuristic that both misses lines and over-counts them (a
   line reading "no errors found" matches). Treat those panels as a place to
   start looking, not as a count of failures.

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

Every alert rule in this cluster is a **Prometheus** rule, shipped by
kube-prometheus-stack. Grafana manages none of its own.

That matters for where you read them. Grafana's *Alerting* section opens on the
Grafana alertmanager by default, which here is empty — and an empty list reads
like a quiet cluster rather than like the wrong view.

.. important::

   To see alerts with their messages: **Alerting → Active notifications**, then
   switch the datasource picker from ``Grafana`` to ``Alertmanager``. The
   dashboard panels titled "alerts" show *counts* only; the text lives here.

Alertmanager has **no receiver**. The chart's default route sends everything to
a ``null`` receiver and that is left in place, because nothing on this network
can reach a mail server or a webhook endpoint. Alerts are therefore visible in
Grafana and in Alertmanager's own UI and go nowhere else. Known limit, not a
fault.

The intended destination is **Splunk or Elasticsearch**, taking alerts as
events. Alertmanager reaches either through its generic ``webhook_config``, so
what is missing is an endpoint and its credentials rather than a change of
shape.

.. warning::

   Adding a receiver means setting ``alertmanager.config`` in
   ``apps/observability/metrics.yaml``, and declaring that key **replaces** the
   chart's default configuration wholesale. The default carries the inhibit
   rules that stop one critical alert arriving alongside the three warnings it
   caused. Copy them across rather than starting from an empty config.

Alertmanager's own UI, when you want it:

.. code-block:: console

   $ kubectl -n observability port-forward \
       svc/kube-prometheus-stack-alertmanager 9093:9093
   # then http://localhost:9093
