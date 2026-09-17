# Changelog / Журнал изменений

Release notes follow the package version and are written in English and
Russian. Dates use `YYYY-MM-DD`.

Заметки о версиях соответствуют версии пакета и ведутся на английском и
русском языках. Даты записываются в формате `YYYY-MM-DD`.

## [0.1.0.dev10] - 2026-09-17

### English

- Completed the Projection Parity milestone: implemented read-only projection parity evaluation in `ptw status` across MCP, rules, and skills for both Codex and Antigravity (`agy`).
- Added deterministic, typed evaluation returning fine-grained statuses per client and component: `in_sync`, `drift_detected`, `conflict`, `invalid`, `empty`, and `uninitialized`.
- Provided safe, actionable remediation guidance in console text and structured JSON without mutating any client configurations, state, or backups during status evaluation.
- Preserved unrelated client data: non-canonical user MCP servers, rules content outside managed `<!-- PAIW:START -->` / `<!-- PAIW:END -->` markers, and independent/untracked client skills remain untouched and do not cause false drift.
- Enforced robust edge-case handling: safe capture of malformed TOML/JSON configurations and invalid rule markers reporting `invalid` instead of crashing; detection of symlink hazards (broken or escaping links), foreign skill scope leakage between clients, and deletion of canonical PTW skills tracked via `state.json`.
- Maintained backward compatibility with `portable_mcp_parity` while providing comprehensive multidimensional projection coverage.
- Conducted an adversarial review ([PROJECTION_PARITY_ADVERSARIAL_REVIEW.md](docs/PROJECTION_PARITY_ADVERSARIAL_REVIEW.md)) analyzing 8 threat vectors: false in_sync detection, zero mutation / read-only guarantees, unrelated data preservation, secret redaction in status reporting, malformed configs and markers, symlink hazards and scope leakage, conflict precedence over drift, and execution determinism.
- Note on scope: this milestone provides read-only evaluation of projection parity alongside existing explicit synchronization (`ptw sync`); it does not implement or claim live background synchronization or real-client runtime proof.
- Independent Codex verification (2026-09-17): 11 focused projection parity tests passed, full 560-test suite passed without regressions, Ruff checks on `status.py` and projection parity tests passed, `git diff --check` passed, and `uv build` passed.

### Русский

- Завершен этап Projection Parity (Паритет проекций): реализован механизм оценки паритета проекций в режиме read-only в `ptw status` по трем осям (MCP, правила и навыки) для Codex и Antigravity (`agy`).
- Добавлена детерминированная типизированная оценка с гранулярными статусами для каждого клиента и компонента: `in_sync`, `drift_detected`, `conflict`, `invalid`, `empty` и `uninitialized`.
- Обеспечен вывод безопасных рекомендаций по устранению расхождений (remediation) в консольном выводе и структурированном JSON без каких-либо мутаций клиентских конфигураций, состояния или бэкапов в процессе проверки статуса.
- Гарантирована сохранность несвязанных данных клиентов: неканонические пользовательские MCP-серверы, текст правил вне управляемых маркеров `<!-- PAIW:START -->` / `<!-- PAIW:END -->` и независимые навыки клиентов остаются нетронутыми и не вызывают ложного дрифта.
- Реализована устойчивая обработка краевых случаев: безопасный перехват синтаксических ошибок в TOML/JSON-конфигурациях и поврежденных маркеров правил со статусом `invalid` без сбоев; детекция угроз символических ссылок (висячие ссылки, выход за границы), утечек скоупов навыков между клиентами и удалений канонических навыков PTW с отслеживанием по `state.json`.
- Сохранена обратная совместимость с `portable_mcp_parity` наряду с предоставлением многомерного отчета по всем типам проекций.
- Проведено адверсариальное ревью ([PROJECTION_PARITY_ADVERSARIAL_REVIEW.md](docs/PROJECTION_PARITY_ADVERSARIAL_REVIEW.md)) с анализом 8 векторов угроз: ложный статус in_sync, гарантии отсутствия мутаций (read-only), сохранность посторонних данных, скрытие секретов в отчете, некорректные конфиги и маркеры, риски symlink и утечки скоупов, безусловный приоритет конфликтов над дрифтом и детерминизм вычислений.
- Ограничение области: данный этап обеспечивает read-only оценку паритета проекций в дополнение к существующей явной синхронизации (`ptw sync`); фоновая синхронизация в реальном времени и подтверждение на живых рантаймах клиентов не заявляются и не реализуются.
- Независимая верификация Codex (17.09.2026): 11 сфокусированных тестов паритета проекций пройдены, полный набор из 560 тестов пройден без регрессий, проверки Ruff для `status.py` и тестов паритета проекций пройдены, `git diff --check` пройден, сборка `uv build` успешна.

## [0.1.0.dev9] - 2026-09-16

### English

- Implemented Phase 4C-C: explicit checkpoint and context handoff slice between Codex and Antigravity (`agy`).
- Added explicit, scriptable checkpointing protocol via `ptw checkpoint` accepting structured JSON payloads over stdin (`<<'EOF'`) with semantic validation, size limits, secret rejection, and safe fail-closed error handling.
- Implemented automated cross-client integration coverage demonstrating end-to-end handoff: Codex session -> explicit checkpoint -> agy `PreInvocation` startup -> agy checkpoint update with blocker -> Codex `SessionStart` startup receiving updated objective, decisions, blockers, and next safe action.
- Added comprehensive integration tests covering idempotence (semantic fingerprint matching prevents redundant backend writes), cross-project isolation (strict single-project boundary prevents context leakage), backend failure handling (fail-closed probe error without corrupted state), and security filtering (rejection of control characters, newline abuse, and credentials).
- Conducted an adversarial review ([CHECKPOINT_ADVERSARIAL_REVIEW.md](docs/CHECKPOINT_ADVERSARIAL_REVIEW.md)) analyzing 8 threat vectors: hostile project/path traversal, hostile payload/secrets, source spoofing, backend failure, idempotency, cross-project leak, false automatic Stop claims, and memory prompt injection.
- Honestly documented the absence of native automatic Stop/exit hooks in both Codex and agy; checkpoint capture remains explicit and manual/instructed via policy/skill prompts rather than native automated capture.
- Independent Codex validation passed: 5 focused handoff tests and full 549-test suite passed without regressions.

### Русский

- Реализована Фаза 4C-C: срез явного сохранения чекпоинта и передачи контекста (handoff) между Codex и Antigravity (`agy`).
- Добавлен явный скриптуемый протокол чекпоинтов через `ptw checkpoint`, принимающий структурированный JSON-payload через stdin (`<<'EOF'`) со строгой валидацией, лимитами размера, блокировкой секретов и гарантией fail-closed при сбоях.
- Реализовано автоматизированное сквозное интеграционное покрытие передачи контекста между клиентами: сессия Codex -> явный чекпоинт -> запуск agy через `PreInvocation` -> обновление чекпоинта из agy с фиксацией блокера -> старт сессии Codex через `SessionStart` с получением обновленного объектива, решений, блокеров и следующего безопасного шага.
- Добавлены всесторонние интеграционные тесты, покрывающие идемпотентность (совпадение семантического отпечатка предотвращает повторные записи в бэкенд), изоляцию между проектами (строгие границы единичного проекта исключают утечку контекста), отказоустойчивость бэкенда (гарантия fail-closed при сбое записи без повреждения состояния) и фильтрацию безопасности (отсечение управляющих символов, переводов строк и учетных данных).
- Проведено адверсариальное ревью ([CHECKPOINT_ADVERSARIAL_REVIEW.md](docs/CHECKPOINT_ADVERSARIAL_REVIEW.md)) с анализом 8 векторов угроз: враждебные пути и path traversal, опасная нагрузка и секреты, подделка источника клиента, сбои бэкенда, идемпотентность, утечки между проектами, ложные утверждения об авто-Stop и внедрение инструкций из памяти.
- Честно зафиксировано отсутствие нативных автоматических Stop/exit-хуков в Codex и agy; фиксация чекпоинта остается явной и обеспечивается на уровне инструкций/политик (manual/instructed) без вымышленных автоматических триггеров.
- Независимая валидация Codex подтвердила успешное прохождение: 5 сфокусированных тестов handoff и полный набор из 549 тестов пройдены без регрессий.

## [0.1.0.dev8] - 2026-09-15

### English

- Implemented Phase 4C-B: safe Codex SessionStart lifecycle hook provisioning and handler support.
- Added canonical hook group appended under `hooks.SessionStart` in Codex `hooks.json` with exact matcher `^(startup|resume|clear|compact)$`, command `ptw hook codex-session-start`, timeout 30, and additionalContextLimit 2500.
- Implemented `ptw hook codex-session-start` handler that reads bounded UTF-8 JSON payload from stdin, requires `hook_event_name == "SessionStart"`, `source` in `startup/resume/clear/compact`, and nonempty absolute `cwd` without NUL bytes; strictly resolves registered projects without auto-registration, retrieves bounded Personal Tideway context once, sanitizes all errors, never reads `transcript_path`, and returns exactly `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": rendered}}`. Validation failure makes zero backend calls.
- Extended lifecycle CLI commands (`ptw hook status`, `ptw hook plan`, `ptw hook install [--dry-run]`, `ptw hook remove [--dry-run]`) to support `--client {agy, codex}` with agy remaining the default.
- Added global `--codex-hooks` CLI flag, `custom_codex_hooks` configuration field, default path `~/.codex/hooks.json`, and `client_paths` persistence.
- Enforced strict protection of Codex `hooks.json`: semantic preservation of unrelated top-level keys, hook events, and array items; atomic writes; backups created before modifications; and refusal of oversized files, duplicate object keys, malformed JSON, non-object roots, symlink hazards, non-object `hooks`, or non-array `SessionStart`.
- Added detection of inline Tideway command collisions recursively below top-level `[hooks]` in `config.toml` using `tomlkit`; malformed/unreadable `config.toml` or collisions report conflict without ever mutating `config.toml` or `hooks.state`.
- Added typed structural evidence (`CodexHookEvidence`) surfaced in `ptw status` and `ptw doctor`, while strictly preserving continuity assurance levels (`instructed`, `manual`, `unavailable`). Verified disposable capability probe passed independently on 2026-09-15 (see [CODEX_HOOK_PROBE.md](docs/CODEX_HOOK_PROBE.md)) validating negative control, interactive TUI trust review, single backend invocation, and exact model marker for startup without security bypass; agy 1.2.2 disposable probe already passed earlier, and Codex `/hooks` review does not apply to agy. Completion of this probe finishes the Phase 4C-B slice; agent integration overall remains partial, and stop/checkpoint lifecycle integration is explicitly scheduled as the next separate slice.

### Русский

- Реализована Фаза 4C-B: безопасная инициализация и обработка нативного хука жизненного цикла SessionStart для Codex.
- Добавлена каноническая группа хуков, добавляемая в `hooks.SessionStart` файла `hooks.json` Codex с точным matcher `^(startup|resume|clear|compact)$`, командой `ptw hook codex-session-start`, timeout 30 и additionalContextLimit 2500.
- Реализован обработчик `ptw hook codex-session-start`, считывающий ограниченный UTF-8 JSON-payload из stdin, требующий `hook_event_name == "SessionStart"`, `source` из `startup/resume/clear/compact` и непустой абсолютный `cwd` без NUL-байтов; строго разрешает зарегистрированный проект без авторегистрации, единожды запрашивает ограниченный контекст Personal Tideway, санитизирует ошибки, никогда не читает `transcript_path` и возвращает строго `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": rendered}}`. Ошибки валидации выполняют ровно 0 обращений к бэкенду.
- Расширены команды CLI управления жизненным циклом (`ptw hook status`, `ptw hook plan`, `ptw hook install [--dry-run]`, `ptw hook remove [--dry-run]`) для поддержки `--client {agy, codex}`, где agy остаётся клиентом по умолчанию.
- Добавлен глобальный флаг CLI `--codex-hooks`, поле конфигурации `custom_codex_hooks`, путь по умолчанию `~/.codex/hooks.json` и сохранение в `client_paths`.
- Обеспечена строгая защита `hooks.json` Codex: семантическое сохранение посторонних ключей, событий и элементов списков; атомарная запись; создание бэкапов перед изменениями; отказ от работы при файлах чрезмерного размера, дубликатах ключей, некорректном JSON, корнях-не-объектах, рисках симлинков, `hooks`-не-объектах или `SessionStart`-не-массивах.
- Добавлено рекурсивное обнаружение коллизий команды Tideway под секцией верхнего уровня `[hooks]` в `config.toml` через `tomlkit`; некорректный/нечитаемый `config.toml` или коллизии вызывают конфликт без изменения `config.toml` или `hooks.state`.
- Добавлены типизированные структурные свидетельства (`CodexHookEvidence`), отображаемые в `ptw status` и `ptw doctor`, при этом уровни continuity assurance строго сохраняются (`instructed`, `manual`, `unavailable`). Одноразовый зонд функциональности успешно пройден независимо 15.09.2026 (см. [CODEX_HOOK_PROBE.md](docs/CODEX_HOOK_PROBE.md)) с подтверждением отрицательного контроля, проверки trust в TUI, единственного вызова бэкенда и точного маркера модели для startup без обхода защит; одноразовый зонд agy 1.2.2 был успешно пройден ранее, а процедура ревью `/hooks` к agy не применяется. Завершение зонда финализирует срез Фазы 4C-B; общая интеграция агентов остаётся в статусе Partial, а жизненный цикл stop/checkpoint вынесен в следующий отдельный этап.

## [0.1.0.dev7] - 2026-09-14

### English

- Implemented first Phase 4C capability: safe Antigravity CLI (agy) initial-context lifecycle hook provisioning and handler support.
- Added managed named hook definition `personal-tideway` for agy's native `PreInvocation` event in `hooks.json`.
- Implemented `ptw hook agy-preinvocation` handler that reads agy's documented JSON payload from stdin. PreInvocation fires before model calls; when `invocationNum == 0` (the first model invocation in the current invocation sequence/execution loop), it resolves project context without auto-registration and injects bounded Personal Tideway context into a single `ephemeralMessage`.
- On subsequent invocations (`invocationNum > 0`), the handler returns a valid empty/no-op JSON response (`{"injectSteps": []}`) immediately without reading context.
- Added explicit CLI lifecycle management: `ptw hook status`, `ptw hook plan`, `ptw hook install [--dry-run]`, and `ptw hook remove [--dry-run]` supporting text and single-document `--json` outputs.
- Enforced strict protection of agy's `hooks.json`: semantic preservation of unrelated top-level hooks and unknown JSON keys, atomic writes, automatic backups on mutation, and refusal of oversized files, duplicate object keys, malformed JSON, non-object roots, symlink hazards, or conflicting user modifications.
- Added typed structural evidence/status (`HookStatus`, `AgyHookEvidence`) exposed in `ptw status` and `ptw doctor`, while strictly preserving continuity assurance levels (`instructed`, `manual`, `unavailable`) and never claiming `hooked` without a real quota-consuming behavioral probe.

This slice delivers the initial Phase 4C-A agy lifecycle hook; real behavioral client probes and Codex lifecycle hooks remain under construction.

### Русский

- Реализована первая возможность Фазы 4C: безопасная инициализация и обработка нативного хука начального контекста для Antigravity CLI (agy).
- Добавлено управляемое именованное определение хука `personal-tideway` для нативного события `PreInvocation` agy в `hooks.json`.
- Реализован обработчик `ptw hook agy-preinvocation`, считывающий документированный JSON-payload agy из stdin. Событие PreInvocation срабатывает перед вызовами модели; при `invocationNum == 0` (первый вызов модели в текущей последовательности вызовов / цикле выполнения) обработчик строго разрешает контекст проекта без авторегистрации и передает ограниченный контекст Personal Tideway в одном `ephemeralMessage`.
- При последующих вызовах (`invocationNum > 0`) обработчик немедленно возвращает валидный пустой no-op JSON-ответ (`{"injectSteps": []}`) без чтения контекста.
- Добавлено явное CLI-управление жизненным циклом: `ptw hook status`, `ptw hook plan`, `ptw hook install [--dry-run]` и `ptw hook remove [--dry-run]` с поддержкой текстового вывода и единого `--json` документа.
- Обеспечена строгая защита `hooks.json` agy: семантическое сохранение посторонних хуков и неизвестных JSON-ключей, атомарная запись, автоматическое создание бэкапов при изменениях и отказ от перезаписи при файлах чрезмерного размера, дубликатах ключей, некорректном JSON, корнях-не-объектах, рисках симлинков или конфликтующих правках пользователя.
- Добавлены типизированные структурные свидетельства и статус (`HookStatus`, `AgyHookEvidence`), отображаемые в `ptw status` и `ptw doctor`, при этом уровни continuity assurance строго сохраняются (`instructed`, `manual`, `unavailable`) и статус `hooked` никогда не выдается без реального поведенческого зонда с расходом квоты.

Этот срез завершает начальный этап Фазы 4C-A для agy; реальный поведенческий зонд и хуки жизненного цикла Codex остаются в разработке.

## [0.1.0.dev6] - 2026-09-13

### English

- Added canonical shared continuity policy (`rules/shared/continuity.md`) and minimal shared continuity skill (`skills/shared/continuity/SKILL.md`) provisioned on `ptw init` into Personal Tideway workspace state without overwriting existing user edits.
- Reused existing explicit `ptw sync`, adapter, backup, marker, and skill-link mechanisms to project continuity rules and skills to configured Codex and agy targets without claiming modern projection parity or native discovery.
- Implemented small typed assurance model (`ContinuityAssuranceLevel`, `ClientContinuityAssurance`, `ContinuityAssuranceReport`) and deterministic evaluation based on evidence of managed rules, skills, and backend usability.
- Explicitly documented and enforced assurance levels: `hooked`, `instructed`, `manual`, and `unavailable`. Never emits `hooked` without verified deterministic lifecycle hooks.
- Surfaced continuity assurance levels and details in `ptw status` and `ptw doctor` in both human-readable text and structured single-document JSON output.
- Always-on continuity policy stays compact (under 50 lines) covering context recall, search before guessing, checkpoint triggers, concise retention, no secrets/transcripts, verified persistence, and pointer to the on-demand skill.
- Added comprehensive automated test suite covering provisioning, idempotency, non-overwrite, dry-run zero mutation, cross-client projections, downgrade scenarios, and absence of false hook claims.

This slice delivers Phase 4B; native agent lifecycle hooks remain under investigation.

### Русский

- Добавлена каноническая общая политика непрерывности (`rules/shared/continuity.md`) и минимальный общий навык (`skills/shared/continuity/SKILL.md`), создаваемые при `ptw init` в воркспейсе Personal Tideway без перезаписи существующих правок пользователя.
- Задействованы существующие механизмы явного `ptw sync`, адаптеров, бэкапов, маркеров и связывания навыков для проекции правил и навыков на сконфигурированные цели Codex и agy без утверждений о современном паритете проекций или нативном обнаружении.
- Реализована компактная типизированная модель assurance (`ContinuityAssuranceLevel`, `ClientContinuityAssurance`, `ContinuityAssuranceReport`) и детерминированная оценка на основе свидетельств доступности управляемых правил, навыков и работоспособности бэкенда.
- Четко определены и соблюдаются уровни assurance: `hooked`, `instructed`, `manual` и `unavailable`. Уровень `hooked` никогда не выдается без проверенных нативных хуков жизненного цикла.
- Статус и детали assurance для клиентов отображаются в выводах `ptw status` и `ptw doctor` как в текстовом виде, так и в структурированном JSON.
- Всегда активная политика непрерывности остается компактной (менее 50 строк) и охватывает восстановление контекста, поиск перед догадками, триггеры чекпоинтов, лаконичность, запрет секретов/транскриптов, проверенную запись и ссылку на навык.
- Добавлен полный набор автоматических тестов, проверяющих инициализацию, идемпотентность, сохранение правок пользователя, нулевую мутацию при dry-run, проекции на обоих клиентов, сценарии даунгрейда и отсутствие ложных заявлений о хуках.

Этот срез завершает Фазу 4B; нативные хуки жизненного цикла агентов остаются в исследовании.

## [0.1.0.dev5] - 2026-09-12

### English

- Implemented safe CLI bridge commands for context retrieval and project checkpoints
  (`ptw context show`, `ptw context search QUERY`, `ptw checkpoint`).
- Context show/search commands expose `--project`, `--budget` (character cap), `--limit`
  (search items cap), and `--json` options backed by the bounded retrieval core API.
- Checkpoint command accepts structured UTF-8 JSON from either `--file` or `--stdin`
  with strict mutual exclusivity and a 64 KiB conservative cap before JSON parsing.
- Enforced strict project selection precedence: command-local `--project` > global/config
  selection > strict current working directory resolution without auto-registration.
- Checkpoint output accurately distinguishes written, unchanged, and dry-run results
  in both human-readable text and structured single-document `--json` format with source client attribution.
- Hardened against TOCTOU path races, symlinks, non-regular files, malformed JSON/UTF-8,
  secret patterns, and host path disclosure in diagnostics and JSON errors.
- Added comprehensive CLI unit tests with mocked Basic Memory runner boundaries.

This slice delivers the Phase 4A CLI bridge; native agent lifecycle hooks, shared
continuity rules, and skills assurance remain under construction.

### Русский

- Реализованы команды безопасного CLI-моста для получения контекста и сохранения
  чекпоинтов проекта (`ptw context show`, `ptw context search QUERY`, `ptw checkpoint`).
- Команды context show/search поддерживают `--project`, `--budget` (лимит символов),
  `--limit` (лимит элементов поиска) и `--json` на базе API ограниченного чтения контекста.
- Команда checkpoint принимает структурированный UTF-8 JSON через `--file` или `--stdin`
  со строгой взаимной исключительностью и консервативным лимитом 64 КиБ до парсинга JSON.
- Установлен строгий приоритет выбора проекта: локальный `--project` > глобальный
  выбор/конфиг > строгое определение по текущей директории без авторегистрации.
- Вывод checkpoint точно различает состояния `written`, `unchanged` и `dry-run`
  как в текстовом виде, так и в формате единого JSON-документа (`--json`) с указанием source_client.
- Реализована защита от гонок TOCTOU, символических ссылок, нерегулярных файлов,
  некорректного JSON/UTF-8, паттернов секретов и утечки путей хоста в ошибках.
- Добавлены полные модульные CLI-тесты с мокированием границ Basic Memory runner.

Этот срез завершает CLI-мост Фазы 4A; нативные хуки жизненного цикла агентов, общие
правила непрерывности и обеспечение навыков остаются в разработке.

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
