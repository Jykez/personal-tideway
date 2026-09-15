# Codex SessionStart Hook Probe Record / Протокол зонда хука SessionStart Codex

**Date / Дата:** 2026-09-15

**Client Version / Версия клиента:** Codex CLI 0.152.0

**Phase / Этап:** Phase 4C-B Disposable Capability Verification / Верификация одноразовой функциональности Фазы 4C-B

---

## English

### Executive Summary

On 2026-09-15, an independent disposable behavioral probe successfully verified the end-to-end execution of the Personal Tideway `SessionStart` lifecycle hook handler within Codex CLI 0.152.0 without security bypasses.

### Test Environment & Isolation

- **Temporary directories:** Disposable `HOME`, `CODEX_HOME`, and `PERSONAL_TIDEWAY_HOME` with a registered Git test project. Live user configurations and live client state were untouched.
- **Handler binary:** Repository `.venv/bin/ptw` invoked through a transparent temporary `PATH` wrapper forwarding `stdin` byte-for-byte.
- **Hook specification:** Canonical hook group in `$CODEX_HOME/hooks.json` under event `SessionStart` with matcher `^(startup|resume|clear|compact)$` and command `ptw hook codex-session-start`.
- **`hooks.json` SHA256:** `055fd33b8234f12fa24aa2a4f628a635a64126a4c50b69c782ba946fe2777440`.

### Negative Control

- **Execution:** Real Codex CLI invoked in non-interactive read-only mode (`-a never exec --ephemeral --json`) prior to granting project trust.
- **Result:** Codex emitted `NO_INITIAL_CONTEXT`. Exactly zero hook executions occurred, and exactly zero calls were made to the context backend.

### Trust Review & Security

- **Interactive TUI review:** The project was marked trusted, prompting `Hooks need review (single hook)`. Review was completed in the disposable environment under user authorization with `Trust all and continue` selected.
- **Digest calculation:** Codex CLI natively computed and persisted the approval hash `sha256:abc2392056c9514c6cbccd09e703de5c4a3e205f7856a1f780ad3ccdb3d9c974`. The Personal Tideway installer never forged or wrote trust hashes.

### Positive Behavioral Verification

- **Execution:** Real Codex CLI invoked with `-a never exec --ephemeral --json` without bypass.
- **Payload delivery:** Handler received a `SessionStart` payload with source `startup` and the exact project `cwd`.
- **Backend call count:** Instrumented mock Basic Memory protocol backend was invoked exactly once for project-scoped `current-state`.
- **Model response:** The model emitted the exact context marker `PTW_CODEX_4CB_92E8B6D1`. The marker was absent from the prompt. While ordinary tools were available to the session, the prompt explicitly prohibited tool and file usage, and execution logs confirmed exactly zero model tool calls.

### Teardown & Scope Boundaries

- **Cleanup:** The hook was cleanly removed via `ptw hook remove --client codex` (status verified as `not-installed`). Temporary credentials and configuration copies were deleted.
- **Behavioral scope:** Verified for the `startup` source. The `resume`, `clear`, and `compact` sources match the regex structurally but were not behaviorally exercised.
- **Assurance status:** This probe confirms disposable execution capability. Evaluator levels remain `instructed`, `manual`, or `unavailable`; `hook_verified` remains `False` in general evaluation until live client installation, user trust, digest matching, and active behavioral evidence are established.

---

## Русский

### Краткое резюме

15.09.2026 независимый одноразовый поведенческий зонд успешно подтвердил сквозное выполнение обработчика хука жизненного цикла `SessionStart` Personal Tideway в Codex CLI 0.152.0 без обхода механизмов безопасности.

### Тестовое окружение и изоляция

- **Временные каталоги:** Изолированные временные `HOME`, `CODEX_HOME` и `PERSONAL_TIDEWAY_HOME` с зарегистрированным тестовым Git-проектом. Рабочие файлы пользователя и живые конфигурации клиентов не затрагивались.
- **Исполняемый файл:** Бинарник `.venv/bin/ptw` вызывался через прозрачную временную обёртку в `PATH`, передающую `stdin` байт-в-байт.
- **Конфигурация хука:** Каноническая группа в `$CODEX_HOME/hooks.json` для события `SessionStart` с матчером `^(startup|resume|clear|compact)$` и командой `ptw hook codex-session-start`.
- **SHA256 `hooks.json`:** `055fd33b8234f12fa24aa2a4f628a635a64126a4c50b69c782ba946fe2777440`.

### Отрицательный контроль

- **Запуск:** Реальный Codex CLI запущен в неинтерактивном режиме (`-a never exec --ephemeral --json`) до подтверждения доверия проекту.
- **Результат:** Codex вернул `NO_INITIAL_CONTEXT`. Зафиксировано ровно 0 вызовов хука и ровно 0 обращений к бэкенду контекста.

### Подтверждение доверия и безопасность

- **Интерактивный TUI:** Проект помечен как доверенный, после чего отобразилось окно `Hooks need review (single hook)` с выбором `Trust all and continue` (проверка выполнена в одноразовом окружении с авторизации пользователя).
- **Расчёт дайджеста:** Codex CLI нативно вычислил и сохранил хеш доверия `sha256:abc2392056c9514c6cbccd09e703de5c4a3e205f7856a1f780ad3ccdb3d9c974`. Установщик Personal Tideway не подделывал и не записывал хеши доверия.

### Положительная поведенческая верификация

- **Запуск:** Реальный Codex CLI запущен с теми же флагами (`-a never exec --ephemeral --json`) без обхода защит.
- **Передача данных:** Обработчик получил payload `SessionStart` с источником `startup` и точным `cwd` проекта.
- **Обращения к бэкенду:** Инструментированный мок-бэкенд протокола Basic Memory был вызван ровно 1 раз для получения `current-state` проекта.
- **Ответ модели:** Модель вернула точный маркер контекста `PTW_CODEX_4CB_92E8B6D1`. Маркер отсутствовал в исходном запросе. Хотя стандартные клиентские инструменты были доступны в сессии, запрос явно запрещал вызов инструментов и чтение файлов, а логи выполнения зафиксировали ровно 0 вызовов инструментов моделью.

### Очистка и границы применимости

- **Очистка:** Хук чисто удалён через `ptw hook remove --client codex` (статус подтверждён как `not-installed`). Временные авторизационные данные и копии конфигурации удалены.
- **Границы:** Проверено только поведение для источника `startup`. Источники `resume`, `clear` и `compact` валидны структурно по regex, но в данном зонде поведенчески не вызывались.
- **Статус assurance:** Зонд подтверждает возможность работы в изолированном окружении. Уровнями evaluator остаются `instructed`, `manual` или `unavailable`; флаг `hook_verified` остаётся `False` до появления постоянной живой установки, доверия пользователя, совпадения дайджеста и актуальных свидетельств выполнения.
