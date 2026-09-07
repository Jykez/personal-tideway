# Personal Tideway v2 — Product and Technical Specification

| Field | Value |
|---|---|
| Status | Draft for user review |
| Scope | Linux, one local user, Codex + Antigravity (`agy`) |
| Memory engine | Basic Memory, local mode |
| Repository | Open-source-ready, not yet public |
| User data | Centralized outside source repositories |
| Web clients | Out of scope |

## 1. Purpose

Personal Tideway is a local control plane that lets a person switch between supported AI
coding agents without manually copying configuration, skills, rules, project
knowledge, current state, decisions, or next steps.

The primary user story is:

1. The user works on a project or infrastructure object in Codex.
2. Codex retrieves only the relevant prior context.
3. Codex records durable outcomes of meaningful work, not the transcript.
4. The user continues the same work in AGY.
5. AGY resolves the same project and receives the same durable context.
6. The reverse direction works identically.

Personal Tideway is not an LLM, agent framework, chat archive, source-code index, or a new
memory database. It coordinates existing client capabilities and delegates
long-term knowledge storage and retrieval to Basic Memory.

## 2. Product principles

The following principles are mandatory and override implementation convenience.

1. **One local workspace.** All Personal Tideway-owned user data lives below one configurable
   root, `PERSONAL_TIDEWAY_HOME` (default: `~/.personal-tideway`).
2. **No repository pollution.** Personal Tideway never writes memory, manifests, generated
   rules, hooks, or metadata into a source repository unless the user invokes an
   explicit export command whose diff is shown first.
3. **One project registry.** Personal Tideway, not an LLM and not Basic Memory, is the source
   of truth for project identity and path mapping.
4. **One canonical artifact, native projections.** Shared rules, skills, and MCP
   definitions are stored once and projected to each client's supported format.
5. **Memory is curated context, not history.** Never store full conversations,
   hidden reasoning, routine command output, generated diffs, or facts that are
   obvious from the current code.
6. **Progressive disclosure.** Session startup receives a small bounded brief.
   Detailed knowledge is searched and loaded only when relevant.
7. **Local-first and inspectable.** Durable knowledge is human-readable Markdown.
   Any database is a disposable/rebuildable index, not the only source of truth.
8. **No silent claims.** An agent may report a memory as persisted only after the
   backing file or Basic Memory operation has been verified successfully.
9. **Safe by default.** Dry-run, backups, atomic writes, secret redaction,
   least-privilege paths, and explicit destructive operations are required.
10. **Prove behavior in real clients.** A matching config file is not evidence
    that a client loaded a rule, skill, MCP server, or memory.
11. **Open-source-ready boundaries, personal-first delivery.** Implement only the
    current Linux/Codex/AGY vertical slice, but keep client and memory backend
    boundaries replaceable.
12. **No Git operations without user approval.** Personal Tideway and its implementation
    process must not commit, amend, merge, tag, push, or publish automatically.

## 3. Supported surfaces and explicit non-goals

### 3.1 Supported in v2

- Linux, one OS user, one machine.
- Codex CLI.
- Local Codex desktop/app tasks that use the machine's Codex configuration.
- Antigravity CLI (`agy`).
- Antigravity IDE using the same current Antigravity customization root.
- Git repositories, Git worktrees, ordinary local directories, and named
  infrastructure objects without a repository.
- Local Basic Memory.
- Existing Personal Tideway MCP, rules, skills, backup, dry-run, and conflict capabilities
  that remain valid after review.

### 3.2 Out of scope for v2

- ChatGPT web and Gemini web.
- Cloud synchronization and multi-device operation.
- Team accounts, RBAC, remote sharing, and `ptw team`.
- Claude, Cursor, Windsurf, VS Code Copilot, and other clients.
- A background daemon required for normal CLI operation.
- Full chat transcript ingestion or automatic import of vendor chat history.
- Training, fine-tuning, embeddings implemented by Personal Tideway, or a custom vector DB.
- Automatic commits or modifications inside user repositories.
- Concurrent multi-agent file locking or task leasing.
- A public release, package publication, or repository push during v2 delivery.

## 4. Terminology

- **PERSONAL_TIDEWAY_HOME**: centralized user-data root, default `~/.personal-tideway`.
- **Software repository**: source code, tests, schemas, docs, and sanitized
  examples for the Personal Tideway application. It contains no user memory or live config.
- **Project**: a stable Personal Tideway identity for a Git repository, local directory, or
  external/infrastructure object.
- **Project binding**: evidence that maps a runtime context to a project, such as
  a canonical path, Git common directory, or normalized remote URL.
- **Canonical artifact**: authoritative Personal Tideway-owned rule, skill, or MCP definition.
- **Projection**: client-native representation generated from canonical data.
- **Memory project**: Basic Memory project owned and registered by Personal Tideway.
- **Current state**: bounded summary of verified present state and unfinished
  work. It is not an event log.
- **Durable knowledge**: a decision, constraint, non-obvious verified fact,
  reusable lesson, known failed approach, or operational runbook.
- **Checkpoint**: explicit persisted update written after meaningful work or
  before context loss.
- **Continuity policy**: common rules specifying when agents recall and retain.

## 5. High-level architecture

```text
                    Codex CLI / Codex App
                              |
                              v
                 +--------------------------+
                 | Client adapter: Codex    |
                 +--------------------------+
                              |
                              v
 +----------+       +--------------------------+       +----------------+
 | AGY/IDE  | ----> |  Personal Tideway core   | ----> | Basic Memory   |
 +----------+       | registry / resolver      |       | local backend  |
       ^            | projections / lifecycle |       +----------------+
       |            +--------------------------+                |
       |                        |                                v
 +--------------------+         |             Markdown under PERSONAL_TIDEWAY_HOME
 | Client adapter: AGY|         v
 +--------------------+   canonical MCP/rules/skills
```

Personal Tideway owns identity, configuration, integration, policy, migration, and health.
Basic Memory owns note parsing, indexing, search, relations, and context building.
Clients must access the same Basic Memory projects; no client-specific memory
copies are permitted.

## 6. Software/data separation

### 6.1 Public software repository

The repository must be shaped as follows. Names may change during implementation
when justified by a reviewed design note.

```text
src/personal_tideway/
  cli/
  core/
    registry.py
    resolver.py
    context.py
    lifecycle.py
  clients/
    base.py
    codex.py
    agy.py
  backends/
    base.py
    basic_memory.py
  distribution/
    mcp.py
    rules.py
    skills.py
  migrations/
  backup.py
  config.py
  exceptions.py
  models.py
tests/
  unit/
  integration/
  behavioral/
docs/
examples/
schemas/
```

The code must not contain real usernames, user-specific home paths, live MCP
tokens, OAuth artifacts, private paths, or copies of user memory.

### 6.2 Central user workspace

```text
~/.personal-tideway/
  config.yaml
  secrets.env
  registry/
    projects.yaml
  projects/
    <stable-project-id>/
      project.yaml
      memory/
        current-state.md
        decisions/
        knowledge/
        tasks/
        runbooks/
  knowledge/
    personal/
  mcp/
  rules/
    shared/
    codex/
    agy/
  skills/
    shared/
    codex/
    agy/
  services/
    basic-memory/
  state/
  conflicts/
  backups/
  locks/
```

No component may create `.personal-tideway`, `.personal-tideway.yaml`, `.basic-memory`, generated `AGENTS.md`, or
generated `GEMINI.md` below a registered source root during ordinary operation.
A regression test must assert this invariant.

## 7. Configuration and paths

Configuration precedence, highest first:

1. Explicit CLI option.
2. Task-specific environment variable.
3. `PERSONAL_TIDEWAY_HOME/config.yaml`.
4. platform default.

Required environment variables:

- `PERSONAL_TIDEWAY_HOME`: user workspace root.
- `CODEX_HOME`: optional Codex root override.
- `GEMINI_HOME` or a documented AGY-specific override: optional Antigravity root.
- `BASIC_MEMORY_CONFIG_DIR`: set by Personal Tideway wrappers, not required from the user.
- `PERSONAL_TIDEWAY_PROJECT`: optional explicit project selection for a process/session.

All paths must expand `~`, resolve safely, preserve symlink semantics where
needed for worktrees, and support spaces and non-ASCII characters.

Configuration and data schemas carry an integer version. Unknown newer versions
must fail read-only with a clear upgrade error; they must never be rewritten.

## 8. Project registry

### 8.1 Ownership

The Personal Tideway project registry is the single source of truth for project existence,
identity, aliases, bindings, and the associated Basic Memory project.

Basic Memory project configuration is a derived integration state. Rebuilding it
from the Personal Tideway registry must be supported.

### 8.2 Project kinds

- `git`: a Git repository, including worktrees.
- `directory`: a local non-Git working directory.
- `external`: infrastructure or another subject without a source directory,
  for example `openwrt`, `proxmox`, or a printer.

### 8.3 Project record

Example only; the implementation must validate against a versioned schema.

```yaml
schema_version: 2
id: "uuid4"
slug: home-infra
display_name: Home Infrastructure
kind: git
aliases: [infra, home-lab]
bindings:
  paths:
    - /path/to/home-infra
  git_common_dirs: []
  git_remotes:
    - host/owner/home-infra
memory:
  backend: basic-memory
  project_name: ptw-home-infra-<short-id>
  path: projects/<stable-project-id>/memory
created_at: "RFC3339 UTC"
updated_at: "RFC3339 UTC"
```

Absolute paths may appear in user data but never in repository fixtures.

### 8.4 Project resolution algorithm

Resolution order is deterministic:

1. Explicit `--project` or `PERSONAL_TIDEWAY_PROJECT` by ID, slug, or unique alias.
2. Exact registered canonical path.
3. Longest registered ancestor path.
4. Git common directory identity.
5. Normalized Git remote identity, only when unambiguous.
6. Unregistered Git root candidate.
7. Unresolved.

Resolution results contain confidence and evidence:

```json
{
  "status": "resolved|candidate|ambiguous|unresolved",
  "project_id": "...",
  "evidence": ["explicit", "path", "git-common-dir", "git-remote"],
  "requires_confirmation": false
}
```

### 8.5 Creation policy

- A previously unseen Git root is auto-registered when
  `projects.auto_register_git: true` (default for v2).
- Auto-registration writes only below PERSONAL_TIDEWAY_HOME.
- An ordinary non-Git directory is not silently registered by default.
- An external object is never silently created. The agent may propose it, but
  must receive user confirmation before calling project creation.
- Ambiguous aliases or remotes never choose arbitrarily.
- Slug collisions are resolved with stable IDs, not silent overwrites.

### 8.6 Moves, clones, and worktrees

- Moving a known Git repository should add/update a path binding rather than
  create duplicate memory.
- Worktrees sharing a Git common directory resolve to the same Personal Tideway project.
- Different repositories with the same basename remain separate.
- Multiple clones with the same remote produce an explicit ambiguity unless the
  registry already has sufficient binding evidence.
- A project removal unregisters bindings but preserves memory by default.
- Purging memory is a separate explicit destructive command with preview.

## 9. Basic Memory backend

### 9.1 Dependency role

Basic Memory is the default and only supported v2 `MemoryBackend`. Personal Tideway must not
reimplement semantic search, Markdown graph parsing, schemas, or embeddings.

Integration follows the current Basic Memory documentation:

- <https://docs.basicmemory.com/start-here/what-is-basic-memory>
- <https://docs.basicmemory.com/reference/configuration>
- <https://docs.basicmemory.com/reference/mcp-tools-reference>
- <https://docs.basicmemory.com/reference/ai-assistant-guide>
- <https://docs.basicmemory.com/integrations/harness-capture>

Pin the tested Basic Memory version or a compatible bounded range. Record the
tested version in status output and behavioral evidence. Automatic dependency
updates must be disabled or explicitly controlled by Personal Tideway for reproducibility.

### 9.2 Isolation

- Basic Memory config and disposable index data live below
  `PERSONAL_TIDEWAY_HOME/services/basic-memory/`.
- Note paths live below `PERSONAL_TIDEWAY_HOME/projects/<id>/memory` or
  `PERSONAL_TIDEWAY_HOME/knowledge/personal`.
- Basic Memory must not select a default project that could receive accidental
  writes when resolution failed. Unresolved operations must require an explicit
  project.
- Personal Tideway must be able to rebuild Basic Memory project registrations and indexes
  without losing Markdown notes.

### 9.3 Backend interface

The Python boundary must be small and testable. At minimum:

```text
health()
ensure_project(project_record)
remove_project_binding(project_record)       # non-destructive to notes
search(project_id, query, limit)
build_context(project_id, references, budget)
read_note(project_id, note_ref)
write_note(project_id, note, expected_revision?)
edit_note(project_id, note_ref, patch, expected_revision?)
reindex(project_id?)
```

Use official CLI/library/MCP behavior rather than parsing Basic Memory's private
SQLite schema. Backend errors must be translated into stable Personal Tideway error types.

### 9.4 MCP launch

Personal Tideway generates one canonical Basic Memory MCP definition targeting Codex and
AGY. Both clients must operate on the same configured Personal Tideway-owned note roots.

Prefer on-demand stdio launch. A local HTTP process is optional only where a
supported local client demonstrably cannot spawn stdio. No always-running daemon
is required for the v2 acceptance path.

If a wrapper is required, provide a documented executable such as
`ptw-memory-run` that:

1. resolves PERSONAL_TIDEWAY_HOME;
2. sets Basic Memory's config directory;
3. resolves the project from explicit selection or process CWD;
4. applies the project constraint when resolution succeeds;
5. fails safely or requires explicit project selection when resolution is
   ambiguous;
6. uses `exec`, not a shell-evaluated command string;
7. never prints secrets.

Concurrent Codex and AGY MCP processes must be tested against the same backend.

### 9.5 Compatibility CLI

Existing `ptw memory` commands become a compatibility facade over the backend:

- `ptw memory search QUERY [--project PROJECT]`
- `ptw memory read REF [--project PROJECT]`
- `ptw memory write ... [--project PROJECT]`
- `ptw memory status`

The v1 ad-hoc Markdown implementation must not remain as a second active store.

## 10. Memory taxonomy and schemas

### 10.1 Required project notes

Each registered project receives these logical records. Physical filenames may
follow Basic Memory conventions, but must remain human-readable and stable.

1. `current-state`: verified current condition, active objective, incomplete
   work, blockers, verification status, and next safe action.
2. `decisions/*`: important decisions with context, alternatives, rationale,
   consequences, and supersession links.
3. `knowledge/*`: stable non-obvious facts and constraints.
4. `tasks/*`: durable tasks or follow-ups that should survive sessions.
5. `runbooks/*`: repeatable operational procedures with verification and safety
   boundaries.

### 10.2 Current-state constraints

- Must be bounded by configurable character/token budget.
- Must be updated, not appended indefinitely.
- Must distinguish `verified`, `partial`, `planned`, and `blocked` claims.
- Must record the last meaningful update time and agent/client attribution.
- Must not duplicate data easily derived from source code or Git status.
- Must not claim deployment or infrastructure success without runtime evidence.

### 10.3 Information worth retaining

Retain only if at least one condition holds:

- a user preference that should affect future work;
- a verified architectural or product decision and its rationale;
- a non-obvious environment, infrastructure, or project constraint;
- a failed approach likely to be retried by a future agent;
- an operational change outside Git whose state would otherwise be lost;
- an unfinished objective, blocker, or next step needed for continuation;
- a reusable procedure whose rediscovery is costly;
- a correction to previously stored knowledge.

Do not retain:

- full prompts, responses, hidden reasoning, or transcripts;
- routine file lists, diffs, command logs, test output, or temporary paths;
- generated facts not verified against source/runtime evidence;
- secrets, tokens, cookies, authorization headers, private keys, or passwords;
- facts already clear and cheap to rediscover from the repository;
- guesses presented as facts;
- duplicate notes when an existing note can be edited.

### 10.4 Provenance

Every material observation should support metadata for:

- project ID;
- category/type;
- created/updated timestamps;
- source client (`codex`, `agy`, `user`, `migration`);
- evidence reference when available, without embedding large outputs;
- confidence or status (`verified`, `partial`, `planned`, `stale`);
- relation/supersession links.

## 11. Continuity lifecycle

### 11.1 Session/task start

For a resolved project:

1. Load only project identity and bounded `current-state`.
2. Search memory when the request refers to prior work, decisions, known
   infrastructure, or an ambiguous project-specific fact.
3. Build context only for selected relevant notes.
4. Report ambiguity rather than load a similarly named project.

For an unresolved context:

- do not write to a fallback/default memory project;
- continue without project memory if appropriate;
- suggest registration only when the work is substantial or persistent;
- external project creation requires user confirmation.

### 11.2 During work

Agents may write a durable decision/finding as soon as it becomes verified and
useful. They must search first and prefer editing an existing note.

### 11.3 Checkpoint

A checkpoint is requested when:

- meaningful work completed;
- a material verified decision was made;
- an external system state changed;
- the task ends with unfinished work or a blocker;
- context compaction is imminent or has just occurred;
- the user explicitly asks to remember/save context.

A checkpoint must be concise and idempotent. Repeating it must not create a new
duplicate note or append the same bullet repeatedly.

### 11.4 Hook strategy

- Use deterministic client lifecycle hooks where current clients expose them.
- Use Basic Memory's supported Codex harness integration where compatible.
- Implement and verify current AGY hooks under its active customization root.
- A small always-on continuity rule is the fallback and policy declaration.
- Detailed workflows live in on-demand shared skills, not the always-on prompt.
- Never state that auto-capture is guaranteed if the client exposes no reliable
  lifecycle event. `ptw doctor` must report the effective assurance level.

Assurance levels:

- `hooked`: deterministic lifecycle event installed and behavior verified.
- `instructed`: rule/skill available, agent tool choice still required.
- `manual`: only explicit user/CLI save is available.
- `unavailable`: memory integration failed or is disabled.

## 12. Rules distribution

### 12.1 Canonical scopes

- `rules/shared`: portable rules applicable to both clients.
- `rules/codex`: Codex-only rules.
- `rules/agy`: AGY-only rules.

Only genuinely portable behavior belongs in `shared`.

### 12.2 Codex projection

- Project/global instruction mechanism must follow the installed Codex version.
- Current target is the managed block in `CODEX_HOME/AGENTS.md`.
- Text outside Personal Tideway's markers is preserved byte-for-byte.
- Existing safe conflict and backup behavior is retained.

### 12.3 AGY projection

The v1 target `~/.gemini/GEMINI.md` is obsolete for global modern AGY use and
must not remain the active default.

Current AGY global customization root is `~/.gemini/config/`. The adapter must
use the installed version's supported global rules mechanism (currently
`~/.gemini/config/rules/` and/or a supported standalone rule inside that root),
including the correct always-on metadata where required.

The adapter must verify discovery using a fresh AGY session and a harmless
sentinel rule. File equality alone is insufficient.

### 12.4 Continuity rule size

The always-on shared rule should contain only:

- project resolution/recall requirement;
- search-before-guessing requirement;
- retention criteria;
- secret and transcript prohibition;
- verified-persistence requirement;
- pointer to on-demand memory skills.

Set and test a bounded size target; avoid copying full Basic Memory manuals into
every prompt.

## 13. Skills distribution

### 13.1 Scopes and validation

- `skills/shared` is exposed only after validation in both client adapters.
- `skills/codex` is exposed only to Codex.
- `skills/agy` is exposed only to AGY.
- No client-specific skill is automatically promoted to shared.

### 13.2 Correct client locations

- Codex default: `~/.codex/skills/`.
- Current AGY default: `~/.gemini/config/skills/`.
- The obsolete v1 AGY target `~/.gemini/skills/` must be migrated safely and no
  longer treated as active without runtime evidence.

### 13.3 Basic Memory skills

Evaluate and vendor, pin, or install only the minimal compatible subset:

- `memory-notes`;
- `memory-continue`;
- `memory-tasks`;
- `memory-reflect` if its capture behavior passes retention tests.

Third-party skills are not trusted merely because installation succeeded.
Codex reviews their complete content, permissions, referenced scripts, update
mechanism, and compatibility before acceptance.

## 14. MCP registry

Retain the v1 canonical MCP model where tests prove it correct:

- stdio and HTTP transports;
- command/args/url;
- targets, enabled, profiles, tags;
- environment placeholders;
- per-client overrides;
- portable-subset comparison;
- managed Codex `[mcp_servers.*]` sections;
- managed AGY `mcpServers` entries with HTTP `serverUrl`;
- `ptw-mcp-run` secret expansion without shell evaluation.

Add the Basic Memory server as a normal canonical MCP object, but mark it as a
core-managed dependency so accidental disable/removal produces a clear warning.

OAuth remains in client-native stores. Personal Tideway never extracts or copies OAuth
tokens. Secrets are represented by environment variable names, never values in
the repository, logs, diffs, state, conflicts, or memory.

## 15. CLI contract

Existing commands remain when compatible. The v2 CLI must provide:

### 15.1 Workspace

```text
ptw init [--dry-run]
ptw status [--json]
ptw sync [--dry-run]
ptw doctor [--json]
ptw migrate plan
ptw migrate apply [--dry-run]
```

### 15.2 Projects

```text
ptw project list [--json]
ptw project add [PATH] [--name NAME] [--kind git|directory]
ptw project add-external NAME [--alias ALIAS ...]
ptw project resolve [PATH] [--json]
ptw project show PROJECT [--json]
ptw project bind PROJECT PATH
ptw project unbind PROJECT PATH
ptw project remove PROJECT [--dry-run]       # memory preserved
ptw project purge PROJECT --confirm PROJECT  # destructive, preview required
```

Auto-registration uses the same internal service as `project add`; it must not
have a divergent code path.

### 15.3 Context and checkpoints

```text
ptw context show [--project PROJECT] [--budget N] [--json]
ptw context search QUERY [--project PROJECT] [--limit N]
ptw checkpoint [--project PROJECT] --file FILE
ptw checkpoint [--project PROJECT] --stdin
```

Checkpoint input is structured and validated. Avoid accepting arbitrary shell
substitution examples that encourage secret leakage.

### 15.4 Memory compatibility facade

As specified in section 9.5. All operations must resolve or require an explicit
project and must use Basic Memory.

### 15.5 Existing distribution commands

Retain and update:

- `ptw mcp list|add|enable|disable|test`;
- `ptw skill list|create|link|unlink|share`;
- `ptw resolve` for supported three-way conflicts.

Exit codes must remain documented, with new stable codes for ambiguous project,
backend unavailable, migration required, and destructive confirmation failure.

### 15.6 Public installation and first-run onboarding

Personal Tideway is installed as an isolated user-level CLI from a reviewed
GitHub release tag or commit. `uv` is the recommended installer; `pipx` is the
fallback:

```bash
uv tool install "git+https://github.com/<owner>/personal-tideway.git@vX.Y.Z"

# Fallback
pipx install "git+https://github.com/<owner>/personal-tideway.git@vX.Y.Z"
```

Installing the executable only places `ptw` and its packaged dependencies in an
isolated tool environment. Package installation must not run Personal Tideway
post-install hooks, create `PERSONAL_TIDEWAY_HOME`, provision Basic Memory, or
modify Codex, AGY, source repositories, or Git state.

A fresh user follows this explicit sequence:

```bash
ptw status
ptw init --dry-run
ptw init
ptw sync --dry-run
ptw sync
ptw doctor
```

- Pre-initialization `ptw status` is read-only and reports detected clients,
  effective paths, and the uninitialized state.
- `ptw init --dry-run` shows central workspace and pinned Basic Memory setup
  actions without writing files.
- `ptw init` creates only Personal Tideway-owned central state and the isolated
  Basic Memory environment. It does not project changes into client configs.
- `ptw sync --dry-run` lists every client file, managed block/link, validation,
  conflict, and backup destination that an apply would affect.
- `ptw sync` is the explicit apply gate. It creates restorable backups before
  modifying client files and must not touch unrelated content.
- `ptw doctor` validates the applied result. No interactive confirmation is a
  substitute for the explicit dry-run and apply commands above.

An existing user follows the migration path instead:

```bash
ptw status
ptw migrate plan
ptw migrate apply --dry-run
ptw migrate apply
ptw sync --dry-run
ptw sync
ptw doctor
```

Migration planning, application, and rollback evidence must be idempotent and
reversible as specified in section 17. Migration must stop before writes when a
legacy state, client path, or conflict cannot be resolved safely.

Onboarding is complete only when `ptw doctor` reports:

- a valid central root outside all registered source repositories, with safe
  permissions and no repository pollution;
- the pinned Basic Memory version, isolated configuration, healthy backend, and
  working MCP connection;
- syntactically valid Codex and AGY projections plus the available behavioral
  evidence and truthful lifecycle assurance level for each client;
- valid backup and rollback records for every client file changed during setup;
- no unresolved migration, configuration, secret, or projection error.

Upgrade and uninstall remain separate from configuration changes:

```bash
uv tool upgrade personal-tideway
uv tool uninstall personal-tideway

# pipx fallback
pipx upgrade personal-tideway
pipx uninstall personal-tideway
```

An executable upgrade must not apply schema migrations or rewrite client
configuration automatically; the user runs `ptw migrate plan` and the explicit
apply sequence afterward. Package-manager uninstall removes only the executable
environment. It preserves `PERSONAL_TIDEWAY_HOME`, memories, backups, and
existing client projections by default. Removing projections or deleting user
data requires a separate previewable operation; destructive purge must require
explicit confirmation and is never part of package uninstall.

## 16. Status and doctor UX

`ptw status` should answer, without exposing secrets:

- Personal Tideway schema/software version;
- detected clients and versions;
- active client paths;
- project count and resolved current project;
- Basic Memory version, health, project registration parity, and index status;
- rules and skills projection parity;
- MCP parity;
- continuity assurance per client;
- pending migration/conflicts;
- last successful behavioral smoke-test time.

`ptw doctor` performs read-only checks by default and prints exact remediation.
Repairs require an explicit subcommand or flag, support dry-run, and back up
modified files.

Example summary:

```text
Current project: home-infra (resolved by git-common-dir)
Memory: healthy, indexed, 14 notes
Codex continuity: hooked + behavior verified
AGY continuity: instructed + behavior verified
Rules: in sync
Skills: in sync
MCP: in sync
Repository pollution: none detected
```

## 17. Migration from v1

Migration is explicit, previewable, idempotent, and reversible.

### 17.1 Required migrations

1. Back up the complete v1 Personal Tideway config and every live client file to be changed.
2. Detect and remove only Personal Tideway-managed blocks/links at obsolete AGY paths.
3. Move AGY rule projection to the current global customization root.
4. Move AGY skill projections to the current global skills root.
5. Preserve unmanaged text and unrelated files.
6. Replace v1 manual memory with Basic Memory-backed projects. Since the current
   live v1 memory is empty, no content conversion is expected, but the general
   migration must support sanitized fixtures containing entries.
7. Create the central registry and project directories only below PERSONAL_TIDEWAY_HOME.
8. Register selected real projects only after showing the plan to the user.
9. Never create `.personal-tideway.yaml` or other metadata in those repositories.
10. Keep existing canonical MCP definitions and secrets behavior after validation.

### 17.2 Rollback

Rollback must restore:

- original Codex and AGY config files;
- original rules files/blocks;
- original skill links;
- original v1 Personal Tideway config/state;
- no partially registered Basic Memory project entries.

New central memory created after successful migration is not silently deleted by
rollback; it is preserved and reported.

## 18. Security and privacy requirements

1. Never inspect, import, print, diff, or delegate OAuth stores or Codex auth
   files.
2. `secrets.env` remains mode `0600`; fail if safe permissions cannot be set.
3. Secret placeholders are expanded only at process launch.
4. No `shell=True`, `eval`, sourced secrets file, or command interpolation.
5. All logs and errors pass through secret-value redaction.
6. Memory writes reject known secret patterns and credential assignments where
   feasible, without echoing suspected values.
7. PERSONAL_TIDEWAY_HOME path boundaries are validated before destructive operations.
8. Symlink attacks and traversal outside approved roots are tested.
9. Downloaded third-party packages/skills are pinned and reviewed.
10. Basic Memory's index may be deleted/rebuilt; Markdown notes are backed up.
11. Behavioral fixtures contain only generated names, repositories, and tokens.
12. The implementation brief sent to AGY contains no live configs or private
    memory; only sanitized fixtures and paths inside its isolated clone.

## 19. Reliability and concurrency

- Writes to Personal Tideway YAML/JSON/Markdown use atomic replace and fsync where applicable.
- Registry modification uses a lock below `PERSONAL_TIDEWAY_HOME/locks` with stale-lock
  diagnostics.
- Two simultaneous project auto-registration attempts converge on one record.
- Concurrent Codex and AGY memory operations must not corrupt notes or indexes.
- Failed backend writes do not advance Personal Tideway checkpoint metadata.
- Partial projections do not report global success.
- Dry-run changes no files, timestamps, backups, indexes, or state.
- Repeated sync/migration/checkpoint operations are idempotent.
- Unknown/unavailable clients do not block healthy supported clients unless a
  requested portable artifact requires both.

## 20. Testing strategy

### 20.1 Unit tests

- configuration precedence and path handling;
- project record/schema validation;
- path, ancestor, Git remote, common-dir, alias, and ambiguity resolution;
- slug collisions and stable IDs;
- retention policy validation and secret rejection;
- Basic Memory backend error translation;
- rule composition and marker parsing;
- skill compatibility classification;
- MCP normalization and client projections;
- migration planning and rollback manifests;
- dry-run immutability;
- lock and atomic-write behavior.

### 20.2 Integration tests in temporary homes

- initialize a clean PERSONAL_TIDEWAY_HOME;
- initialize local Basic Memory below that root;
- register Git, worktree, directory, and external projects;
- write/search/edit/build context through the backend;
- launch two backend/MCP processes against the same store;
- generate Codex and AGY configs while preserving unrelated data;
- install rules and skills in current client paths;
- migrate sanitized v1 fixtures;
- roll back to byte-identical original client files;
- assert zero files were written to fixture repositories.

### 20.3 Behavioral client tests

Tests use harmless sentinel projects and facts, never live secrets.

1. **Codex rule discovery:** a fresh task obeys a unique shared sentinel rule.
2. **AGY rule discovery:** a fresh AGY session obeys the same sentinel rule.
3. **Skill discovery:** each client can identify/invoke a shared sentinel skill.
4. **Codex to AGY handoff:** Codex persists a verified decision and unfinished
   next step; a fresh AGY session retrieves both without being told their text.
5. **AGY to Codex handoff:** reverse direction.
6. **External project isolation:** an OpenWRT-like fact is not returned in an
   unrelated Git project briefing.
7. **No default miswrite:** unresolved context cannot write into another project.
8. **Token budget:** startup briefing stays within configured bound.
9. **Persistence evidence:** the expected Markdown note exists after the agent
   reports success.
10. **Restart survival:** context remains available after both clients and MCP
    processes restart.

Behavioral tests that consume model quota are explicitly invoked smoke tests,
not part of every unit-test run.

### 20.4 Live user acceptance test

After review and backup, use exactly:

- one disposable Git repository;
- one disposable external project;
- one harmless decision;
- one unfinished next step;
- both transfer directions.

Only after passing disposable tests may existing real projects be registered.

## 21. Acceptance criteria

v2 is accepted only when all are true:

1. A fresh Codex task and fresh AGY session load the same shared personal rule.
2. Both clients discover the same accepted shared Basic Memory skills.
3. Both clients access the same local Basic Memory notes through MCP.
4. A verified fact saved by Codex is retrievable by AGY after restart, and vice
   versa.
5. A Git project is auto-registered centrally without modifying its worktree.
6. Git worktrees resolve to one project memory.
7. An external project requires confirmation once and works thereafter.
8. Ambiguous resolution never chooses silently.
9. Project memory, tasks, and state live exclusively below PERSONAL_TIDEWAY_HOME.
10. No Personal Tideway-generated file enters a source repository during standard flows.
11. Startup context is bounded and details are loaded on demand.
12. Repeated checkpoints do not duplicate content.
13. Secrets and OAuth material never enter memory, logs, patches, or delegated
    fixtures.
14. `sync`, migration, and checkpoint operations are idempotent.
15. Dry-run is byte-for-byte non-mutating.
16. Backup and rollback restore original client configuration.
17. Unmanaged Codex TOML and AGY JSON content remains semantically unchanged.
18. `ptw doctor` detects obsolete AGY paths and provides safe remediation.
19. All automated tests pass in a fresh isolated environment.
20. Codex independently reviews the entire patch and runs all tests; AGY's own
    report is not treated as verification.

## 22. Implementation phases

### Phase 0 — Specification and baseline gate

- Review and approve this specification.
- Decide the temporary baseline contents; remove build artifacts from tracking
  expectations and verify `.gitignore`.
- Run the current test suite and capture results.
- Show complete initial repository diff/status to the user.
- Create the first baseline commit only after separate explicit approval.
- Prepare sanitized fixtures and an AGY implementation brief.

Exit gate: clean committed source baseline suitable for isolated cloning; no live
user data in Git.

### Phase 1 — v2 boundaries and current client adapters

- Refactor without behavior loss into core/client/backend/distribution boundaries.
- Add schema versioning and migration framework.
- Correct modern AGY rule and skill paths.
- Preserve and extend v1 MCP, backup, conflict, and dry-run tests.
- Add runtime discovery metadata and doctor checks.

Exit gate: current rules/skills/MCP work in isolated Codex/AGY fixtures; no
memory implementation yet.

### Phase 2 — Central project registry and resolver

- Implement models, schema, locking, commands, and deterministic resolution.
- Implement Git auto-registration, worktree identity, moves, aliases, and
  external project confirmation.
- Prove no writes occur in project roots.

Exit gate: disposable Git/worktree/external cases pass unit and integration
tests.

### Phase 3 — Basic Memory backend

- Add pinned dependency and isolated Basic Memory config.
- Implement backend boundary and project reconciliation.
- Register canonical MCP for both clients.
- Replace v1 memory commands with the backend facade.
- Add concurrency, rebuild, and failure tests.

Exit gate: two local MCP clients can read/write the same isolated memory project.

### Phase 4 — Continuity rules, skills, and lifecycle

- Add compact canonical continuity policy.
- Review and integrate minimal Basic Memory skills.
- Integrate Codex lifecycle support.
- Integrate current AGY rules/hooks with accurate assurance reporting.
- Implement bounded briefing and idempotent checkpoint schemas.

Exit gate: behavioral sentinel tests prove discovery and both handoff directions.

### Phase 5 — Migration and live-safe installation

- Implement v1 detection, plan, apply, rollback, and obsolete-path cleanup.
- Validate against sanitized copies of current live configurations.
- Show final migration diff to the user.
- After approval, back up and migrate live configuration.
- Smoke-test disposable projects before registering real ones.

Exit gate: live clients pass smoke tests; rollback evidence is retained.

### Phase 6 — Dogfooding and open-source hardening

- Use v2 on real personal work without publishing.
- Record observed recall quality, false saves, duplicates, token budgets, and
  resolution failures.
- Adjust policies based on evidence.
- Remove personal assumptions, improve setup UX, write public docs, add CI,
  choose license, and perform trademark availability checks.

Exit gate: separate user approval for public repository creation/push/release.

## 23. Delegation and review protocol

AGY performs the implementation in an isolated Git clone. Codex remains product
owner, reviewer, verifier, and sole integrator.

### 23.1 Before delegation

Codex must provide AGY:

- the exact approved specification revision for traceability, but only the
  sections and excerpts directly relevant to the assigned iteration;
- clean baseline commit hash;
- one bounded capability to implement;
- a small explicit set of allowed files/modules;
- sanitized fixtures;
- invariants and acceptance criteria for the assigned iteration;
- required tests, all marked as not run by AGY;
- explicit prohibition on terminal, network, credentials, live config, commits,
  and direct user-workspace edits inside the implementation run.

The brief must not attach the complete specification, unrelated architecture,
or requirements for later iterations. Large context is permitted only when AGY
is explicitly able and allowed to coordinate its own subagents. The isolated
implementer workflow currently forbids subagents, so every task in that workflow
must be short and self-contained.

### 23.2 AGY output

Required artifacts:

- complete patch including new files;
- changed-file manifest;
- structured implementation report;
- assumptions and known limitations;
- test plan with tests marked `NOT_RUN`.

AGY must not modify the source repository, live PERSONAL_TIDEWAY_HOME, or live client configs.

### 23.3 Codex review

For every delivered patch Codex:

1. verifies source and clone hashes/status;
2. reads the manifest and full diff;
3. checks for unrelated/private/generated files;
4. inspects every changed implementation and test file;
5. runs formatting, unit, integration, migration, and behavioral checks as
   appropriate;
6. classifies the patch as accepted, partially accepted, returned, or rejected;
7. returns raw failures and concrete requirement mismatches;
8. permits at most two correction rounds before replanning;
9. shows the accepted final diff and checks to the user before integration;
10. performs no commit/push/release without separate user approval.

### 23.4 Short-iteration batching

Implementation phases are planning milestones, not delegation units. Never send
an entire phase or a combination of phases to AGY in one implementation run.

Every iteration must:

1. implement one independently testable capability;
2. receive only the minimum relevant specification excerpt and fixtures;
3. touch a small, explicit file set;
4. start from an explicitly approved committed baseline;
5. produce a patch that Codex can review completely before the next iteration;
6. stop and reduce scope after a timeout or excessive context use instead of
   retrying the same oversized request.

The expected implementation sequence is split into work packages such as:

1. configuration schema and centralized path boundaries;
2. current Codex path discovery and diagnostics;
3. current AGY path discovery and diagnostics;
4. project registry schema and persistence;
5. Git repository resolution and auto-registration;
6. Git worktree identity and repository move handling;
7. ordinary directory registration;
8. external-object registration and confirmation;
9. isolated Basic Memory installation and health checks;
10. Basic Memory project reconciliation;
11. bounded project context retrieval;
12. concise idempotent checkpoint writes;
13. continuity rules and Codex lifecycle integration;
14. continuity rules and AGY lifecycle integration;
15. MCP, rules, and skills projection parity;
16. migration planning;
17. migration apply and rollback;
18. public installation and first-run onboarding.

Codex may split any package further when its brief, file set, or acceptance
criteria no longer fit a short implementation run. Dependent packages are
delegated sequentially. Phase 6 remains driven by real usage evidence and is not
delegated wholesale.

## 24. Definition of done

The project is not done when configuration files merely contain expected text.
It is done when the user can:

1. work meaningfully in a disposable project with Codex;
2. close/restart the client;
3. open AGY and continue from the persisted verified state without copying a
   briefing;
4. make a further meaningful change in AGY;
5. close/restart it;
6. resume accurately in Codex;
7. inspect every retained fact as Markdown below one central PERSONAL_TIDEWAY_HOME;
8. confirm that neither source repository contains Personal Tideway-generated state;
9. view health, provenance, and assurance with `ptw status`/`doctor`;
10. recover original client configuration through tested rollback.

Only then may open-source packaging proceed.

## 25. Decisions fixed by the user

The following are decided and are not open implementation questions:

- Web chatbot synchronization is excluded.
- Basic Memory is the v2 memory engine.
- Personal Tideway is a central local control plane, not a custom memory database.
- Personal Tideway owns project identity.
- All project state/memory is outside source repositories by default.
- Git projects may be auto-registered centrally.
- External projects require confirmation before first creation.
- Codex and AGY must share one canonical rule/skill/memory system.
- The software should be capable of becoming a GitHub open-source project.
- AGY performs most implementation; Codex plans, reviews, tests, and integrates.

## 26. Questions deferred until after the first vertical slice

These must not block phases 1–4 unless evidence forces a decision:

- whether optional selected memory notes may be exported into a repository;
- support for clients beyond Codex and AGY;
- cloud or multi-device memory;
- advanced automatic consolidation/decay;
- task coordination between simultaneously editing agents;
- graphical UI;
- public package distribution channel.
