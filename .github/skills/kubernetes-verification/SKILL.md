---
name: kubernetes-verification
description: Verification-first workflow for writing or debugging raw Kubernetes manifests, CRDs, Helm/Kustomize output, and kubectl operations. Use this before writing any apiVersion, kind, or field you haven't confirmed against the live cluster, and before applying any manifest for real.
---

# Kubernetes: verify the resource schema against the live cluster

`apiVersion`/`kind`/field names drift across Kubernetes versions and, for
CRDs, across operator/chart versions. A recalled manifest shape is a
hypothesis, not a fact.

## 1. Confirm cluster context first — never assume which cluster you're on

```bash
kubectl config current-context
kubectl version
kubectl cluster-info
```

## 2. Confirm the resource exists and its exact API group/version

```bash
kubectl api-resources | grep -i <resource>
kubectl api-versions
```

## 3. Get the real field schema instead of recalling it

```bash
kubectl explain <kind> --recursive
kubectl explain <kind>.spec.<field>
```

This works the same for CRDs once installed — use it instead of assuming a
CRD's shape from a generic example, since operator versions change fields
often:

```bash
kubectl get crd
kubectl explain <crd-kind> --recursive
```

If you need a real, currently-valid example rather than building one from
scratch, dump an existing similar object and adapt it:

```bash
kubectl get <kind> <existing-name> -o yaml
```

## 4. Validate before applying

```bash
kubectl apply --dry-run=client -f <file>.yaml    # fast syntax/schema check
kubectl apply --dry-run=server -f <file>.yaml    # full server-side validation, admission webhooks, CRD validation
```

Prefer `--dry-run=server` whenever the extra round-trip is acceptable — it's
the only one that runs real admission control. For kustomize/helm, render
first and validate the rendered output:

```bash
kustomize build <dir> | kubectl apply --dry-run=server -f -
helm template <release> <chart> | kubectl apply --dry-run=server -f -
```

If available in this repo, also run `kubeconform`/`kubeval` against the
rendered manifest as an extra offline check.

## 5. Execute narrow, then verify, then widen

```bash
kubectl apply -f <file>.yaml -n <narrowest-namespace>
```

Verify actual resulting state, not just "created"/"configured":

```bash
kubectl get <kind> <name> -n <ns> -o yaml
kubectl describe <kind> <name> -n <ns>       # check Events for silent failures
kubectl rollout status <kind>/<name> -n <ns> # Deployments/StatefulSets/DaemonSets
```

Re-apply the same manifest — server-side apply on a well-formed manifest
shows no diff the second time. If it doesn't, something (a defaulted field,
a mutating webhook) is fighting your manifest — investigate before
widening scope.

## Common failure modes to guard against

- A deprecated/removed `apiVersion` — confirm via `kubectl api-versions`
  against *this* cluster's actual version, not a remembered default.
- Assuming a CRD's field shape from a similar-sounding but different
  operator — always `kubectl explain` the specific installed CRD.
- Silent no-ops from mismatched label selectors — after apply, confirm with
  `kubectl get pods -l <selector>` that the selector matches what you expect.
