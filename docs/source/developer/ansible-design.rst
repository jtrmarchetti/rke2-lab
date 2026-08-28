====================================
The Ansible automation: how it works
====================================

`automation-design` covered the chain (this repo → controller → GitLab → Flux)
and the artifact pipeline. This page covers the other half: the Ansible half
itself — how the automation is built, and why the pieces are arranged the way
they are. The rules are enforced by ``plan/ANSIBLE_STANDARDS.md``; this page
is the map of the result, and it says *why* where the layout is surprising.

What lives where
================

.. code-block:: text

   ansible/
   ├── playbooks/      orchestration: one file per phase, imported by site.yml
   ├── roles/          behaviour: one role per capability, environment-agnostic
   ├── inventory/
   │   ├── hosts.yml   the eight estate VMs + the controller, as groups
   │   └── group_vars/<group>/
   │       ├── main.yml        inventory_<ns>_<noun>  (desired state)
   │       └── artifacts.yml   where the group's artifacts are pinned
   ├── files/<role>/   templates, per role, OUTSIDE the roles
   └── preflight       every playbook except gitops and controller
                      imports a secrets precheck first

The one layout choice to understand first: **templates do not live inside
their role.** Every role's templates sit under ``ansible/files/<role>/``, and
the role names the directory once in its ``vars/main.yml``:

.. code-block:: yaml

   # roles/time_sync/vars/main.yml
   _var_time_sync_files_dir: "{{ role_path | dirname | dirname }}/files/time_sync"

Why: the configuration a host ends up running is the thing that gets read the
most, and under one tree it can be read without opening twenty roles first.
The cost — a role is no longer a copy-elsewhere unit — is accepted because
these roles are not published anywhere.

A playbook is one phase
=======================

``site.yml`` is eleven ``import_playbook`` lines, in build order:

.. code-block:: text

   controller → repo01 → core01 → gitlab → kubecp → kubewk
   → controller tooling → cluster_services → gitops
   → cluster_init → gitops (again)

One non-obvious thing about that order:

* **``gitops`` appears twice.** The first push creates the GitOps tree; the
  vault it deploys (OpenBao) does not exist yet, so the unseal keys cannot be
  sealed yet. ``cluster_init`` creates the vault; the second ``gitops`` run
  seals the keys into a tree that already exists.

All ten phase playbooks except ``gitops.yml`` and
``controller.yml`` start by importing
``preflight_secrets.yml`` with their own list of required environment
variables. The two exceptions carry no such list because they need
nothing it would assert: ``gitops.yml``'s one environment value
(``OPENBAO_UNSEAL_KEYS``) is re-read from the ``env.sh`` file on disk,
the mechanism shown in the Secrets section below; ``controller.yml``
passes only ``inventory_*`` values and derives the rest from
``hostvars``, so it has no secret precheck to run:

.. code-block:: yaml

   # playbooks/gitlab.yml (abridged — its list has three entries)
   - name: Verify required secrets are present
     ansible.builtin.import_playbook: preflight_secrets.yml
     vars:
       _preflight_required_env:
         - GITLAB_ROOT_PASSWORD
         - FREEIPA_ADMIN_PASSWORD

The precheck asserts each named variable is set and non-empty and fails with
``source ~/.config/rke2lab/env.sh`` as the remedy. The list is passed on the
import, so each playbook states its own secret dependencies rather than
sharing one that silently covers more than is needed.

How a value reaches a host
==========================

The variable spelling is the design. Where a value is defined determines how
it is spelled, so its origin is readable at the point of use:

.. list-table::
   :header-rows: 1
   :widths: 30 26 44

   * - Defined in
     - Spelled
     - Example
   * - ``group_vars/<group>/``
     - ``inventory_<ns>_<noun>``
     - ``inventory_gitlab_external_url``
   * - ``roles/<r>/defaults/``
     - ``<role>_<ns>_<noun>``
     - ``gitlab_root_password``
   * - ``roles/<r>/vars/``
     - ``_var_<role>_<ns>_<noun>``
     - ``_var_gitlab_compose_file``
   * - a task's own ``vars:``
     - ``_<ns>_<noun>``
     - ``_gitops_env_sh_text``

The load-bearing rule: **an ``inventory_*`` value reaches a role only through
a playbook line that names it.**

.. code-block:: yaml

   # playbooks/core01.yml (abridged — the task passes five such values)
   - name: core01 | Include time synchronization role
     ansible.builtin.include_role:
       name: time_sync
     vars:
       time_sync_sources: "{{ inventory_time_sync_sources }}"

Each of those ``vars:`` lines is the interface: it states, at the point where the value
crosses from inventory into the role, which values cross. A role that
"picks up" an inventory variable by sharing a name has an invisible
interface — nothing states the coupling, and a rename in one place silently
stops the value arriving in the other. The standards doc's version of the
rule, with the worked example of ``rke2_publish`` refusing to read
``artifact_stage``'s manifest variable even though both roles need it, is
worth reading when a coupling starts to look tempting.

The same discipline runs down a level: a role's public parameters live in
``defaults/`` and are validated by ``meta/argument_specs.yml``; internal
constants in ``vars/``; computed values in the task that computes them. A
role reads three things and nothing else: its defaults, its vars, and the
parameters its caller passed.

Idempotency is a design constraint, not a property
===================================================

The working rule: **a second run of any playbook reports zero changes.** That
is not a testing nicety, it is what makes every run safe and makes the
controller's whole "re-run until green" loop meaningful. The mechanisms that
serve it:

* **Declarative modules over ``command``/``shell``** — the module knows the
  target state and reports ``ok`` when it is already reached. Where
  ``command`` is unavoidable, ``changed_when`` is written down.
* **Handlers for restarts** — a task *notifies* a handler; the handler runs
  once at the end of the pass if anything actually changed. A task that
  restarts a service conditionally restarts it every time, which breaks the
  rule.
* **Check mode is a real mode** — a task that cannot say what it *would* do
  in ``--check`` is a task with an undocumented assumption. The standards
  doc requires check-mode safety unless the task says why it cannot.

Secrets: one author, many consumers
===================================

Every credential is authored exactly once — in
``~/.config/rke2lab/env.sh`` on the controller — and pushed outward by the
playbook that owns it. Automation reads it with ``lookup('env', ...)``; the
preflight check above is what catches a forgotten ``source``. Two
consequences are worth knowing:

* ``lookup('env')`` reads the environment the ``ansible-playbook`` process
  *started* with. A value edited into ``env.sh`` mid-run is invisible to
  that run — re-source, re-run.
* Values cross the controller boundary as task-local facts, never as
  ``set_fact`` overrides of role state, and ``no_log`` defaults true.

The ``gitops.yml`` playbook illustrates the edge: its first task re-reads
``OPENBAO_UNSEAL_KEYS`` from the ``env.sh`` file on disk (falling back to the
process environment), because that value must survive being absent from the
current shell without breaking the seal step.

Lint: what is skipped, and why
==============================

``ansible/.ansible-lint`` skips five entries, four of them argued in a
comment there — read it rather than re-deriving it:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Rule
     - Why it is off
   * - ``key-order[task]``
     - The repository orders keys as name → controls → module → vars so the
       module block stays unbroken and whether a task runs is visible before
       what it does
   * - ``name[casing]``
     - Task names are prefixed with their file (``main | Ensure …``) so a
       failure names the file to open; that reads as a casing violation
   * - ``var-naming[no-role-prefix]``
     - Internal variables are spelled by *origin* (``_var_``, ``_reg_``,
       ``_item_``), public ones by role name — the rule expects the role
       name first on everything
   * - ``yaml[line-length]``
     - The naming scheme makes some registered-result indexing unavoidably
       wide; the name wins over the column limit

The validation baseline for any Ansible change is ``yamllint .``,
``ansible-lint``, and ``ansible-playbook -i inventory/hosts.yml
playbooks/site.yml --syntax-check``, with a ``--check`` run where behavior
changed.

.. note::

   The recurring *patterns* — marker files, out-of-band recovery, the Flux/
   Ansible ownership split — are where the design decisions concentrate.
   They get their own page: :doc:`ansible-patterns`.
