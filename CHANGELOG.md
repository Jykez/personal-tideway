# Changelog / Журнал изменений

Release notes follow the package version and are written in English and
Russian. Dates use `YYYY-MM-DD`.

Заметки о версиях соответствуют версии пакета и ведутся на английском и
русском языках. Даты записываются в формате `YYYY-MM-DD`.

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
