===========================================
Rebooting, draining and restarting the lab
===========================================

Rebooting one worker
====================

.. code-block:: console

   $ kubectl drain kubewk01.dev.lo --ignore-daemonsets --delete-emptydir-data
   $ ssh root@192.168.2.31 reboot
   # wait for Ready
   $ kubectl uncordon kubewk01.dev.lo
   $ kubectl -n longhorn-system get volumes.longhorn.io

**One at a time**, and wait for every volume to return to ``healthy`` before
the next. With two replicas, two workers down together is data loss.

Rebooting a control plane node
==============================

.. code-block:: console

   $ ssh root@192.168.2.21
   # systemctl stop rke2-server
   # /usr/local/bin/rke2-killall.sh
   # pgrep -a etcd            # MUST be empty
   # reboot

etcd survives ``systemctl stop`` and keeps holding its ports; the next start
then attaches to a stale datastore and blocks forever. Check by process name,
every time.

Wait for the node to rejoin and etcd to report three healthy members before
touching the next one. ``kube.dev.lo`` stays available throughout — kube-vip
floats the address to a surviving server.

Stopping and starting the whole environment
===========================================

Down, in this order:

#. Workers, drained then powered off.
#. Control plane, one at a time, with the etcd check above.
#. ``core01`` — everything resolves through it, so it goes late.
#. ``repo01`` last: it is the tunnel, the proxy and GitLab.

Up, in reverse. Then confirm, in this order:

.. code-block:: console

   $ ping 192.168.2.99 && ssh root@192.168.2.99 systemctl status wg-quick@wg0
   $ dig @192.168.2.4 gitlab.dev.lo
   $ kubectl get nodes
   $ kubectl -n openbao exec openbao-0 -- bao status     # sealed → unsealer works
   $ kubectl -n longhorn-system get volumes.longhorn.io  # all attached/healthy
   $ flux get kustomizations
   $ kubectl get pods -A | grep -Ev 'Running|Completed'

A full cold stop of every node has been done, and the cluster came back with
OpenBao unsealing itself and all eight Longhorn volumes attaching healthy.
Expect OpenBao to be sealed for the first 15 seconds — that is the unsealer's
poll interval, not a fault.

Changing VM sizing
==================

CPU, memory and disks are Pulumi's, in
``infra/pulumi/modules/vm_definitions.py``, with ``plan/TARGETS.md`` as the
source of truth the code must match. Applying a CPU or memory change
power-cycles the VM, so batch such changes.

.. warning::

   The estate is allocated more memory than the hypervisor physically has. Do
   not enable ballooning or memory reservations — with overcommitment this
   size, a reservation that cannot be satisfied is a VM that will not start.
   Watch swap on the **hypervisor**; zero has been the health signal through
   every phase.

Re-running automation
=====================

Every playbook is idempotent and safe to re-run; a second run should report no
changes.

.. code-block:: console

   $ cd ansible
   $ ansible-playbook playbooks/site.yml          # the whole lab, in phase order
   $ ansible-playbook playbooks/repo01.yml        # one host
   $ ansible-playbook playbooks/kubewk.yml --limit kubewk02

``site.yml`` runs ``gitops.yml`` twice on purpose: OpenBao's unseal keys cannot
predate the vault that the first push deploys.
