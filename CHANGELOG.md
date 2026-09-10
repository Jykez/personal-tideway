# Changelog / Журнал изменений

Release notes follow the package version and are written in English and
Russian. Dates use `YYYY-MM-DD`.

Заметки о версиях соответствуют версии пакета и ведутся на английском и
русском языках. Даты записываются в формате `YYYY-MM-DD`.

## [0.1.0.dev2] - 2026-09-10

### English

- Current Codex and agy installation discovery without modifying client paths.
- Deterministic project registry and identity for Git repositories, ordinary
  directories, and explicit external projects outside source trees.
- Isolated pinned Basic Memory 0.23.2 installation and health checks.
- Registry-to-config reconciliation without implicit main or default projects.
- Explicit reindex workflow and per-project JSON status reporting.
- Safe dry-run and fail-closed path boundaries across operations.
- 325 passing automated tests plus real disposable smoke tests.

This checkpoint delivers client discovery, project identity, and Basic Memory
plumbing. Bounded context retrieval, concise checkpoint writing, agent lifecycle
hooks, live migration, and public alpha remain unimplemented.

### Русский

- Обнаружение актуальных путей и установок Codex и agy без их изменения.
- Детерминированный реестр и идентификация проектов для Git-репозиториев,
  обычных каталогов и явных внешних проектов вне деревьев исходного кода.
- Изолированная установка фиксированной версии Basic Memory 0.23.2 и проверка
  работоспособности.
- Синхронизация реестра с конфигурацией без неявного назначения main или default.
- Явный процесс переиндексации (reindex) и JSON-отчёт о статусе каждого проекта.
- Безопасные режимы dry-run и fail-closed границы путей во всех операциях.
- 325 успешно проходящих автоматических тестов и реальное одноразовое
  smoke-тестирование.

Этот чекпоинт реализует обнаружение клиентов, идентификацию проектов и базовую
интеграцию Basic Memory. Ограниченная выдача контекста, запись компактных
checkpoint, хуки жизненного цикла агентов, живая миграция и публичная alpha пока
не реализованы.

## [0.1.0.dev1] - 2026-09-07

### English

- Established the Personal Tideway pre-alpha repository baseline.
- Added the first v2 work package: versioned `config.yaml`, centralized data
  paths, deterministic configuration precedence, and schema validation.
- Added preflight path-boundary protection against traversal, invalid object
  types, and symlink escapes.
- Preserved the earlier tested MCP, rules, skills, conflict, backup, and
  dry-run engine as an implementation base.
- Added 78 automated tests, bilingual project documentation, roadmap, and
  versioning policy.

This version is a development foundation. Automatic project identity, Basic
Memory, cross-agent context retrieval, checkpoints, and live migration are not
yet implemented.

### Русский

- Создан pre-alpha фундамент репозитория Personal Tideway.
- Добавлен первый пакет v2: версионированный `config.yaml`, централизованные
  пути данных, определённый приоритет конфигурации и проверка схемы.
- Добавлена предварительная проверка границ путей, неправильных типов объектов
  и выхода через symlink.
- Ранний протестированный движок MCP, rules, skills, конфликтов, backup и
  dry-run сохранён как основа дальнейшей реализации.
- Добавлены 78 автоматических тестов, двуязычная документация, roadmap и
  правила версионирования.

Это фундамент для разработки. Автоматическое определение проектов, Basic
Memory, передача контекста между агентами, checkpoints и живая миграция пока не
реализованы.
