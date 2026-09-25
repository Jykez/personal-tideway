# Migration 5E live acceptance

**Date:** 2026-09-24
**Baseline:** `efbc062afd46b63d759fcc575bc0d26eedc4d6dd`
**Result:** `ACCEPT` for the fresh-install/live-handoff path

## Scope

Migration 5E closes the live acceptance gap left after Migration 5D. The
registered repository was already on schema v2, so `ptw migrate plan --json`
returned `status=not_required`; no legacy apply or rollback was appropriate.
Acceptance therefore exercised the approved fresh-install path: backup,
installed-client convergence, native trust review, and a real
Codex-to-agy-to-Codex model handoff.

## Pre-mutation backup

The restricted backup directory is:

`~/.personal-tideway/backups/migration-5e-live-20260924T162006+0300`

- `pre-live.tar` contains the pre-change Codex config, Codex hooks file,
  canonical continuity skill, and installed-tool receipt. SHA-256:
  `050197c283cae03ecce33508d9b137a2a3c577532f1b839361bdff5e73a3fabd`.
- `pre-sync-state.tar` contains the pre-sync `state.json`. SHA-256:
  `f990f4684e812a3b21bcfa0ae719be4e7da67d8218bca036e818cbd619701b9b`.
- The directory mode is `0700`; archive modes are `0600`.

## Findings and corrections

The first live probes exposed two gaps that structural status had missed:

1. Codex ignored the canonical `hooks.json` while `features.hooks` was absent
   from `config.toml`. The installer now enables the runtime feature while
   preserving unrelated TOML, and status reports a canonical-but-disabled hook
   as `not-installed` rather than installed.
2. Codex rejected the managed `continuity` skill because its `SKILL.md` lacked
   YAML frontmatter. Fresh templates now include `name` and `description`.
   `ptw init` upgrades only the byte-exact legacy managed template and preserves
   every user-modified variant.

Live convergence changed only the expected content:

- added `hooks = true` below the existing `[features]` table;
- prepended valid managed YAML frontmatter to the canonical continuity skill;
- updated Tideway's skill fingerprint state after both clients observed the
  same canonical change;
- left `~/.codex/hooks.json` byte-identical.

## Behavioral evidence

The prompt for each positive probe omitted the expected marker and prohibited
tool calls, file reads, and shell commands.

### Codex to agy

- Codex-authored checkpoint marker:
  `PTW_5E_CODEX_TO_AGY_20260924_C4F8`.
- A first agy print-mode run intentionally omitted a workspace and failed
  closed because `workspacePaths` was empty.
- A fresh run with the project supplied through agy's supported `--add-dir`
  option loaded the `PreInvocation` hook and returned the exact hidden marker.
- agy conversation evidence:
  `6b65d4de-c657-4ba8-877e-1c7b0d95bd70`.

### agy to Codex

- agy generated return marker: `PTW_5E_AGY_TO_CODEX_E8F90123`.
- Codex's native TUI displayed the exact `SessionStart` event, matcher,
  command, timeout, source file, and context limit before trust was granted.
- Codex computed and persisted its own trust digest; Tideway did not forge it
  and no bypass flag was used.
- After live convergence, a normal fresh Codex invocation without
  `--enable hooks` recovered the exact hidden agy marker.
- Final Codex thread evidence:
  `01a0d394-1d0a-7321-8bec-053a36ac6860`.

## Verification

- full automated suite: `684 passed`;
- focused Ruff: passed;
- focused mypy with imports isolated: passed;
- `git diff --check`: passed;
- `uv build`: passed;
- installed candidate wheel SHA-256 before the final version bump:
  `c4935332fceedc4e915ff662293b433a44f3aa042c492e125ab4ded7c3343f75`;
- final installed `0.1.0.dev13` wheel SHA-256:
  `bc4a7fd715d888b416e05915bf91e3e46cc2bf3c685feb26d37c6db2bee92563`;
- `ptw sync --dry-run`: no changes;
- projection parity: `in_sync` for Codex and agy;
- both lifecycle hook definitions: `installed`.

## Assurance boundary

The live handoff is behaviorally verified, but the current evaluator does not
yet persist active probe time, installed/enabled state, native trust digest,
and marker evidence as a durable machine-readable record. It therefore remains
truthful for `ptw status` to report `instructed` with `hook_verified=false`.
Migration 5E does not claim the automated `hooked` level. Durable behavioral
evidence is the next bounded agent-integration capability.

### Follow-up candidate record (2026-09-25, uncommitted)

The follow-up implementation can store a bounded local candidate record per
client in `state/behavioral-evidence/`. It records the probe time, SHA-256
digests of a marker and proposed client/trust traces, and a fingerprint of the
exact hook and runtime configuration bytes. A local HMAC seal, restrictive file
permissions, a 30-day expiry, and path checks reject damaged, stale, changed,
or cross-client records. Raw markers and transcripts are not stored.

This seal only protects local record integrity. The current code does not yet
verify native Codex/agy traces, prove that a hidden marker reached the model,
or establish that the client's own trust review accepted the hook. The record
writer is deliberately internal and does not promote assurance. `ptw status`
therefore remains `instructed` and `hook_verified=false` even with a current
candidate. A future verifier must correlate a fresh challenge with native
client output and native trust evidence before promotion can be considered.

## Verdict

**ACCEPT.** The schema-v2 fresh-install path, live client convergence, native
trust boundary, and real Codex-to-agy-to-Codex context handoff are verified.
Migration is complete for this installation because no legacy state existed to
migrate. Public-alpha packaging and durable automatic hook assurance remain
separate future work.
