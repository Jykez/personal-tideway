# Personal Tideway

[Русская версия](README.ru.md)

Personal Tideway is a local continuity layer for people who switch between
Codex and Antigravity (`agy`). Its goal is to give both agents the same curated
project context, reusable skills, rules, and MCP definitions without copying
whole conversations or scattering metadata across source repositories.

> **Development status:** pre-alpha. The repository contains a tested
> configuration and synchronization foundation, but the seamless project
> memory workflow is not complete. Do not point this development version at
> live client configuration without reviewing a dry run and backup plan.

## What it should feel like

1. Open a Git repository, an ordinary directory, or an infrastructure task.
2. Personal Tideway identifies the project and retrieves a small relevant
   context bundle.
3. Work in either Codex or agy.
4. Important decisions, state, and next steps are checkpointed locally.
5. Continue later with either agent without rediscovering the project.

The project deliberately stores curated facts and checkpoints, not chat logs.

## Current state

Available and covered by automated tests:

- versioned central configuration with deterministic precedence;
- a single workspace outside source projects with strict path-boundary and
  symlink-escape protection;
- current Codex and agy installation discovery without modifying client paths;
- deterministic project registry and identity for Git repositories, ordinary
  directories, and explicit external projects;
- isolated pinned Basic Memory 0.23.2 installation, health checks, and
  registry-to-config reconciliation without implicit main or default projects;
- explicit reindex workflow and per-project JSON status reporting;
- read-only, bounded context retrieval plus concise, idempotent project
  checkpoint writes through Basic Memory;
- safe CLI bridge for bounded project context retrieval (`ptw context show`,
  `ptw context search`) and idempotent checkpoint persistence (`ptw checkpoint`);
- runnable, tested checkpoint handoff workflow between Codex and agy via the canonical CLI bridge and initial-context lifecycle handlers (see [CHECKPOINT_HANDOFF.md](docs/CHECKPOINT_HANDOFF.md));
- canonical shared continuity policy and minimal shared continuity skill
  provisioned on init and projected via sync to Codex and agy;
- typed, evidence-backed continuity assurance evaluator reporting instructed,
  manual, or unavailable in status and doctor without false hook claims;
- safe Antigravity CLI (agy) initial-context lifecycle hook provisioning (`ptw hook install`,
  `ptw hook remove`, `ptw hook status`, `ptw hook plan`) and bounded PreInvocation handler;
- safe Codex initial-context lifecycle hook provisioning (`ptw hook install --client codex`,
  `ptw hook remove --client codex`, `ptw hook status --client codex`, `ptw hook plan --client codex`) and bounded SessionStart handler (`ptw hook codex-session-start`);
- deterministic, read-only Projection Parity evaluator in `ptw status` reporting separate MCP, rules, and skills results per Codex/agy with safe sync remediation and portable MCP parity preservation;
- safe dry-run and fail-closed operational boundaries;
- the earlier MCP, rules, skills, conflict, backup, and dry-run engine;
- comprehensive passing automated test suite plus real disposable smoke tests.

Still under construction:

- durable live client hook assurance verification (evaluator reports `instructed`, `manual`, or `unavailable`; disposable capability probe does not auto-promote live assurance without active installed/enabled/trust/digest evidence);
- automatic stop/exit lifecycle hook capture (clients currently expose no native stop event; checkpointing remains explicit/instructed via CLI);
- continuous live background synchronization for rules, skills, and MCP;
- live migration and rollback from the legacy workspace;
- public alpha installer, onboarding, CI, and release packaging.

See [ROADMAP.md](ROADMAP.md) for the delivery order.

## Codex lifecycle integration

Personal Tideway integrates with Codex CLI (0.152.0) using its native hook contract ([Codex hooks documentation](https://learn.chatgpt.com/docs/hooks)):

- **Lifecycle commands:** `ptw hook plan --client codex`, `ptw hook install --client codex`, `ptw hook status --client codex`, and `ptw hook remove --client codex` (`--dry-run` is supported for `install` and `remove`; default client remains `agy`).
- **Hook target:** Managed hooks live in `$CODEX_HOME/hooks.json`. An optional `--codex-hooks` flag accepts paths strictly bounded within `$CODEX_HOME` with lexical symlink traversal validation.
- **Event contract:** Listens exclusively to the single `SessionStart` event with matcher regex `^(startup|resume|clear|compact)$`, executing handler `ptw hook codex-session-start`.
- **Parsing and serialization safety:** Enforces a strict 256 KiB file limit for JSON and TOML hook files, while the handler stdin payload enforces a 64 KiB cap; both enforce a maximum JSON recursion depth of 64 (TOML is parser-bounded with standard parser exceptions and no explicit depth limit). Unrelated JSON content is preserved semantically. Hook file writes are atomic, and backups are created before modifications. The installer performs append/remove only on its exact managed group. Duplicate entries, modified managed entries, and inline collision with existing Tideway commands fail closed.
- **Layering and trust model:** Codex hook layers are additive (not override); matching hook commands can run concurrently. Project-level hooks only run in trusted projects. User-level hooks require manual approval via `/hooks` or initial trust review in the Codex TUI. The installer never writes trust hashes and never bypasses client security prompts. Explicitly disabled hooks or managed-only policies may prevent hook execution.
- **Handler boundaries:** The `codex-session-start` handler only serves strictly registered projects (no automatic project registration), ignores conversation transcript dumps, and executes exactly one bounded context retrieval (max 5 items, 8,000 characters), returning a `hookSpecificOutput` payload with `additionalContext`.
- **Assurance and behavioral probe status:** Structural presence in `ptw status` or `ptw doctor` reports assurance strictly among `instructed`, `manual`, or `unavailable` (`hook_verified` remains `False`; no `incapable` level exists in the evaluator). The isolated disposable capability probe passed on 2026-09-15 without security bypass (see [CODEX_HOOK_PROBE.md](docs/CODEX_HOOK_PROBE.md)), validating the negative control (`NO_INITIAL_CONTEXT`), TUI trust review, Codex-computed digest, single backend retrieval, and exact model marker `PTW_CODEX_4CB_92E8B6D1` for `startup`. Live configs were not changed, and disposable success does not automatically promote live assurance to `hooked`.
- **Next steps:** Stop/checkpoint lifecycle integration is explicitly scheduled as the next separate slice; no migration, feature parity, or public alpha is claimed yet.

## Data layout

All Personal Tideway-owned user data lives under `PERSONAL_TIDEWAY_HOME`
(default: `~/.personal-tideway`):

```text
~/.personal-tideway/
  config.yaml
  secrets.env
  registry/projects.yaml
  projects/
  knowledge/personal/
  mcp/
  rules/{shared,codex,agy}/
  skills/{shared,codex,agy}/
  services/basic-memory/
  state/
  conflicts/
  backups/
  locks/
```

Ordinary operation must not create Personal Tideway or Basic Memory state
inside a registered source directory.

## Development setup

Requirements: Linux, Python 3.11 or newer, and `uv`.

```bash
git clone https://github.com/Jykez/personal-tideway.git
cd personal-tideway
uv sync --extra test
uv run ptw --version
uv run pytest -q
```

Use temporary homes while experimenting:

```bash
export PERSONAL_TIDEWAY_HOME=/tmp/ptw-home
export CODEX_HOME=/tmp/ptw-codex
export GEMINI_HOME=/tmp/ptw-gemini
uv run ptw init
uv run ptw status
uv run ptw sync --dry-run
```

Do not use those example paths for valuable data.

## Safety model

- `secrets.env` is created with mode `0600` and is ignored by Git;
- OAuth remains in each client's own credential store;
- unknown newer schemas fail without being rewritten;
- initialization validates its complete path plan before mutation;
- conflicting objects are isolated instead of overwriting unrelated data;
- dry runs must not change files, state, backups, or timestamps.

Security-sensitive behavior is tested in temporary directories. Run:

```bash
uv run --with pytest pytest -q
uv build
```

## Versioning and release notes

The package uses PEP 440 versions from one source:
`personal_tideway.__version__`. See [VERSIONING.md](VERSIONING.md) and the
bilingual [CHANGELOG.md](CHANGELOG.md).

## Detailed specification

The current design contract is
[docs/PERSONAL_TIDEWAY_V2_SPEC.md](docs/PERSONAL_TIDEWAY_V2_SPEC.md). It is a
design document, not a claim that every described feature is already shipped.

## License

Personal Tideway is licensed under the [Apache License 2.0](LICENSE).
