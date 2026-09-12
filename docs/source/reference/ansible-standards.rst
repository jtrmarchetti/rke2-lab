=================================================
Ansible standards
=================================================

This page is the source of truth for Ansible automation in this repository.
Every rule is mandatory unless it is explicitly marked as guidance. The intent
is to produce content that matches Red Hat CoP style: reusable roles,
explicit interfaces, idempotent tasks, validated inputs, and
inventory-driven configuration.

When in doubt, prefer the most specific Ansible module, keep logic in roles,
and make behavior visible through inventory and role parameters rather than
embedded assumptions.

The basis is Red Hat CoP Good Practices for Ansible
(`https://redhat-cop.github.io/automation-good-practices/`). Where these
rules are stricter than CoP guidance, they win.

Core principles
===============

- Automation must be repeatable, idempotent, and safe to rerun.
- Automation must work in ``--check`` mode without errors unless a task is
  inherently not check-safe and has a documented reason.
- Roles must be environment-agnostic. Inventory provides desired state.
- Playbooks orchestrate. Roles implement behavior. Templates render
  config.
- Prefer declarative modules over ``command`` or ``shell``.
- Use FQCN for every module.
- Treat role defaults as the public API for user-configurable values.
- Treat ``vars/main.yml`` as internal constants only.

Files and extensions
====================

- Use ``.yml`` everywhere. Never use ``.yaml``.
- Use ``.j2`` for all templates.
- Use ``.yml`` for inventory, ``group_vars``, ``host_vars``, role files, and
  playbooks.

Repository layout
=================

Inventory must be structured, not flat::

  inventory/
  ├── hosts.yml
  └── group_vars/
      └── <group>/
          └── main.yml

Every role must use this structure::

  roles/<name>/
  ├── defaults/main.yml
  ├── vars/main.yml
  ├── tasks/main.yml
  ├── handlers/main.yml
  └── meta/main.yml
      └── argument_specs.yml

Templates do not live inside the role. They live in a per-role subdirectory
of ``ansible/files/``::

  files/
  └── <role>/
      └── <name>.j2

The reason is that the configuration a host ends up running is the thing most
often read, and putting every template under one tree means it can be read
without opening twenty roles first. The cost is that a role is no longer a
self-contained unit you can copy elsewhere, a trade this repository accepts
because these roles are not published. See :doc:`../developer/ansible-design`
for the full argument.

Every path into that tree is an inventory variable. ``inventory_files_base``
(computed in ``inventory/group_vars/all/main.yml`` as
``{{ ansible_config_file | dirname }}/files``) anchors the per-file and
per-folder path variables that each play passes into its roles::

  # inventory/group_vars/repo/main.yml
  inventory_apt_proxy_template: >-
    {{ inventory_files_base }}/apt_proxy/apt-cacher-ng.conf.j2

  - name: repo01 | Include the apt proxy role
    ansible.builtin.include_role:
      name: apt_proxy
    vars:
      apt_proxy_template: "{{ inventory_apt_proxy_template }}"

The path keys are ordinary role parameters: the role declares them in
``defaults/main.yml`` set to ``null`` (documenting the interface) and
validates them in ``meta/argument_specs.yml``, exactly like any other input.
For folder-based consumers (``gitops_source``, ``gitops_bootstrap``) the
inventory variable holds the folder, not the file. A template task then reads
its path from its own parameter::

  ansible.builtin.template:
    src: "{{ time_sync_template }}"
    dest: /etc/chrony/chrony.conf

Always render with ``template`` unless the file is truly static.

Playbooks
=========

- Name every play.
- Set ``gather_facts`` explicitly on every play.
- Set ``become`` explicitly on every play.
- Use ``become: true`` at the play level when root is required.
- Never mix ``roles:`` and ``tasks:`` in the same play.
- Keep playbooks free of business logic.
- Prefer ``include_role`` inside ``tasks:`` plays when mapping inventory
  variables into role parameters.
- Use ``import_role`` only when static inclusion is intentional and
  documented.

Passing variables to roles
--------------------------

- Pass role inputs explicitly with ``vars:`` on the role include task when
  the value maps from inventory into role parameters.
- Use play-level ``vars:`` only for values that apply to the whole play.
- Keep role inputs in ``defaults/main.yml`` so callers can override them from
  inventory or from the role include task.
- Validate every public role input with ``meta/argument_specs.yml``.
- Use internal ``__`` variables inside the role for computed values, not for
  caller-supplied inputs.
- Prefer inventory variables over extra vars for desired state.
- Use extra vars for troubleshooting, debugging, validation gates, or
  exceptional overrides only.
- Keep variable scope as small as practical; avoid broad-scope variable
  injection when task or block scope is sufficient.
- Never write self-referential passthroughs into role arguments or
  ``import_playbook vars:``. ``"-e ..."`` where the key on the left already
  names the same variable recurses into itself at template time (``import_
  playbook vars:`` puts the value at playbook scope; a task that templates the
  very variable it defined fails at template time with a recursive-loop
  error). The role's own default covers the absent case, and an override
  reaches the role through normal variable precedence without a passthrough.

Compliant::

  ---
  - name: Configure repo host
    hosts: repo
    gather_facts: true
    become: true
    tasks:
      - name: Include disk setup role
        ansible.builtin.include_role:
          name: disk_setup
          apply:
            tags:
              - disk_setup
        vars:
          disk_setup_mount_path: "/data"
        tags:
          - disk_setup

Non-compliant::

  ---
  - hosts: repo
    roles:
      - disk_setup
    tasks:
      - name: Format disk
        ansible.builtin.command: mkfs.ext4 /dev/sdb

The ``serial:`` keyword
-----------------------

The play-level ``serial:`` keyword is templated **before** inventory
``group_vars`` and even play-level ``vars:`` enter variable scope. A knob
that lives in ``group_vars`` — ``serial: "{{ kubecp_serial | default(2) }}"``
where ``kubecp_serial`` is set in ``group_vars/all`` — silently degrades to
the default; the inventory value never reaches the keyword. Only a literal and
an extra var (``-e kubecp_serial=1``) do. Task-context resolution of the same
variable works fine, which is what makes the trap easy to miss.

- Put the effective width in the keyword's own default:
  ``serial: "{{ kubecp_serial | default(1) }}"``. The ``group_vars`` line
  stays as the documented value and keeps the default numerically consistent
  with it; ``-e`` remains the override path.
- Probe batch behavior with a no-op playbook: ``hosts: <group>``, the
  ``serial`` under test, one ``debug`` task — one ``PLAY [`` banner per
  batch.
- Never write ``serial: all`` on this estate. ansible-core rejects the word
  at batch computation; one batch of every host is ``serial: 100%``.

Roles
=====

Naming
------

- Use ``snake_case`` for role names. Never use dashes.
- Prefix every user-facing role variable with the role name.
- Prefix every role tag with the role name.

Variable naming
---------------

Where a variable is defined determines how it is spelled, so its origin is
readable at the point of use rather than only at the point of definition.

.. list-table::
   :header-rows: 1
   :widths: 30 30 40

   * - Defined in
     - Pattern
     - Example
   * - ``roles/<r>/defaults/*.yml``
     - ``<role>_<namespace>_<noun>``
     - ``gitlab_admin_password``
   * - ``roles/<r>/vars/*.yml``
     - ``_var_<role>_<namespace>_<noun>``
     - ``_var_gitlab_api_url``
   * - ``inventory/<group>/*.yml``
     - ``inventory_<namespace>_<noun>``
     - ``inventory_gitlab_url``
   * - A task's own ``vars:``
     - ``_<namespace>_<noun>``
     - ``_gitlab_registry_url``
   * - ``loop_control.loop_var``, Jinja ``for``
     - ``_item_<namespace>_<noun>``
     - ``_item_container_image``
   * - ``register:``
     - ``_reg_<namespace>_<noun>``
     - ``_reg_container_image``

Variable ownership
------------------

- Put only values that are correct in **any** environment in
  ``defaults/main.yml``. A domain name, an address, a URL, a credential or a
  hostname is not one of those, and belongs in inventory.
- Where a parameter has no environment-independent value, keep the key in
  ``defaults/main.yml`` set to ``null`` so the role's interface stays
  documented, and let the play that includes the role supply the value.
- Document every default with a short comment.
- Put internal constants, package lists, and magic values in
  ``vars/main.yml``. Nothing in ``vars/`` is a user input; it exists to make
  the role work.
- Do not put user inputs in ``vars/main.yml``.
- Do not use inline ``| default(...)`` to replace a missing default.

Argument validation
-------------------

- Every role must have ``meta/argument_specs.yml``.
- Validate every public role parameter with type, description, and default
  when a safe default exists.
- Keep role inputs narrow and explicit.

Role boundaries
---------------

A role reads three things and nothing else: its own ``defaults/``, its own
``vars/``, and the parameters its caller passed to the options declared in its
``meta/argument_specs.yml``. It never picks a value up from inventory by name
collision, from a play that happens to define it, or from another role's
namespace.

Where a role needs a value another role also uses, it declares that value as
its own parameter and the caller passes it to both. ``rke2_publish``
publishes the set ``artifact_stage`` staged, and takes
``rke2_publish_manifest`` and ``rke2_publish_artifact_root`` to do it — it
does not read ``artifact_stage_manifest``. Reading the sibling's variable
would work right up until the two roles were used apart, and would make a
rename in one role break the other with nothing in either to say why.

The same rule applies to a role that includes another role: the calling role
declares every parameter the called role needs in its own defaults and
argument spec, and passes them on. A called role never inherits values from
the caller's scope by accident.

Role behavior
-------------

- Do not reference inventory group names directly inside role code.
- Pass hosts, addresses, and group-derived values in as variables.
- Do not embed host lists in variables and loop over them.
- Target inventory groups from playbooks instead of looping over host arrays.
- Use handlers for restarts and reloads.
- Keep roles fully idempotent.
- Keep roles check-mode safe.

Task name prefixes
------------------

- Prefix every task name with the name of the file it lives in, single-file
  roles included: ``main | Ensure packages are installed``. A failure then
  names the file to open without a search.
- Handlers are the exception. A handler's name is what ``notify`` refers to,
  so it stays a plain name.

Tasks
=====

- Name every task, block, and play.
- Use imperative voice for names: ``Ensure``, ``Install``, ``Create``,
  ``Configure``.
- Avoid abbreviations in task names.
- Use 2-space indentation.
- Keep lines at or below 82 characters when practical.
- Use block style for module arguments.
- Use ``>-`` for folded multiline strings.
- Use ``true`` and ``false`` only. Never use ``yes``, ``no``, ``on``, ``off``,
  or title case booleans.
- In ``command``/``shell`` tasks, keep apostrophes out of the ``cmd`` body
  and any trailing comment: a stray ``'`` in a trailing comment once broke
  argument parsing. Read back the exact command a shell task will run, not
  just the exit code of a dry-run.

Task parameter order
--------------------

Red Hat CoP does not prescribe one global key order for every task keyword.
Keep task keys consistent within a role or repository so review and diff
quality remain high. Keep conditionals (``when``, ``changed_when``,
``failed_when``) written as plain Jinja expressions (without ``{{ }}``).

Repository convention for this project:

1. ``name`` first, prefixed with the task file's name — ``<file> | <task
   name>``. Handlers are the exception: a handler's name is the ``notify``
   contract, so it carries no prefix.
2. Then every task keyword, conditionals first: ``when``, ``changed_when``,
   ``failed_when``, ``become``, then the rest — ``register``, ``loop``,
   ``notify``, ``tags`` and so on.
3. Then ``vars:``, last of the keywords, directly above the module it feeds.
4. Then the module and its arguments, in YAML block style, at the end of the
   task.

The point is that a reader sees whether a task runs, and under what identity,
before reading what it does; that a task's local values sit next to the
module that consumes them; and that the module and its arguments stay together
as one unbroken block at the foot of the task rather than being split by
keywords.

This is a consistency convention, not an Ansible semantic requirement. It
disagrees with ansible-lint's ``key-order[task]``, which is skipped in
``ansible/.ansible-lint`` for that reason.

Jinja2 and conditionals
-----------------------

- Put one space inside template markers: ``{{ var }}``.
- Never wrap ``when``, ``changed_when``, or ``failed_when`` values in
  ``{{ }}``.
- Use bracket notation for dictionaries and facts.
- Use ``ansible_facts['key']``, not ``ansible_distribution`` or dot
  notation.
- Cast values before comparisons or arithmetic.
- Use anchored ``match``, ``search``, or ``regex`` expressions for string
  checks.

Package handling
----------------

- Pass the full package list to package modules.
- Never loop over package installation one item at a time when the module
  can take a list.

Retry and wait budgets
----------------------

- ``until``/``retries``/``delay`` loops are the standard way to wait for
  readiness. A loop is acceptable while every condition in it is true: the
  ``until`` expression is accurate and becomes true the moment the thing is
  actually ready (no polling past the state), ``delay`` stays short, and
  ``retries × delay`` stays proportional to the real worst case.
- Prefer a fast probe that detects the state change itself over a long blind
  poll. ``kubectl wait --for=condition=Ready`` with a bounded ``--timeout``
  plus a short confirm loop replaces a long loop of ``kubectl exec`` probes.
- No static ``sleep`` settle steps. A bare ``sleep N`` between two operations
  is a blind wait; the follow-up operation's own timeout and confirm loop
  carry the settle requirement.
- Prune budgets against measured evidence, not intuition. The timing
  callbacks in ``ansible/ansible.cfg`` (``profile_tasks``, ``profile_roles``,
  ``timer``) log per-task durations to ``ansible/ansible.log``; when
  hardware or the build shape changes, re-measure and cut
  ``retries``/``delay`` accordingly, and delete the dead budgets.

Module selection
----------------

- Use the most specific module available.
- Prefer ``ansible.builtin.command`` over ``ansible.builtin.shell`` unless a
  shell feature is required.
- When ``command`` or ``shell`` is unavoidable, add a short comment explaining
  why no dedicated module fits the task.
- Add ``changed_when`` to every ``command`` or ``shell`` task.
- Use module-native idempotency controls such as ``creates`` or ``removes``
  when they are available.
- Do not use ``lineinfile`` when ``template``, ``blockinfile``, ``ini_file``,
  or ``xml`` is the better fit.

Debug and error handling
------------------------

- Set ``verbosity: 2`` or higher on every ``debug`` task.
- Never use ``ignore_errors: true`` without an inline comment explaining why.
- Use ``meta: end_host`` to stop processing a host.
- Do not use ``meta: end_play`` for host-specific skipping.
- Use handlers for service restarts instead of conditional restart tasks.

Templates
=========

- Begin every template with ``{{ ansible_managed | comment }}``.
- Do not add timestamps or ``last modified`` lines.
- Use ``template`` for configuration files, even if the file is static today.
- Use ``copy`` only for binary files, remote artifacts, or files that will
  never need templating.
- Address templates through their path parameter, which the caller passes
  from inventory: ``{{ role }}_<what>_template`` for a file, or the role's
  folder parameter (the gitops roles render from a directory passed in the
  same way). The values are the ``inventory_files_base``-anchored variables
  described in the inventory section, and each path key is a validated role
  parameter set to ``null`` in ``defaults/main.yml``.
- A template may only reference ``inventory_*`` variables, except for values
  that are runtime-derived and cannot live in inventory: slurped markers or
  tokens slurped by a task in the same run (the etcd_rejoin join-pin marker,
  the registry deploy token), and public keys derived by a task (WireGuard
  key derivation, peer hostvars reads). Every such reference keeps its
  parameter on the role interface and is documented under
  :doc:`../developer/ansible-design`. A template that needs a constant instead
  of a parameter (a directory path, a dict of sysctl values, a list of
  installer flags) inlines the value over ``inventory_*`` variables; a
  parameter exists on the role interface only when a task consumes it.
- In the environment-specific templates under ``ansible/files/``, a
  ``{% if X | length > 0 %}`` guard whose entire body is a loop over ``X`` is
  noise — a loop over an empty list renders nothing, so the guard is dropped
  and the loop is written bare. Guards that emit a YAML key or section
  header, are feature switches, or handle value-override cases (for example
  the ``kube_cli_node_kubeconfig`` override to empty, or ``rke2_server`` list
  sections that would render a null key) are load-bearing and stay.
- Where an expression is composable from inventory but has mutually exclusive
  branches that a single static inventory value cannot express (the freeipa
  installer flags: ``--forwarders`` vs ``--no-forwarders``, ``--setup-ntp``
  vs ``--no-ntp``), use a named template-local ``{% set %}`` composed purely
  from ``inventory_*`` variables and document the branches in a template
  comment (see ``files/freeipa_server/docker-compose.freeipa.yml.j2``).
- Reuse an existing ``inventory_*`` variable when the value is the same value
  stored in the inventory for another purpose; do not mint a second variable
  for the same fact.

Variables and secrets
=====================

- Do not use ``set_fact`` to override role defaults, vars, or role
  parameters.
- Prefer task-scoped ``vars:`` over ``set_fact`` whenever possible.
- Do not store sensitive data in ``set_fact``.
- Do not use ``-e`` extra vars to define desired environment state.
- Reserve extra vars for debugging, troubleshooting, or explicit safety
  gates.
- Default ``no_log`` to ``true`` for sensitive values and allow explicit
  override only when debugging requires it.

Compliant::

  no_log: "{{ no_log_override | default(true) }}"

Inventory
=========

- Use a structured inventory directory.
- Use ``group_vars/<group>/`` subdirectories rather than flat
  ``group_vars/<group>.yml`` files.
- Template paths live in inventory, not in roles. ``inventory_files_base``
  resolves to ``ansible/files/`` from the ansible config file's directory,
  and every per-file path variable (one per template a role renders) and
  every folder variable (the gitops roles' source directories) builds on
  it. Roles receive those paths as ordinary, validated parameters.
- Store environment-specific values in ``group_vars`` and ``host_vars``.
- Keep roles free of environment-specific data. A role default that names
  this lab's domain, addresses or artifact host is a value in the wrong
  file.
- Name every inventory variable ``inventory_<namespace>_<noun>``, so that no
  inventory value can reach a role except through a playbook that names it.
- Pass inventory values into roles explicitly, on the ``include_role`` task::

    - name: core01 | Include the time synchronization role
      ansible.builtin.include_role:
        name: time_sync
      vars:
        time_sync_sources: "{{ inventory_time_sync_sources }}"

  Inventory that happens to share a role's parameter names is an invisible
  interface: nothing states which values a role reads, and a variable can be
  renamed in one place and silently stop arriving in another.
- Use inventory groups to target hosts instead of embedding host lists in
  vars.
- Never embed lists of hosts as variables and loop over them; target groups
  with ``hosts:`` instead.
- Treat inventory variables as desired state, not as facts.
- Do not hard-code hostnames, IP addresses, or domain names in role logic
  when the value can be passed in from inventory.

Collections and dependencies
============================

- Declare any collection dependency explicitly in role metadata when a role
  depends on non-core content.
- Use FQCN even when the collection is already declared.
- Keep role dependencies explicit and minimal.

Validation
==========

Validate documentation and automation changes with the narrowest useful
check. For Ansible content, the usual baseline is::

  yamllint .
  ansible-lint            # config: ansible/.ansible-lint
  ansible-playbook -i inventory/hosts.yml playbooks/site.yml --syntax-check

Add role-specific ``--check`` coverage when a change affects behavior. Fix
idempotency problems before widening scope.

Lint skips are argued in ``ansible/.ansible-lint`` with a comment per rule
(``key-order[task]``, ``name[casing]``, ``var-naming[no-role-prefix]``,
``yaml[line-length]`` + ``line-length``); read that file before disabling or
re-enabling anything. The timing callbacks enabled in ``ansible/ansible.cfg``
(``ansible.posix.profile_tasks``, ``profile_roles``, ``timer``) feed
``ansible/ansible.log`` and are the evidence base for any wait-budget or
parallelism change.

Documentation
=============

A change is not complete when the automation is correct. Two places follow
every change that has an operational effect:

- **This guide.** The operator's view (``sysadmin/``) and the builder's view
  (``developer/``): a new service, credential, URL, address, version,
  storage decision or newly learned failure mode all oblige a page in the
  right section. :doc:`../reference/maintaining-this-guide` maps the kind of
  change to the page, and ``make -C docs html`` must still pass — it builds
  with ``-W``, so a broken cross-reference fails it.

The test is the same one the rest of this file applies to code: could someone
who was not here follow it? A role that only its author can operate is not
finished, however idempotent it is.

Working notes
=============

Multi-step agentic work keeps a scratch pad and a verification log in
``agent_plans/`` (git-ignored: they are working state, not a source of
truth). A verification log records, per claim, the command or file that
grounds it — the standing rule is that a command or number is not written
into this guide as fact until it has a live check or a read of the file it
names, recorded there.

Performance claims get their evidence from the timing callbacks in
``ansible/ansible.cfg``, not from watch timings: ``ansible/ansible.log``
captures per-task durations for every run, and a wait-budget or
parallelism change is argued from those numbers.

Decision rules
==============

- If a dedicated module exists, use it.
- If a value can come from inventory, do not hard-code it in a role.
- If a task changes remote state, make its idempotency explicit.
- If a rule is unclear, prefer the most conservative option that preserves
  repeatability and reviewability.
