# Changelog / Журнал изменений

Release notes follow the package version and are written in English and
Russian. Dates use `YYYY-MM-DD`.

Заметки о версиях соответствуют версии пакета и ведутся на английском и
русском языках. Даты записываются в формате `YYYY-MM-DD`.

## [0.1.0.dev4] - 2026-09-12

### English

- Added concise, bounded project checkpoint writes through Basic Memory 0.23.2.
- Checkpoints use a semantic fingerprint and a per-project lock, so identical
  state is skipped without changing its timestamp.
- Reads fail closed on errors or malformed responses; only the verified
  Basic Memory missing-note response with null content permits a new write.
- Dry-run performs no executable probe, clock read, lock creation, runner call,
  or filesystem mutation.
- Added bounded no-shell subprocess I/O, strict project resolution, secret-safe
  validation and errors, and hostile lock-file checks.
- Incompatible custom write runners are rejected before invocation instead of
  silently dropping checkpoint content from stdin.
- 407 automated tests pass together with focused lint, type, compile, secret
  scans, and independent adversarial agy review.

This checkpoint completes the core read/write context loop. CLI commands,
shared continuity rules, and Codex/agy lifecycle integration are the next work
package; live migration remains unimplemented.

### Русский

- Добавлена компактная ограниченная запись состояния проекта через Basic
  Memory 0.23.2.
- Семантический fingerprint и отдельная блокировка проекта позволяют пропускать
  неизменившееся состояние без обновления timestamp.
- Ошибки и некорректные ответы чтения обрабатываются в режиме fail-closed; новую
  запись разрешает только проверенный ответ Basic Memory с пустым содержимым.
- Dry-run не запускает проверку executable, часы, блокировку, runner и не меняет
  файловую систему.
- Добавлены ограниченный no-shell ввод-вывод, строгий выбор проекта, безопасные
  проверки секретов и защита от враждебных lock-файлов.
- Несовместимый пользовательский write-runner отклоняется до запуска и больше не
  может молча потерять checkpoint, передаваемый через stdin.
- Успешно проходят 407 автоматических тестов, отдельные lint, type и compile
  проверки, secret-scan и независимое адверсариальное ревью agy.

Эта версия завершает базовый цикл чтения и записи контекста. Следующий пакет —
CLI-команды, общие правила непрерывности и подключение к жизненному циклу Codex
и agy; живая миграция пока не реализована.

## [0.1.0.dev3] - 2026-09-11

### English

- Added read-only, bounded project context retrieval through Basic Memory
  0.23.2 with deterministic dry-run previews.
- Current-state notes are selected only by an actual `current-state` permalink;
  unrelated search results are never promoted.
- Search results are validated, deduplicated, ranked, and constrained by item,
  character, and two-megabyte subprocess-output limits.
- Hardened query, permalink, and note-identifier handling against command-line
  flag injection and path traversal.
- Added sanitized failure boundaries that distinguish search from read-note
  errors without exposing queries, note contents, paths, stderr, or secrets.
- 366 passing automated tests, focused type checks, lint, and an independent
  adversarial review.

This checkpoint completes the retrieval half of the context loop. Concise,
idempotent checkpoint writing is the next work package; lifecycle hooks and live
migration remain unimplemented.

### Русский

- Добавлено доступное только для чтения получение ограниченного проектного
  контекста через Basic Memory 0.23.2 с детерминированным dry-run preview.
- Текущим состоянием признаётся только заметка с настоящим permalink
  `current-state`; посторонние результаты поиска не повышаются автоматически.
- Результаты поиска валидируются, дедуплицируются, ранжируются и ограничиваются
  по числу элементов, символам и двум мегабайтам вывода дочернего процесса.
- Обработка запросов, permalink и идентификаторов заметок защищена от подмены
  флагов командной строки и обхода путей.
- Ошибки search и read-note различаются, но не раскрывают запросы, содержимое
  заметок, пути, stderr или секреты.
- Успешно проходят 366 автоматических тестов, отдельная проверка типов, lint и
  независимое adversarial review.

Этот чекпоинт завершает половину контекстного цикла, отвечающую за чтение.
Следующий пакет — компактная идемпотентная запись checkpoint; lifecycle hooks и
живая миграция пока не реализованы.

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
