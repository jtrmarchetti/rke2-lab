=================================================
Ansible patterns: where the design decisions live
=================================================

The conventions (naming, layout, lint) are uniform and boring. The interesting
part of this automation is a small set of **recurring patterns** — the places
where a specific failure, discovered the hard way, became the rule that is in
the code today. Each pattern below names the problem it was built for.

The Flux / Ansible ownership split
==================================

The standing decision, owned by ``plan/FLUX_OWNERSHIP.md``: **Flux owns every
in-cluster object it can reach through the GitOps tree; Ansible covers the
gaps** — bootstrap before GitOps exists, provisioning into external systems
(OpenBao, Keycloak, FreeIPA, Garage), host-level configuration, and
out-of-band recovery.

That split is not a preference; it is what Flux *can do*. The available
primitives were probed on the installed CRDs, not recalled:

* **Kustomization ``dependsOn``** blocks the apply of a KZ until the named
  KZs are Ready — build ordering.
* **Kustomization ``healthChecks``** holds a KZ not-Ready while a named
  in-cluster object is absent; it retries forever and never goes terminal.
* **HelmRelease ``driftDetection``** in ``warn`` mode on all eight releases:
  observational only, never blocks or mutates.

What the CRDs **cannot** do drives the other patterns. A Kustomization's
``healthChecks`` can only see *in-cluster* objects — it cannot observe a
marker that an Ansible role publishes out of band. So:

.. admonition:: Pattern: the marker and the gate
   :class: important

   **Ansible owns the marker, Flux owns the gate.** ``garage_init`` finishes
   by publishing ``ConfigMap garage-buckets-ready`` in the ``garage``
   namespace — deliberately *outside* the Flux tree. The in-tree
   ``garage-ready`` Kustomization holds the ``observability`` Kustomization
   with a ``healthChecks`` readiness check on that marker, so observability
   does not install before its S3 buckets exist. The gate KZ applies an inert
   marker ConfigMap so its build is never empty, and it must **not** set
   ``spec.wait`` — in Flux 2.9.4, ``wait: true`` disables ``healthChecks``.

The marker has two moving parts kept in sync by convention: the marker name
``garage-buckets-ready`` is hardcoded in the in-tree gate **and** in the
``garage_init_marker_name`` default (documented in the role's argument spec).
Rename one and the gate checks for a marker nothing publishes.

The out-of-band recovery path: ``flux_unstall``
===============================================

A HelmRelease that exhausts its remediation retries flips to
``Stalled=True`` — and in helm-controller 1.6.3 that condition is
**terminal**: the controller never retries it, and a plain ``flux resume``
does not clear it. Flux's declarative recovery primitives are not available
in the installed version, so the recovery path lives in Ansible instead:

.. code-block:: text

   detect:  kubectl jsonpath scan for Stalled=True releases
   suspend: each stalled release (resets the retry budget)
   resume:  each release, then wait for the reconciliation

No static settle between the suspend and the resume: the resume's
``--timeout`` and the confirm loop wait on the real reconciliation
result, so a blind sleep would be pure static wait.

``flux_unstall`` is wired into ``cluster_init.yml`` after the Garage
sequence and is an idempotent no-op when nothing is stalled — the same
"safe to re-run" discipline as everything else, applied to a manual
intervention.

The artifact marker scheme
==========================

Staging is idempotent by construction, and the proof that an artifact is
"already there" is a file on disk next to it:
``<dest>.published.<token>``, where the token is the artifact's *source*
(url for files, image ref for images), stripped to ``[A-Za-z0-9._~-]``.

The design decisions inside that, each one bought with a failure:

* **The token is the source, not the destination.** An entry is "published"
  only while a marker for its *current* source exists. Bump a version and
  the source changes, the marker no longer matches, and the refetch
  happens. A destination-only marker would have kept serving the old version
  forever.
* **``force: true`` on marker-gated downloads.** ``get_url``'s default
  Last-Modified heuristic kept a stale local file whenever its mtime beat
  the remote's. It actually happened: a bumped RKE2 version left the old
  image list in place, the new images never published, and the control
  plane crash-looped on ``MANIFEST_UNKNOWN``.
* **Marker files are written with ``copy: content: "" force: false``**, not
  ``file: state: touch`` — a touch bumps the mtime and reports ``changed``
  every run, which breaks the idempotency rule the scheme exists to serve.
* **Three writers, byte-identical markers.** ``artifact_stage`` writes the
  marker after a fetch; ``rke2_publish`` writes it before deleting the
  transit copy; the image-set refetch reads it. If the token computation
  drifts between writers, an artifact looks both published and missing
  depending on which role is asking.

**Check the destination, not the disk** is the rule the marker scheme
implements: retention deletes local transit copies after a push, so a later
publish of the same entry refetches whatever it no longer has on disk,
gated on the source marker as above. The confirmation that an entry is done
is the *published* set — the images, sets, charts and packages the run
actually pushed — not the leftover files in the staging directory.

Tag-filtered runs: what the gotchas look like
=============================================

The patterns interact with the ``--tags`` habit, in the two ways that have
already cost runs:

* ``gitops.yml`` **cannot** be tag-filtered to ``--tags gitops_source`` — the
  "re-read the OpenBao unseal keys" task is deliberately untagged, and the
  ``include_role`` below it names the fact it sets in its ``vars:`` block.
  Skip the re-read and the include fails at task finalisation with
  ``'gitops_openbao_unseal_keys' is undefined`` — a hard error, so the run at
  least cannot be mistaken for a complete one; the point remains that the tag
  filter did not select a self-contained task set.
* ``cluster_init.yml --tags garage_init`` pulls the OpenBao second-pass
  secrets task, which needs the root token produced under the
  ``openbao_init`` tag. For warm clusters, the working move is to publish
  the marker ConfigMap by hand instead of tag-filtering.

The rule the two failures distill into: *a tag filter selects tasks, not a
self-contained set of work* — an unselected prerequisite and the fact it
sets, or a dragged-in task that needs a skipped task's output, still gets
exercised by the run. Run with the widest tag that is still narrow
enough, and read what actually runs before believing the tag.

Where the pattern record lives
==============================

The durable ownership record is ``plan/FLUX_OWNERSHIP.md`` (the split and
the garage gate); the failure histories are in the ``plan/PHASE<N>``
documents' "what the run taught" sections. When a pattern here is updated,
the trigger table in :doc:`../reference/maintaining-this-guide` — the rows
for Ansible role/playbook changes and for Ansible pattern changes — says
which of this page's sections the new fact belongs in, alongside the
established rows for design-decision and automation-flow changes.
