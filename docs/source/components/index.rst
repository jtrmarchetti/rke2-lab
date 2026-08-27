=========
The stack
=========

One page per layer. Each page says what the component is, where it runs, the
command that proves it is healthy, and what it looks like when it is not.

The health check that covers the most ground in one line is Flux's, because
everything inside the cluster is reconciled by it:

.. code-block:: console

   $ flux get kustomizations
   $ flux get helmreleases -A
   $ kubectl get pods -A | grep -Ev 'Running|Completed'

A healthy cluster answers those with seven ready Kustomizations, eight ready
HelmReleases, and no pods in the third — the set the
:doc:`../sysadmin/health-checks` page runs first.

.. toctree::
   :maxdepth: 1

   platform-hosts
   rke2
   gitops
   storage
   secrets
   identity
   object-storage
   observability
