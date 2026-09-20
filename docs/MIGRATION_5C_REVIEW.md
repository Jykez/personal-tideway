# Migration 5C review record

## Scope

Migration 5C adds the public `ptw migrate rollback` command, bundle-ID and
apply-reported manifest-path resolution, a non-mutating rollback dry-run,
sanitized human/JSON reporting, and generated v1 apply-to-rollback fixtures.
Live client configuration and live migration were outside this review.

## Independent Agy review

- Conversation: `1a02a4b1-58a0-4a9e-8615-96f7c8df811e`
- Mode: fresh read-only review of the complete candidate diff and test evidence
- Verdict: `ACCEPT`
- Findings: no P0, P1, P2, or P3 findings

The review assessed CLI/API compatibility, dry-run semantics, path
normalization, error contracts, test quality, documentation accuracy, and
scope boundaries.

## Independent Agy adversarial review

- Conversation: `ed26c7ff-3214-4c55-95b2-c6121bf8561b`
- Mode: fresh adversarial review with attacker-controlled CLI arguments,
  manifests, backup contents, paths, and timing
- Verdict: `ACCEPT` with one P2 fidelity issue and two P3 hardening findings

Findings and Codex disposition:

1. **P2 — incomplete dry-run `preserved_memory`: accepted and fixed.** The
   preview now reports new central-memory files and predicts collision-safe
   `.post_migration` destinations without mutating the workspace.
2. **P3 — malformed entries on an already rolled-back manifest: accepted and
   fixed.** Structural entry validation now precedes the idempotent return,
   while retained backup payloads are not required for a repeated rollback.
3. **P3 — unchecked `destinations_created` report data: accepted and fixed.**
   The field and every returned path are validated before reporting.

Codex also tightened manifest field types for `symbolic_id`, `root_key`,
`rel_path`, `existed_before`, `entry_type`, mode, and file hashes, and added
regression coverage for the corrected boundaries.

## Agy correction review

- Conversation: `81823825-2aa3-4c98-be48-cbf4f8c6224d`
- Mode: fresh read-only review of the cumulative corrected candidate
- Verdict: `ACCEPT`
- Result: all previous findings `FIXED`; no new P0–P3 regressions

## Codex verification boundary

Agy reviewed supplied diffs and evidence without workspace or tool access.
Codex remained the integration owner and independently ran focused and full
tests, Ruff delta checks, `git diff --check`, CLI help, and package build.

This record does not claim a live migration, a live rollback, or resistance to
an already-authorized same-user process racing arbitrary filesystem changes.
