---
name: pulumi-verification
description: Verification-first workflow for writing or debugging Pulumi programs (Python) targeting Kubernetes, Linux hosts (command provider), or other providers. Use this before writing any Pulumi resource whose input schema you haven't already confirmed against the installed provider SDK, and before running pulumi up for real.
---

# Pulumi: verify the provider schema before you write the resource

Provider SDKs (pulumi-kubernetes, pulumi-command, pulumi-tls, etc.) are
generated from schemas that change across versions. A resource/arg name
recalled from memory may not match what's pinned in this project's
`requirements.txt` / `Pulumi.yaml`.

## 1. Check what's pinned and installed

```bash
pulumi version
pip show pulumi pulumi-kubernetes pulumi-command   # or the relevant packages
pulumi plugin ls
```

## 2. Get the real resource schema before writing it

```bash
pulumi package get-schema <provider> > /tmp/schema.json
python3 -c "import json; s=json.load(open('/tmp/schema.json')); print(list(s['resources']['kubernetes:apps/v1:Deployment']['inputProperties'].keys()))"
```

Or introspect the installed Python SDK directly — often faster:

```bash
python3 -c "import pulumi_kubernetes as k8s; import inspect; print(inspect.signature(k8s.apps.v1.Deployment.__init__))"
```

Or grep the installed SDK source for the args class:

```bash
python3 -c "import pulumi_kubernetes, os; print(os.path.dirname(pulumi_kubernetes.__file__))"
grep -rn "class DeploymentArgs" <site-packages-path>/pulumi_kubernetes/
```

For a resource wrapping a Kubernetes object, both the Pulumi input shape
*and* the Kubernetes API shape are moving targets — verify the k8s side too
(see the `kubernetes-verification` skill) and confirm `apiVersion`/`kind`
match what the target cluster actually serves.

## 3. Validate before applying

```bash
pulumi preview --diff
```

Read the diff for the resource you touched line by line. A plan with no
error but an unexpected replace (`+/-`) instead of an in-place update often
means a property is immutable or misspelled — providers don't always
surface an unrecognized property as an error. If a diff looks suspicious,
check actual current state before concluding:

```bash
pulumi stack export --show-secrets=false | python3 -m json.tool | less
pulumi stack output
```

## 4. Execute narrow, then verify, then widen

- Prefer targeted updates while iterating: `pulumi up --target <urn>` on the
  single resource under test, not a full-stack `pulumi up`.
- After apply, verify the actual remote object, not just Pulumi's reported
  success — e.g. `kubectl get -o yaml` the object Pulumi says it created.
- Re-run `pulumi preview` after a successful `up`. No pending changes means
  correct. Immediate drift means the resource args don't match reality yet
  (often a defaulted field you didn't set explicitly) — fix before widening.

## Stack discipline

Confirm which stack you're targeting before every `up`/`destroy`:
`pulumi stack ls`, `pulumi stack select`, `pulumi whoami`. State it out loud.
Treat `pulumi destroy` and `pulumi state delete` as guarded operations —
always get explicit confirmation before running them.
