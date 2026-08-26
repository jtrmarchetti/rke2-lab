=============
Health checks
=============

The commands that say whether the estate is healthy, in the order they are
useful. All of them run from the controller — no browser, no internal host.

The three that cover the most ground
====================================

.. code-block:: console

   $ kubectl get nodes                    # six Ready, no NotReady
   $ kubectl get pods -A | grep -Ev 'Running|Completed'   # empty is healthy
   $ flux get kustomizations
   $ flux get helmreleases -A

A healthy estate answers the last two with **seven** Kustomizations all
``READY=True`` — ``apps``, ``flux-system``, ``garage-ready``, ``infra-configs``,
``infra-controllers``, ``observability``, ``unsealer`` — and **eight**
HelmReleases all ready — cert-manager, external-secrets, longhorn, alloy,
kube-prometheus-stack, loki, tempo, openbao.

Flux is the layer that reconciles everything inside the cluster, so its two
``get`` commands are the widest single net you can cast: a fault in nearly any
service shows up as a HelmRelease or Kustomization that stops saying
``Applied``/``Ready``.

The ones that catch what Flux cannot
====================================

.. code-block:: console

   $ kubectl -n longhorn-system get volumes.longhorn.io   # attached + healthy replicas
   $ kubectl -n openbao exec openbao-0 -- bao status      # unsealed
   $ kubectl get certificate -A                            # all Issued
   $ kubectl get externalsecret -A                         # all Synced

The first two are the ones that are silent in a Flux report: Longhorn's replica
state and the vault's seal state are not things Flux watches. Sealed is normal
for the first ~15 seconds after a restart — that is the unsealer's poll
interval, not a fault (see :doc:`troubleshooting`).

What to do with each result
===========================

* **A Kustomization or HelmRelease not ready** — ``flux reconcile`` it after
  reading why: :doc:`../components/gitops` has the controller-log path, and
  :doc:`troubleshooting` the symptom entries. A HelmRelease that says
  ``upgrade retries exhausted`` needs ``flux reconcile helmrelease <name>
  -n <ns> --force`` once the cause is fixed.
* **A pod stuck** — ``kubectl describe`` on it, then
  ``kubectl logs --previous``; :doc:`troubleshooting` orders the pod symptoms.
* **A volume degraded** — one of three workers is down, or its
  ``/var/lib/longhorn`` did not mount: :doc:`storage-longhorn`.
* **A certificate not ``Issued``** — ``kubectl describe certificate`` and then
  the ``certificaterequest``; nearly always the ``k8s-ca`` issuer rather than
  the workload.
* **An ExternalSecret not ``Synced``** — it names the vault path it cannot
  read, and the usual cause is that nothing has written that path yet.

.. note::

   The machine's own version of this page is the ``verify/`` suite
   (:doc:`verify-suite`): it drives the real flows and reads data back out of
   the datasources instead of trusting "no error". Run it before you believe
   the environment is well, not just after a change.
