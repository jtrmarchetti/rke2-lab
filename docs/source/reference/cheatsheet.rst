===========
Cheat sheet
===========

.. code-block:: console

   $ export KUBECONFIG=~/.kube/dev-lo.config
   $ source ~/.config/rke2lab/env.sh
   $ source ~/.venvs/rke2lab/bin/activate

Is it healthy?
==============

.. code-block:: console

   $ kubectl get nodes
   $ kubectl get pods -A | grep -Ev 'Running|Completed'
   $ flux get kustomizations
   $ flux get helmreleases -A
   $ kubectl -n longhorn-system get volumes.longhorn.io
   $ kubectl -n openbao exec openbao-0 -- bao status
   $ kubectl get certificate -A
   $ kubectl get externalsecret -A

Look at one thing
=================

.. code-block:: console

   $ kubectl -n <ns> describe pod <pod>
   $ kubectl -n <ns> logs <pod> [--previous] [-f]
   $ kubectl -n <ns> exec -it <pod> -- sh
   $ kubectl -n <ns> get events --sort-by=.lastTimestamp | tail -20
   $ kubectl top nodes ; kubectl top pods -A --sort-by=memory
   $ k9s

Make Flux do something now
==========================

.. code-block:: console

   $ flux reconcile source git flux-system
   $ flux reconcile kustomization apps --with-source
   $ flux reconcile helmrelease <name> -n <ns> --force
   $ flux suspend|resume helmrelease <name> -n <ns>

Change the cluster
==================

.. code-block:: console

   $ cd ansible
   $ ansible-playbook playbooks/gitops.yml -e gitops_source_push=false   # review
   $ ansible-playbook playbooks/gitops.yml                               # apply
   $ ansible-playbook playbooks/cluster_init.yml     # vault, SSO, Garage
   $ ansible-playbook playbooks/cluster_services.yml # artifacts, registry
   $ ansible-playbook playbooks/site.yml             # everything, in order

Hosts
=====

.. code-block:: console

   $ ssh root@192.168.1.20          # repo01, from outside
   $ ssh root@192.168.2.4           # core01, over the tunnel

   $ sudo wg show                                   # tunnel handshake
   $ docker exec gitlab gitlab-ctl status           # repo01
   $ docker exec freeipa-server ipactl status       # core01
   $ systemctl status rke2-server|rke2-agent        # cluster nodes
   $ df -h / /data1 /var/lib/longhorn

Identity
========

.. code-block:: console

   # on core01
   $ docker exec -it freeipa-server bash
   $ kinit admin
   $ ipa group-add-member <app>-users --users alice
   $ ipa group-show <app>-admins

Build this guide
================

.. code-block:: console

   $ make -C docs html
   $ xdg-open docs/_build/html/index.html
