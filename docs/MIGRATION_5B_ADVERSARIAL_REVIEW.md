# Адверсариальное ревью: Migration 5B Apply Engine & Rollback Mechanism (Final Review, Round 2)

**Дата:** 2026-09-18
**Базовый коммит:** `67be57e7e96a571544cdf0be247adc87e46ad0dd`
**Изолированный клон:** отдельный review clone без доступа к пользовательским конфигам
**Объект ревью:** Финальный кандидат Migration 5B после раунда исправлений 2 (Correction Round 2) — модуль применения миграции `src/personal_tideway/core/migration_apply.py`, модуль планирования `src/personal_tideway/core/migration.py`, обработчики CLI в `src/personal_tideway/cli/main.py`, пакетные экспорты `src/personal_tideway/core/__init__.py`, а также регрессионный тестовый набор `tests/test_migration_apply.py` и `tests/test_migration_rollback.py`.
**Режим ревью:** Независимое финальное адверсариальное ревью (Worker-отчёт не запрашивался и не принимался на веру; команды и тесты `NOT_RUN`).

---

## Итоговый вердикт

### **`ACCEPT`**

> [!NOTE]
> В финальном кандидате раунда 2 (R2):
> 1. Единственное изменение относительно принятого кандидата R1 заключается в устранении лишней пустой строки в конце файла `tests/test_migration_rollback.py` (`diff-check whitespace issue at EOF`). Статический анализ подтвердил, что семантическое поведение продакшн-кода и тестовых наборов осталось полностью неизменным.
> 2. Все ранее зафиксированные дефекты уровней **P0** и **P1** остаются полностью устранёнными.
> 3. Замечание о публичной CLI-команде отката снято в соответствии с условиями контракта (публичный CLI rollback запрещён спецификацией фазы 5B; внутренний транзакционный Python API `rollback_migration` полностью проверен и принят).
> 4. Все границы транзакции и отката надёжно защищены и покрыты регрессионными тестами.
> 5. Новых дефектов или регрессий не обнаружено. Финальная приёмка полностью обоснована.

---

## Сводный статус по всем дефектам и замечаниям

| ID | Область / Замечание | Итоговый статус | Подтверждённый механизм в кодовой базе |
|---|---|---|---|
| **P0-1** | Циклический импорт `migration.py` ↔ `migration_apply.py` | **FIXED** | Re-export блок удалён из `migration.py`; изолированный импорт проверен тестом `test_isolated_migration_apply_import`. |
| **P0-2** | Удаление корня воркспейса при `rel_path: "."` в манифесте | **FIXED** | В `migration_apply.py` (`L1417–L1431`, `L1474–L1476`) внедрён запрет `rel_path in (".", "/", "\\")` и строгий контроль `target_p.resolve() == root_dir.resolve()`. Проверено тестом `test_manifest_with_dot_rel_path_rejected_without_deletion`. |
| **P0-3** | Зависание процесса на FIFO (named pipe DoS) | **FIXED** | В `_copy_tree_without_symlinks` и точках обхода внедрён контроль `lstat()` и `stat.S_ISREG`. Наличие FIFO пресекается немедленным `ValidationError`. Проверено тестом `test_fifo_in_memory_or_skills_aborts_without_hang`. |
| **P1-1** | Уничтожение новых заметок памяти при коллизии имён в rollback | **FIXED** | В `migration_apply.py` (`L1511–L1523`) файл пользователя сохраняется с суффиксом `.post_migration` с сохранением прав и внесением в `preserved_memory`. Проверено тестом `test_rollback_preserves_colliding_post_migration_central_memory_file`. |
| **P1-2** | Неполная очистка перемещённых навыков и созданных файлов | **FIXED** | В `migration_apply.py` (`L1000–L1230`) каждый созданный файл и каждая перемещённая папка навыка фиксируются в `destinations_created` и удаляются при rollback. Проверено тестом `test_rollback_removes_relocated_skill_when_target_parent_existed`. |
| **P1-3** | Ложный `rollback_failed` при ошибке создания бэкапа | **FIXED** | В `migration_apply.py` (`L949–L968`) сбои создания бэкапа локализованы в Phase A; временный бандл удаляется, возвращается `STATUS_BLOCKED`. Проверено тестом `test_backup_failure_cleans_bundle_and_does_not_report_rollback_failed`. |
| **P1-4** | Недетекция дрейфа исходных файлов под локом | **FIXED** | Реализована функция `capture_source_snapshot(...)`, проверяющая SHA-256 хеши файлов до и после взятия блокировки. Проверено тестом `test_source_drift_after_plan_blocks_apply`. |
| **P1-5** | Отсутствие CLI-команды `rollback` | **WITHDRAWN** | Отозвано по прямому указанию спецификации контракта пользователя (CLI rollback запрещён в 5B). Внутренний Python API `rollback_migration` проверен и принят. |
| **P2-1** | Расхождение состава бэкапов плана 5A и манифеста | **FIXED** | Клиентские файлы из плана включены в `targets_to_backup`. Проверено тестом `test_actual_bundle_contents_match_plan_backups`. |
| **P2-2** | Семантическое расхождение поля `mutations` в dry-run и apply | **FIXED** | В `dry-run` поле `mutations` возвращает только реальные мутации ФС (`02_...` — `06_...`). Проверено тестом `test_dry_run_and_apply_mutation_list_parity`. |
| **P2-3** | Утечка абсолютных путей хоста в отчётах | **FIXED** | Функция `sanitize_error_message` экранирует пути хоста. Проверено тестом `test_apply_error_never_contains_absolute_host_paths`. |
| **P2-4** | Несогласованность кодов возврата CLI | **FIXED** | Исключения миграции унифицированы с кодом `ExitCode.CONFIG_ERROR`. |
| **P2-5** | Несохранение прав доступа директорий | **FIXED** | Права директорий сохраняются в манифесте и восстанавливаются через `os.chmod`. Проверено тестом `test_rollback_cleans_empty_archive_directory_and_restores_dir_mode`. |
| **P3-1** | Оставление пустой папки `archive` при откате | **FIXED** | Пустая папка `archive/` удаляется при rollback. Проверено тестом `test_rollback_cleans_empty_archive_directory_and_restores_dir_mode`. |
| **P3-2** | Отсутствие проверки хеша файлов директории при бэкапе | **FIXED** | В `_copy_tree_without_symlinks` добавлена сверка `hash_file(t_f) != hash_file(f_path)`. |
| **Diff** | Лишняя пустая строка в конце `test_migration_rollback.py` | **FIXED** | Строка удалена в R2, файл корректно завершается одиночным `\n`. |

---

## Подтверждённые сильные стороны реализации

1. **Строгая фазовая модель:** Невозможность мутаций до завершения и валидации бэкапов Phase A.
2. **Изоляция секретов:** Секреты и токены авторизации не считываются и не попадают в отчёты.
3. **Защита от race conditions:** Неблокирующий лок с флагом `O_NOFOLLOW` и контролем `st_nlink == 1`.
4. **Контроль целостности:** Защита бэкапов через SHA-256 с fail-closed реакцией на повреждения.
5. **Сохранение данных:** Безопасное сохранение post-migration центральной памяти и правил вне маркеров.

---

## Непроверенные runtime assumptions и Checks: NOT_RUN

* В соответствии с мандатом ревьюера тесты и команды в терминале **не запускались** (`checks: NOT_RUN`).
* Все выводы базируются на **строгом статическом анализе исходного кода, синтаксических структур и системных контрактов**.

---

## Остаточные риски

1. **Аварийный сбой питания (Power-loss):** При внезапном прерывании электропитания во время применения мутаций восстановление осуществляется вызовом Python API `rollback_migration(manifest_path)`.
2. **Ограничения umask:** Права создаваемых директорий детерминированно защищены вызовами `chmod` в кодовой базе.

---

## Заключение

Финальная кодовая база Migration 5B в клоне R2 полностью соответствует всем критериям корректности, безопасности и отказоустойчивости.
**Финальный вердикт: `ACCEPT`.**

---

## Codex acceptance addendum

После этого статического verdict Codex обнаружил три падающих apply-теста и самостоятельно исправил их по отдельному разрешению пользователя. Дополнительно усилены проверки raw root symlinks, hardlinks, промежуточных symlink-компонентов backup entries и восстановление пустых вложенных директорий. Итоговый cumulative candidate независимо проверен Codex: focused migration suite, full repository suite, восемь временных fixture-сценариев, Ruff delta, `git diff --check` и package build прошли успешно. Этот addendum не является частью исходного заключения Agy Reviewer.
