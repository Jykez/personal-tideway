# Migration 5D live-migration readiness audit

Audit date: 2026-09-20 (Europe/Moscow)

## Scope and safety boundary

This stage prepares a reproducible live-migration runbook. It does not authorize
or perform migration apply, rollback, client synchronization, hook installation,
project registration, or any write to live Personal Tideway, Codex, or agy
configuration.

The audit used repository sources, path metadata, the sanitized migration-plan
output, and generated test fixtures. It did not inspect or print OAuth stores,
authentication files, secret values, or private memory. Agy was not used.

## Source baseline

The following was verified before the audit:

- worktree: clean;
- branch: `main`;
- `HEAD`: `6ff2e48199679f5d54614fbc08f8c40964e01379`;
- local `origin/main`: the same SHA;
- remote `refs/heads/main`, queried without updating local refs: the same SHA;
- baseline repository CLI version: `ptw 0.1.0.dev10`;
- initial Migration 5D candidate version: `ptw 0.1.0.dev11`;
- corrected runtime-registration candidate version: `ptw 0.1.0.dev12`;
- Migration 5C explicitly did not claim live migration or live rollback.

The three controlling sources for this audit are `ROADMAP.md`,
`docs/PERSONAL_TIDEWAY_V2_SPEC.md`, and `docs/MIGRATION_5C_REVIEW.md`.

## Effective live paths

No `PERSONAL_TIDEWAY_HOME`, `CODEX_HOME`, `GEMINI_HOME`, or `AGY_HOME`
environment override was set. The effective paths were therefore the default
paths below. Tildes intentionally replace the audited account's absolute home
path so that this record remains safe to publish.

| Surface | Effective path | Verified metadata state at audit start |
|---|---|---|
| Personal Tideway home | `~/.personal-tideway` | absent |
| Personal Tideway config | `~/.personal-tideway/config.yaml` | absent |
| Legacy Personal Tideway memory | `~/.personal-tideway/memory` | absent |
| Personal Tideway migration backups | `~/.personal-tideway/backups` | absent |
| Codex home | `~/.codex` | directory, mode `0755`, not a symlink |
| Codex config | `~/.codex/config.toml` | regular file, mode `0600`, not a symlink |
| Codex rules | `~/.codex/AGENTS.md` | regular file, mode `0644`, not a symlink |
| Codex hooks | `~/.codex/hooks.json` | absent |
| Codex skills | `~/.codex/skills` | directory, mode `0755`, not a symlink |
| agy home | `~/.gemini` | directory, mode `0755`, not a symlink |
| Legacy agy rules | `~/.gemini/GEMINI.md` | regular file, mode `0644`, not a symlink |
| Legacy agy skills | `~/.gemini/skills` | directory, mode `0755`, not a symlink |
| Current agy config | `~/.gemini/config/mcp_config.json` | regular file, mode `0644`, not a symlink |
| Current agy rules | `~/.gemini/config/GEMINI.md` | absent |
| Current agy skills | `~/.gemini/config/skills` | absent |
| Current agy hooks | `~/.gemini/config/hooks.json` | absent |

There is no globally discoverable `ptw` executable. The audited CLI is run from
this repository through `uv run --frozen ptw`. The detected client versions are
Codex CLI `0.155.0` and agy `1.2.7`.

## Read-only evidence

The live plan was run with explicit roots so that environment precedence could
not change the audit target:

```bash
uv run --frozen ptw \
  --home "$HOME/.personal-tideway" \
  --codex-home "$HOME/.codex" \
  --gemini-home "$HOME/.gemini" \
  migrate plan --json
```

Verified result:

- `status`: `blocked`;
- `source_schema_version`: `null`;
- `target_schema_version`: `2`;
- blocker: `Ambiguous layout: legacy artifacts present but config.yaml is missing`;
- actions, backups, legacy paths, preservation entries, and canonical MCP
  entries: empty;
- process exit code: `0`;
- target-path metadata before and after the command: identical.

The zero exit code is not an acceptance signal. Every runbook gate must parse
the JSON document and require `status == "ready"`.

The live `migrate apply --dry-run` was deliberately not run. Before it reaches
the blocked plan, the current implementation captures content hashes for client
configs and legacy skill trees. That is non-mutating, but it reads their bytes
and therefore exceeds this audit's stricter no-secret-reading boundary. The
same dry-run behavior was instead exercised against generated temporary
fixtures:

```text
tests/test_migration_plan.py
tests/test_migration_apply.py
tests/test_migration_rollback.py
tests/test_migration_5c.py
```

Initial result: `92 passed`.

## Readiness verdict

**MIGRATION NOT REQUIRED. USE THE FRESH-INSTALL PATH AFTER EXPLICIT APPROVAL.**

The initial planner result was a false positive. The machine has no identifiable
v1 Personal Tideway root or config. A bounded provenance check established,
without printing content or artifact names, that the existing agy rules contain
no Personal Tideway managed markers and that the two legacy-root skill entries
are valid external links whose targets exist outside Personal Tideway.

The planner was corrected to distinguish explicit Personal Tideway provenance
from ordinary client artifacts when `config.yaml` is absent:

- valid managed rule markers, managed skill markers, or links into the expected
  Personal Tideway root still fail closed as a lost-v1-config recovery case;
- malformed or unsafe ownership evidence remains blocked;
- ordinary rules, directories, files, and external skill links are reported as
  unmanaged preservation items and do not become migration inputs;
- no symlink is followed during provenance classification;
- bounded reads are used only for exact ownership-marker detection, and no
  content is returned by the plan.

After the correction, the live plan reports `status=not_required`, no blockers,
no actions, no backups, and preservation of `agy:unmanaged_rules` and
`agy:unmanaged_skills`. Target metadata remained identical. Therefore live
`migrate apply` and `migrate apply --dry-run` must not be run. Fresh
initialization was the correct next product operation and was performed only
after separate explicit approval, as recorded below.

## Fresh-install preview

The repository previously documented `ptw init --dry-run` but did not expose
the flag. This audit adds a deterministic preview that runs the same path and
type preflight as `ptw init`, then returns symbolic create/permission actions
before the mutation phase.

The live preview was executed with explicit PTW, Codex, and agy roots. It
reported 21 central directories and 6 central files to create exclusively
below `~/.personal-tideway`. Target metadata before and after the command was
identical, and the Personal Tideway root remained absent. No Codex or agy
projection is part of init.

The first implementation exposed a second readiness gap: it previewed and
created only the central file tree, even though the v2 first-run contract also
requires the pinned isolated Basic Memory environment. The corrected init flow
now:

- resolves or accepts an explicit absolute `uv` executable;
- validates both workspace and Basic Memory plans before the first write;
- includes the pinned `basic-memory==0.23.2` install and health-check actions in
  `ptw init --dry-run`;
- provisions and verifies Basic Memory during an approved non-dry init;
- retains zero filesystem mutation and zero subprocess calls during dry-run;
- never projects changes into Codex or agy as part of init.

## Approved initialization and current live state

After explicit approval, the original central-only `ptw init` completed with
exit code zero. It created the v2 workspace below `~/.personal-tideway`, with
`secrets.env` mode `0600`, but the missing installer integration meant that the
Basic Memory runtime was still absent. No Codex or agy file metadata changed.

Read-only post-init checks established:

- workspace schema v2 is initialized;
- one bundled shared skill exists and no memory entries exist;
- Basic Memory is unavailable because its isolated executable is absent;
- Codex and agy continuity assurance and lifecycle hooks are not installed;
- both client rule projections are uninitialized;
- `ptw sync --dry-run` predicts only managed rules-block initialization for
  Codex and agy and leaves client metadata unchanged.

After correcting the init contract, a second live `ptw init --dry-run` was run
against the initialized workspace. It reported no central workspace changes,
the exact isolated Basic Memory directories/config, pinned install command, and
health check. Personal Tideway metadata was identical before and after. The
real Basic Memory installer and real client sync remained separate write gates.

After a further explicit approval, the corrected `ptw init` installed and
health-checked Basic Memory `0.23.2` successfully. The service root and bootstrap
config have modes `0700` and `0600`; status reports `backend_usable=true`.
Metadata for the predetermined Codex and agy config/rules/hooks/skills targets
shows no installation-time change. A broad whole-home metadata hash did change
while Codex was active, so it is intentionally recorded as runtime noise rather
than used as client-config evidence. Real client sync, hook installation, and
project registration remain unexecuted gates.

The first post-install sync preview exposed and blocked obsolete routing before
apply: default agy rules and skills still pointed at the legacy root. The fix
routes managed agy rules to `~/.gemini/config/GEMINI.md` and managed skills to
`~/.gemini/config/skills`, while preserving `~/.gemini/GEMINI.md` and
`~/.gemini/skills` as unmanaged legacy artifacts. First sync no longer imports
unrelated client-only skills into Personal Tideway, and its dry-run now reports
each managed skill projection. Project registration now also reconciles the
registry into Basic Memory configuration so a successfully added project is not
left without its memory binding.

The corrected live `sync --dry-run` predicts exactly four client operations:
managed rules initialization for Codex and agy, plus the `continuity` managed
skill projection for each client. The predetermined target metadata and central
sync state remained unchanged. This preview is accepted as read-only evidence,
not authorization for real sync.

A final first-run gap was found before hooks or sync were approved: the healthy
backend had no canonical MCP object, although the specification requires the
same local Basic Memory MCP server for both clients. Init now builds a
core-managed `basic-memory` stdio definition using the isolated executable,
explicit `mcp --transport stdio` arguments, the isolated config directory, and
disabled auto-update/promotions. It refuses to overwrite a divergent existing
definition. The live init dry-run predicts creation of only
`~/.personal-tideway/mcp/basic-memory.yaml`; that central write has not yet run,
so the accepted client sync preview must be refreshed after it is approved.

The final candidate also rejects a pre-existing linked, special, oversized, or
malformed `basic-memory.yaml` without following it or returning its content.
Canonical init files with multiple hard links are rejected before chmod or any
write. Repeated init no longer chmods already-converged `secrets.env` or the
Basic Memory lock file, so the healthy/no-op path does not create metadata
noise before the missing MCP definition is written.

## Read-only preview of the next approved setup gate

Operations A-C below have **not** been executed. Their current read-only
preview is:

1. Re-run `ptw init`. The workspace portion reports `no_changes`; the isolated
   executable reports Basic Memory `0.23.2`; the installer will skip reinstall
   after that health check. The only missing canonical artifact is
   `~/.personal-tideway/mcp/basic-memory.yaml`. Codex and agy targets are not
   part of init.
2. Install the verified `0.1.0.dev11` wheel as the uv tool
   `personal-tideway`. The currently absent paths that will be created are
   `~/.local/share/uv/tools/personal-tideway` and `~/.local/bin/ptw`.
3. Register the current Git repository. Read-only resolution confirms slug
   `gpt-gemini`, display name `GPT + Gemini`, and normalized remote
   `github.com/Jykez/personal-tideway`. The operation will modify only
   `~/.personal-tideway/registry/projects.yaml` and
   `~/.personal-tideway/services/basic-memory/config/config.json`, normalize
   `~/.personal-tideway/projects` from mode `0755` to `0700`, and create
   `~/.personal-tideway/projects/<generated-project-uuid>/memory`. The UUID and
   the derived Basic Memory name are generated atomically at apply time; no
   source-repository file is part of the plan.

Before A-C, retain byte-for-byte backups of the two existing files changed by
C (`projects.yaml` and Basic Memory `config.json`) in a new restricted central
setup-backup directory, plus a manifest recording that the MCP definition, uv
tool, launcher, and project memory root were initially absent. Recovery is:
restore the two files atomically, remove only the manifest-recorded paths that
were absent before A-C and still match the created artifacts, then re-run the
read-only health, registry, binding, status, doctor, and sync previews. Do not
delete a non-empty project memory root or overwrite a launcher/tool path that
has drifted.

## Complete backup set for a future approved apply

The exact set must come from a new successful `migrate plan --json` and a
matching `migrate apply --dry-run --json`. The following is the required
upper-bound inventory; conditional entries are included only when present and
listed by the accepted plan.

| Backup item | Symbolic identity | Requirement |
|---|---|---|
| Complete v1 config | `ptw:config_yaml` | Mandatory for a v1 migration |
| Legacy manual memory tree | `ptw:memory_dir` | Conditional; preserve bytes, tree structure, and modes without exposing content |
| Legacy agy rules file | `agy:legacy_gemini_md` | Conditional; only after managed-block ownership is proven |
| Legacy agy skills tree | `agy:legacy_skills` | Conditional; only after link/file ownership is proven |
| Current agy MCP config | `agy:config` | Conditional when the plan lists it as a changed client file |
| Codex config | `codex:config` | Conditional when the plan lists it as a changed client file |
| Codex rules | `codex:rules` | Conditional when the plan lists it as a changed client file |
| Every pre-existing target that apply will change | manifest entry with `existed_before=true` | Back up bytes/tree and modes before the first mutation |
| Every initially absent destination | manifest entry with `existed_before=false` | Record absence so rollback removes only migration-created paths |
| Transaction evidence | bundle manifest and entry hashes | Must reach `completed` and remain retained after acceptance |

The transaction bundle belongs below
`~/.personal-tideway/backups/migrations/<bundle-id>/` with mode-restricted
entries and a manifest. Hooks are not in the current migration mutation set and
must not be inferred as backed up. Any later `sync` or hook operation requires
its own preview and backup gate.

Before live approval, compare the accepted dry-run backup list with this table.
An omitted existing target, a target without a restorable mode/hash record, or
a plan/dry-run mismatch is a stop condition.

## Acceptance matrix

| Phase | Gate | Required evidence | Failure action |
|---|---|---|---|
| Pre-apply | Immutable source baseline | Clean repository at the approved SHA; exact CLI version recorded | Stop; do not substitute another checkout or executable |
| Pre-apply | Unambiguous roots | Explicit PTW/Codex/agy roots; no symlink component, traversal, non-regular root, or unexpected override | Stop and investigate read-only |
| Pre-apply | Migration identity | `migrate plan --json` returns `status=ready`, schema v1, expected legacy paths, preservation set, and no blocker | Stop; current live state fails this gate |
| Pre-apply | Privacy | No OAuth/auth store is opened, copied, logged, or delegated; reports contain symbolic IDs, not values | Stop and discard unsafe evidence |
| Pre-apply | Dry-run parity | `migrate apply --dry-run --json` is metadata-stable and exactly matches the accepted plan's targets/actions/backups | Stop on any drift or unexpected read/write surface |
| Pre-apply | Backup capacity and quiescence | Sufficient space for the complete bundle plus margin; Codex/agy processes cannot concurrently modify target files | Stop; the implementation does not claim protection from arbitrary same-user races |
| Apply | Backup before mutation | Bundle and manifest created; every existing target copied and hash-verified; modes recorded | Automatic stop before mutation; retain diagnostics, not partial evidence |
| Apply | Transaction result | `status=applied`, non-empty bundle ID, manifest `state=completed`, no blocker/error, exact expected mutations only | Stop further setup and enter rollback decision gate |
| Apply | Idempotence | Immediate read-only plan reports `not_required`; a repeated dry-run predicts no new migration mutations | Stop and preserve the bundle |
| Smoke | Structural health | `ptw status --json` and `ptw doctor --json` report the expected root, valid config, no unresolved migration/conflict/secret/projection error | Roll back before any later sync or real-project registration |
| Smoke | No repository pollution | Disposable repository remains byte/status clean; no `.personal-tideway.yaml` or memory is created there | Roll back and treat as a release blocker |
| Smoke | Client projections | Only after separate preview/approval: Codex and agy parse the intended rules, skills, and MCP projections; unmanaged config remains semantically unchanged | Roll back the responsible operation; do not claim behavioral success |
| Smoke | Continuity behavior | Only with explicit model/quota approval: disposable Codex-to-agy and agy-to-Codex sentinel handoffs pass after restart | Do not register real projects; preserve truthful partial assurance |
| Rollback | Preview validation | Public rollback dry-run validates the exact retained bundle, entry structure, paths, hashes, destinations, and preserved memory report | Stop; never attempt ad-hoc bulk restoration |
| Rollback | Restoration | Actual rollback restores original bytes/modes/links and removes only initially absent migration-created paths | Freeze further writes and recover from the retained bundle entry-by-entry after review |
| Rollback | Post-check | Pre-migration metadata/hash inventory matches; new post-migration central memory is preserved/reported; repeated rollback is idempotent | Keep the bundle and record the mismatch as a blocker |

## Stop conditions

Any one of the following blocks live apply:

1. dirty Git worktree, unapproved SHA, or local/remote baseline mismatch;
2. for a v1 migration, plan JSON status other than `ready`, regardless of
   process exit code; `not_required` routes to fresh install and must never be
   passed to migration apply;
3. missing or malformed v1 config, unknown schema, ambiguous ownership, or
   conflicts between legacy and current destinations;
4. changed environment overrides or a path differing from the reviewed roots;
5. symlink components, traversal, hard links where prohibited, special files,
   unsafe permissions, or unresolved duplicate artifacts;
6. dry-run metadata drift, plan/dry-run mismatch, or any live timestamp/state
   mutation;
7. incomplete backup inventory, insufficient capacity, backup hash failure, or
   inability to retain the bundle outside later cleanup;
8. concurrent client activity or source drift between plan and locked apply;
9. any OAuth/auth/private-memory content appearing in output, logs, diffs, or
   fixtures;
10. apply result other than `applied`, missing rollback evidence, failed
    structural smoke checks, or unexpected repository pollution;
11. rollback dry-run failure or a manifest/bundle that cannot be validated.

## Recovery commands for an approved future transaction

Use the same reviewed repository checkout and the exact bundle ID printed by
apply. First validate without mutation:

```bash
uv run --frozen ptw \
  --home "$HOME/.personal-tideway" \
  --codex-home "$HOME/.codex" \
  --gemini-home "$HOME/.gemini" \
  migrate rollback '<exact-bundle-id>' --dry-run --json
```

Only after that JSON result is reviewed and rollback is explicitly approved:

```bash
uv run --frozen ptw \
  --home "$HOME/.personal-tideway" \
  --codex-home "$HOME/.codex" \
  --gemini-home "$HOME/.gemini" \
  migrate rollback '<exact-bundle-id>' --json
```

Then run read-only verification:

```bash
uv run --frozen ptw \
  --home "$HOME/.personal-tideway" \
  --codex-home "$HOME/.codex" \
  --gemini-home "$HOME/.gemini" \
  migrate plan --json
```

If public rollback validation fails, do not run a second mutating command and do
not use recursive copy/remove commands. Preserve the complete bundle, manifest,
and current target metadata; restore individual manifest entries only after a
separate reviewed recovery plan.

## Current acceptance evidence

### Verified live state

- `HEAD`, local `origin/main`, and remote `origin/main` remain
  `6ff2e48199679f5d54614fbc08f8c40964e01379`; the reviewed candidate is an
  intentionally uncommitted worktree patch on that baseline;
- the isolated executable health check returns Basic Memory `0.23.2`, and the
  structural assurance check reports `backend_usable=true`;
- the current v2 migration plan is `not_required`, with no action, backup, or
  blocker;
- `ptw init --dry-run` reports no workspace changes and exactly one missing
  canonical object, `~/.personal-tideway/mcp/basic-memory.yaml`;
- the full Personal Tideway tree plus the predetermined Codex/agy target
  metadata were byte-identical as metadata snapshots before and after that
  preview;
- the current `sync --dry-run` reports four operations only: Codex and agy
  managed rules plus the `continuity` skill for each client. It is metadata
  stable. This preview predates the missing Basic Memory MCP object and must be
  refreshed after A;
- legacy `~/.gemini/GEMINI.md` and `~/.gemini/skills` remain present and are not
  active projection targets; active agy targets are under `~/.gemini/config`;
- no global `ptw` is currently discoverable; uv reports tool root
  `~/.local/share/uv/tools` and launcher root `~/.local/bin`;
- read-only project resolution confirms the candidate identity documented in
  the previous section;
- no OAuth store, secret value, or private memory was opened or printed during
  this final verification.

### Fixture and static verification evidence

- focused regression set: `135 passed` before the final type-only corrections;
- full suite after the final candidate changes: `666 passed`;
- focused mypy over all affected production modules: no issues in 8 files;
- changed-Python-file Ruff comparison: 45 findings versus 64 on the clean
  baseline, with no new finding category or per-file regression;
- `git diff --check`: pass;
- `uv build`: pass, producing the `0.1.0.dev11` wheel and source distribution;
- generated fixtures cover zero-mutation init preview, unsafe MCP symlink and
  hardlink rejection, canonical-file hardlink rejection, current agy routing,
  preservation of unmanaged skills, project-to-Basic-Memory reconciliation,
  and explicit managed-skill preview output.

### Assumptions and limitations

- the generated project UUID cannot be known before C without persisting a
  reservation; the preview therefore identifies that single dynamic path
  component explicitly rather than pretending it is fixed;
- no guarantee is made against an arbitrary same-user process racing files
  between the accepted preview and a later approved write;
- sufficient disk space and client quiescence must be checked immediately
  before A-C;
- structural `backend_usable=true` is not itself behavioral MCP proof; the
  separate exact-version process health check is the runtime evidence available
  before client projection;
- historical statements earlier in this document describe the initial absent
  workspace and the already-approved Basic Memory installation chronologically;
  this section is the current-state acceptance record.

### Live A-C execution result

- A completed from the reviewed source tree. Repeated real init recognized the
  healthy isolated Basic Memory `0.23.2` installation and created only the
  canonical central `mcp/basic-memory.yaml`; a second init left the monitored
  Personal Tideway metadata hash unchanged.
- B completed from the verified wheel with SHA-256
  `0295dfee7a5687375ec7b78e087ddc0524d108d4878f46628da7d4c9e80bb16f`.
  The global launchers are `ptw` and the package-provided `ptw-mcp-run`, and
  `ptw --version` reports `0.1.0.dev11`.
- C registered the repository as `gpt-gemini`, project UUID
  `db8e082a-671b-4fb6-9c24-b917d4d31656`, and Basic Memory project
  `ptw-gpt-gemini-db8e082a`. Registry resolution/list/show and the generated
  path binding are correct; the empty memory root is mode `0700`.
- The first behavioral `context show` exposed a live-only readiness gap:
  configuration reconciliation did not register the project in Basic Memory's
  runtime database. The canonical explicit per-project
  `reindex --search --project ptw-gpt-gemini-db8e082a` completed successfully;
  Basic Memory status then reported zero files and `context show` returned an
  empty, non-truncated bundle. This reindex changed disposable Basic Memory
  runtime state (`memory.db`, `.bmignore`, and the log), which was not included
  in the two-file C mutation preview or its pre-C backup. The A-C mutation
  forecast is therefore not accepted as exact even though the intended live
  behavior now passes.
- Post-A-C `status`, `doctor`, and `sync --dry-run` all exited successfully.
  Status and doctor retain expected warnings for unprojected/legacy client
  state; continuity is `manual`, not `hooked`. The refreshed sync preview
  remains exactly four client operations: Codex and agy rules initialization
  plus `continuity` skill projection for both clients. Hashes of the registry,
  Basic Memory config, canonical MCP object, and predetermined client config
  files, plus source `git status`, were unchanged across these read-only
  commands.

### Remaining live gates

- real sync of MCP/rules/skills and central `state.json` (separate approval);
- hook installation and structural checks (separate approval after sync);
- real Codex and agy behavioral hook probes; no `hooked` claim is made;
- live migration apply/rollback, which are not required for the current v2
  workspace and must not be run merely as a smoke test.

### Dev12 runtime-registration correction

The `0.1.0.dev12` correction makes runtime initialization part of both
`project add` and `project add-external`. A successful command now requires the
canonical per-project reindex and bounded status verification to succeed; the
previous state in which the registry/config were written while Basic Memory
still returned `Project not found` is no longer reported as success.

Before any registration mutation, Personal Tideway captures bounded,
no-follow, single-link snapshots of the exact registry and Basic Memory config
bytes. If initialization of a newly created project fails, rollback:

1. removes only that runtime binding with Basic Memory's supported
   `project remove NAME --local` command and never passes `--delete-notes`;
2. restores the exact source-of-truth bytes and original modes atomically;
3. restores the caller's in-memory registry and removes only newly created
   empty central memory directories;
4. reports a distinct incomplete-rollback error if runtime cleanup or any
   restoration step fails.

For a failed existing Git-binding update, the previous registry/config are
restored and their runtime binding is reinitialized; an unchanged existing
registration is never destructively rolled back. The SQLite database and logs
remain explicitly disposable derived runtime state, while Markdown notes are
preserved.

Verification after the correction:

- full suite: `681 passed`, including success, complete rollback, cleanup
  failure, previous-binding restoration, already-absent runtime cleanup,
  non-destructive argv, invalid-timeout, and CLI runner-propagation cases;
- focused mypy: no issues in the four affected production modules;
- new/affected runtime modules: Ruff clean; `git diff --check`: pass;
- `uv build`: pass; wheel SHA-256
  `b493f5d0a49723e3766afbb125c1deaafe77eff949cbd2a86bbf4cf20948805f`;
- restricted recovery bundle:
  `~/.personal-tideway/backups/runtime-fix-20260923T204030+0300`;
- the verified wheel is installed globally and `ptw --version` reports
  `0.1.0.dev12`;
- repeating the real repository registration exited successfully without the
  former manual reindex, reported the existing `gpt-gemini` project, and left
  registry, Basic Memory config, canonical MCP, predetermined client configs,
  and source `git status` byte-stable;
- Basic Memory status reports zero files; `context show` returns the expected
  empty non-truncated bundle; `status`, `doctor`, and `sync --dry-run` all exit
  successfully; the sync preview remains the same four unexecuted client
  operations.

Real client sync and hook installation remain outside 5D and were not run.

### Independent dev12 reviews

- ordinary independent AGY review
  `2c9ac2ed-db36-45d4-bebb-8479153c5e45`: `ACCEPT`, no findings;
- adversarial AGY review
  `ae20af22-a10b-4971-82a0-eb34d3bb5f61`:
  `RETURN_FOR_CORRECTION` with one accepted hardening item and several findings
  that were already covered by the explicit fail-closed contract or did not
  match the supported external-registration API.

The accepted item is corrected: failure to remove a newly created runtime
project now prevents deletion of its memory root, preserving the recovery
target while the command reports incomplete rollback. Removal of a newly
created source-of-truth file now uses no-follow parent/file descriptors,
regular-file and link-count checks, and inode identity verification before
unlink. A regression fixture confirms that non-empty note roots remain intact
even after successful runtime cleanup. The final suite after these changes is
the `681 passed` result above.

Final independent adversarial re-review
`516e6bac-14ab-4aa2-aba7-6a5f4cd6226d` returned `ACCEPT` with no remaining
P0-P2 findings. It explicitly verified the preserved recovery root after
runtime-cleanup failure, no-follow descriptor and inode checks, non-destructive
Basic Memory removal, previous-binding restoration, correct external-project
no-op handling, and the final live/static evidence.

## Final Migration 5D verdict

**ACCEPT.** The dev12 implementation, rollback behavior, built artifact, and
installed live behavior satisfy the Migration 5D readiness gates. This verdict
authorizes no further mutation: real client sync, hook installation and
behavioral hook probes remain separate future approval gates. The reviewed
worktree remains intentionally uncommitted on baseline
`6ff2e48199679f5d54614fbc08f8c40964e01379`.

## Post-acceptance live execution

After the user separately approved real client sync, hook installation, commit,
and push, the approved live operations were executed on 2026-09-23 (MSK).

- A restricted pre-mutation backup was created at
  `~/.personal-tideway/backups/client-sync-hooks-20260923T214837+0300`.
  It records both existing and absent predetermined targets and contains a
  mode-preserving tar snapshot of the existing targets.
- `ptw sync` initialized the managed rules block and continuity skill for both
  Codex and agy. A subsequent `ptw sync --dry-run` reported no changes.
- `ptw status` reported no pending changes or conflicts, portable MCP parity
  `in_sync`, and rules/MCP/skills projection parity `in_sync` for both clients.
- Canonical lifecycle hooks were installed for Codex `SessionStart` and agy
  `PreInvocation`; both structural status checks report `installed`.
- Direct execution through the globally installed `ptw` binary succeeded for
  both handlers against the registered repository. Each returned the expected
  bounded empty-state response because the project currently has zero memory
  entries.
- Native non-model client inspection (`codex mcp list` and `agy mcp list`)
  confirmed that both clients can parse and expose the projected
  `basic-memory` server.
- The post-operation status intentionally remains `instructed`, not `hooked`.
  No quota-consuming model invocation or interactive Codex hook trust approval
  was performed, so this section does not claim end-to-end model delivery.

The final local rebuild completed successfully. Its wheel SHA-256 is
`0774c49191901228dd4e15ee9c29fc8eb1d122ad69d8ddbebcc67331ce5804eb`;
wheel archives are timestamp-sensitive, so this later hash does not replace the
earlier reviewed artifact hash recorded above.
