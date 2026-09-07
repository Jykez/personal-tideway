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
- a single workspace outside source projects;
- strict path-boundary and symlink-escape protection;
- canonical directories for projects, knowledge, rules, skills, MCP, state,
  conflicts, backups, locks, and the future Basic Memory service;
- the earlier MCP, rules, skills, conflict, backup, and dry-run engine.

Still under construction:

- current Codex and agy installation discovery;
- project registry and automatic project identity;
- isolated Basic Memory integration;
- bounded context retrieval and concise checkpoint writes;
- agent lifecycle integration and migration from the earlier PAIW workspace.

See [ROADMAP.md](ROADMAP.md) for the delivery order.

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
