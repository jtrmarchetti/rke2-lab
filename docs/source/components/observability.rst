=============
Observability
=============

Three kinds of telemetry — **metrics**, **logs** and **traces** — collected by
four Helm releases in the ``observability`` namespace, all reconciled by Flux,
all read through one Grafana at ``https://grafana.k8s.dev.lo``.

The releases are the unit Flux manages; the *pieces* are what runs inside them,
and kube-prometheus-stack alone is six of them. This page names each one, what
it does, and which of the three signals it belongs to — because when something
is missing, the useful question is which link in that chain stopped.

The pieces
==========

.. list-table::
   :header-rows: 1
   :widths: 26 10 12 52

   * - Piece
     - Release
     - Signal
     - What it does
   * - Prometheus
     - kube-prometheus-stack 88.5.4
     - Metrics
     - Scrapes every target in the cluster every 60s, stores the samples, and
       evaluates the alert rules. The datasource behind almost every dashboard
       panel
   * - Prometheus Operator
     - kube-prometheus-stack
     - Metrics
     - Turns ``ServiceMonitor`` and ``PodMonitor`` objects into Prometheus
       scrape configuration. It is how a new service gets scraped without
       anyone editing Prometheus — it ships a ServiceMonitor with itself
   * - node-exporter
     - kube-prometheus-stack
     - Metrics
     - Per-node hardware and OS metrics — CPU, memory, disk, filesystem,
       network. A DaemonSet on **all six nodes**, control plane included, since
       a node with no exporter is a node with no metrics
   * - kube-state-metrics
     - kube-prometheus-stack
     - Metrics
     - Turns the state of Kubernetes objects into metrics: how many replicas a
       Deployment wants versus has, why a pod is pending, whether a Job failed.
       Object state, not resource usage — that is node-exporter's half
   * - Alertmanager
     - kube-prometheus-stack
     - Metrics
     - Receives firing alerts from Prometheus, groups and deduplicates them,
       and routes them to a receiver. Here it has no receiver (see `Alerting`_)
   * - Grafana
     - kube-prometheus-stack
     - All three
     - The single UI over all four datasources. Dashboards and datasources come
       from ConfigMaps in Git; sign-in is Keycloak
   * - Loki
     - Loki 7.3.0
     - Logs
     - The log database. Indexes labels only — not line contents — which is why
       it is cheap and why searching text means scanning a label-selected
       window. Retains 168h; chunks go to Garage
   * - Tempo
     - Tempo 1.24.4
     - Traces
     - The trace database. Receives spans over OTLP, stores them by trace ID,
       and answers "show me trace X". Retains 48h; blocks go to Garage
   * - Alloy
     - Alloy 1.12.0
     - Logs + traces
     - The collector, a DaemonSet on all six nodes. Reads every container's log
       file and pushes to Loki, and accepts OTLP spans from anything in the
       cluster and forwards them to Tempo

.. note::

   **There is no separate OpenTelemetry Collector, on purpose.** Grafana Alloy
   *is* an OpenTelemetry Collector distribution. Running both would mean two
   collectors with overlapping receivers and no rule about which owns a
   pipeline — an ambiguity that only surfaces when telemetry goes missing.

How each signal gets there
==========================

Metrics — a pull
----------------

.. code-block:: text

   node-exporter    ─┐
   kube-state-metrics┤
   control plane     ├─ scraped by ─▶  Prometheus ─▶ Alertmanager
   any pod with a    ┘   (every 60s)       │            (no receiver)
   ServiceMonitor                          └──────▶  Grafana

Nothing pushes metrics. Prometheus goes and fetches them, and it knows where to
go because the Operator watched a ``ServiceMonitor`` appear. The selectors are
deliberately widened to watch **every** namespace — the chart's defaults are
scoped to the release's own labels, which silently ignores every ServiceMonitor
the rest of the cluster creates.

That "nothing pushes" is also why the control-plane note below matters: a target
that refuses the connection and a target that was never scraped both look like
missing data.

Logs — a push, from the node
----------------------------

.. code-block:: text

   /var/log/containers/*.log  ─▶  Alloy (DaemonSet)  ─▶  Loki  ─▶  Grafana
                                  labels: namespace,      │
                                  pod, container, node    └─▶ chunks in Garage

Alloy discovers pods through the Kubernetes API rather than by reading the
filesystem layout, so a change in how the container runtime names log files
does not silently stop collection. It attaches four labels and ships the line
otherwise untouched — see the severity-label warning under `Logs`_.

Traces — a push, from the application
-------------------------------------

.. code-block:: text

   your app  ─OTLP─▶  Alloy :4317 (gRPC)  ─▶  Tempo  ─▶  Grafana
                            :4318 (HTTP)       │
                                               └─▶ blocks in Garage

Nothing in this cluster emits traces on its own — an application has to be
instrumented and pointed at Alloy. That endpoint is
``alloy.observability.svc.cluster.local:4317``, and it is a cluster Service
rather than a pod-local port precisely so a workload does not have to know
which node it landed on.

Addresses and ports
===================

.. list-table::
   :header-rows: 1
   :widths: 46 12 42

   * - Service
     - Port
     - What answers there
   * - ``kube-prometheus-stack-prometheus.observability``
     - 9090
     - Prometheus UI and API — ``/targets`` is the one to know
   * - ``kube-prometheus-stack-alertmanager.observability``
     - 9093
     - Alertmanager UI and API
   * - ``loki.observability``
     - 3100
     - Loki push and query API
   * - ``tempo.observability``
     - 3200
     - Tempo query API — **not** 3100; a datasource on the wrong port reports
       "no data" rather than an error
   * - ``alloy.observability``
     - 4317 / 4318
     - OTLP receivers, gRPC and HTTP
   * - ``https://grafana.k8s.dev.lo``
     - 443
     - Grafana, through Traefik

Turned off on purpose
=====================

Three workers hold 30 GiB between them for every workload in the cluster, and
the control plane is tainted, so this stack competes with Longhorn, OpenBao,
Garage and Keycloak for the same memory. Each of these is a chart default left
switched off — worth knowing before enabling one, and before assuming its
absence is a fault:

.. list-table::
   :header-rows: 1
   :widths: 32 68

   * - Off
     - Why
   * - Thanos Ruler
     - There is one Prometheus and nothing to federate
   * - Admission webhook certgen Job
     - cert-manager is already here and issues the webhook certificate from the
       same authority as everything else — one fewer image and one fewer Job
   * - Loki read / write / backend
     - Loki runs in ``SingleBinary`` mode; the scalable and distributed modes
       exist to spread read and write paths across nodes this cluster does not
       have. Each would be another pod holding memory
   * - Loki caches, canary, gateway
     - Same reason. The rules sidecar is also off — alerting lives in
       Prometheus, and enabling it pulls an image from a registry namespace no
       mirror rewrite covers
   * - Grafana persistence and image renderer
     - Dashboards and datasources come from Git; see `Grafana`_
   * - Windows monitoring, ``tempoQuery``
     - No Windows nodes, and Tempo is queried through Grafana

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

   kube-proxy needs one thing more, in the chart: its Service selector is
   ``component: kube-proxy``, not the chart's default ``k8s-app``, which is
   kubeadm's label. RKE2's static pods do not carry it, so the Service matched
   no pod and produced no ``up`` series at all — and ``KubeProxyDown`` alerts
   on the metric being *absent*, meaning it fires with no failing target to
   point at.

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

A sidecar container in the Grafana pod is what makes that work: it watches
every namespace for ConfigMaps labelled ``grafana_dashboard`` or
``grafana_datasource`` and writes them into Grafana's provisioning directory.
So a dashboard arrives by commit, and survives Grafana being deleted entirely.

Four datasources are provisioned. Prometheus and Alertmanager come from the
chart itself; only Loki and Tempo are declared in
``apps/observability/datasources.yaml``, because a second Prometheus marked
``isDefault`` does not merge or override — it makes Grafana refuse to start.

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

Only the part in braces is indexed. Everything after it is a scan over the
lines those labels selected, which is why a query wants a namespace or pod
before it wants a string.

.. warning::

   **There is no severity label.** Alloy ships each line with its labels and
   does not look inside it, so nothing parses ``level``. The error panels on
   both dashboards regex the line for ``error``, ``fatal``, ``panic`` and
   ``exception`` — a heuristic that both misses lines and over-counts them (a
   line reading "no errors found" matches). Treat those panels as a place to
   start looking, not as a count of failures.

Storage and retention
=====================

Each of the four stateful pieces holds a Longhorn PVC, and the two that can
also age data out to object storage do:

.. list-table::
   :header-rows: 1
   :widths: 24 14 26 36

   * - Piece
     - PVC
     - Retention
     - Long-term store
   * - Prometheus
     - 8 GiB
     - 5 days, or 6 GB
     - None — the PVC is all of it
   * - Alertmanager
     - 1 GiB
     - n/a
     - None; the volume holds silences and notification state
   * - Loki
     - 5 GiB
     - 168 hours
     - Chunks in Garage (``loki`` bucket)
   * - Tempo
     - 2 GiB
     - 48 hours
     - Blocks in Garage (``tempo`` bucket)

Prometheus carries both a time and a size limit, and the size limit is the one
that actually protects the cluster: a window says how long data is kept, a size
says how much disk it may take, and only the second is a promise the disk can
keep.

Loki's and Tempo's credentials for Garage arrive from OpenBao through an
``ExternalSecret`` — the key was minted by Garage, stored in the vault by
Ansible, and reaches the pod as a Secret that neither Git nor a human ever
held.

These are small volumes on a small cluster — if a component starts evicting
data sooner than you want, grow the claim (:doc:`../tasks/storage-pvc`) rather
than reducing what is collected, and check worker memory before doing either.

.. note::

   The observability stack is the biggest single consumer of worker memory in
   the environment — roughly 800 MiB of requests for kube-prometheus-stack
   alone — and worker memory is the resource the hypervisor has least of. If
   the Proxmox host starts swapping, this is the first place to look.

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
