# Personal Tideway Roadmap / Дорожная карта

This file is the canonical product roadmap. Personal Tideway memory will later
store only the active milestone, decisions, blockers, and next action, with a
link back here. It must not maintain a second copy of the roadmap.

Этот файл — каноническая дорожная карта продукта. В будущем память Personal
Tideway будет хранить только текущий этап, решения, блокеры и следующий шаг со
ссылкой на этот файл, а не отдельную копию roadmap.

| Status | Milestone | User-visible outcome |
|---|---|---|
| Done | Foundation / Фундамент | Versioned configuration, central paths, boundary safety, tests |
| Done | Client discovery / Обнаружение клиентов | Diagnose current Codex and agy paths without modifying them |
| Done | Project identity / Идентификация проектов | Register Git, directory, and external projects outside source trees |
| Done | Basic Memory | Isolated installation, health checks, and project reconciliation |
| Done | Context loop / Контекстный цикл | Retrieve a bounded context bundle and write concise checkpoints |
| Partial | Agent integration / Интеграция агентов | Live handoff is verified; bounded candidate evidence storage exists, but native-client attestation, automatic `hooked` assurance, and stop capture remain / Живая передача подтверждена; есть ограниченное хранилище кандидатных свидетельств, но остаются нативная проверка клиента, автоматический уровень `hooked` и stop capture |
| Done | Projection parity / Общие возможности | Read-only projection parity reporting across MCP, rules, and skills for Codex and agy, and existing explicit sync projection / Отчёт о паритете проекций MCP, правил и навыков в режиме чтения для Codex и agy, а также существующая явная проекция через синхронизацию |
| Done | Migration / Миграция | Transactional migration tooling and rollback are fixture-verified; this schema-v2 installation completed the backed-up fresh-install path and live smoke acceptance because no legacy migration was required / Транзакционная миграция и rollback проверены на fixtures; для этой установки schema v2 завершены fresh-install с backup и живая smoke-приёмка, поскольку legacy-миграция не требовалась |
| Planned | Public alpha / Публичная alpha | Installer, onboarding, CI, documentation, license, and release artifacts |

## Working protocol / Рабочий протокол

Each implementation iteration should complete one independently testable
capability. Its completion updates this table only when the milestone status
actually changes. Detailed implementation work remains in issues or local PTW
project memory rather than expanding this file into a task dump.

Каждая итерация должна завершать одну независимо проверяемую возможность. Эта
таблица обновляется только при реальном изменении статуса этапа. Подробные
задачи остаются в issues или локальной памяти проекта PTW, чтобы этот файл не
превращался в свалку задач.
