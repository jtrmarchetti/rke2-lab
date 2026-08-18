---
name: ansible-verification
description: Verification-first workflow for writing or debugging Ansible playbooks and roles targeting Linux, FreeIPA (freeipa.ansible_freeipa), or Kubernetes (kubernetes.core). Use this before writing any Ansible task whose module parameters you haven't already confirmed against the installed collection, and before running any playbook for real.
---

# Ansible: verify the module interface before you write the task

Don't write a task parameter from memory. Collection APIs
(community.general, kubernetes.core, freeipa.ansible_freeipa,
ansible.posix, etc.) change across minor versions, and a recalled parameter
name is a guess, not a fact.

## 1. Check what's installed

```bash
ansible --version
ansible-galaxy collection list
```

## 2. Get the real parameter list before writing a task

```bash
ansible-doc <fqcn.module.name>
ansible-doc -t module <module> --json   # machine-readable, good for grep
```

If the module isn't resolvable via `ansible-doc` (uninstalled, or you're
unsure of the fully-qualified name), read the installed source directly
instead of guessing:

```bash
python3 -c "import ansible_collections; print(ansible_collections.__path__)"
find / -path "*/ansible_collections/*/plugins/modules/<module>.py" 2>/dev/null
grep -A 40 "argument_spec" <path-to-module>.py
```

For a module's actual return-value keys (what's safe to reference in a
`register:` + `when:` chain), grep the module's `RETURN` docstring rather
than assuming standard keys exist.

## 3. Validate before running

Always, in order:

```bash
ansible-playbook <playbook>.yml --syntax-check
ansible-lint <playbook>.yml            # if configured in this repo
ansible-playbook <playbook>.yml --check --diff --limit <narrowest target>
```

Read the `--diff` output like a reviewer. If a task reports "changed" but
the diff doesn't show what you expected, the parameter is likely wrong even
though nothing errored. Note `--check` is unreliable for modules that shell
out (`command`, `shell`, some `community.general` modules) — say so and
propose a narrowly-scoped real run instead of trusting `--check` blindly.

## 4. Execute narrow, then verify, then widen

```bash
ansible-playbook <playbook>.yml --limit <one-host-or-small-group>
```

Verify actual state on the target rather than trusting the "ok"/"changed"
summary:

```bash
ansible <host> -m <relevant-fact-or-command-module> -a "..."
```

Re-run the same playbook against the same limited scope — a correctly
written task set reports zero "changed" on the second run. If it doesn't,
fix the idempotency issue before widening `--limit`.

## FreeIPA via Ansible

The `freeipa.ansible_freeipa` collection wraps the `ipa` RPC API. Option
names often mirror `ipa` CLI flags but not always exactly — verify with
`ansible-doc freeipa.ansible_freeipa.<module>` rather than assuming a 1:1
mapping. Most modules are idempotent by design (`state: present/absent`),
but attribute-list options (e.g. group membership) sometimes replace vs.
append depending on the module — check the doc's `action`/`state` semantics.

## Kubernetes via Ansible

Prefer `kubernetes.core.k8s` with `state: present` and a full manifest over
ad-hoc helper modules unless you've confirmed the helper supports the field
you need (`ansible-doc kubernetes.core.k8s`). Cross-check `apiVersion`/`kind`
against what the target cluster actually serves — see the
`kubernetes-verification` skill.