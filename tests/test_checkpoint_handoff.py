"""Интеграционные тесты жизненного цикла checkpoint/handoff между Codex и Antigravity (agy).

Проверяемые сценарии:
1. Полный сквозной цикл handoff:
   - Codex сохраняет чекпоинт с завершенной задачей (source_client="codex", status="verified").
   - Повторный вызов сохраняет идемпотентность (skip write, unchanged=True, счетчик runner write не растет).
   - Antigravity (agy) запускается в проекте и получает начальный контекст через реальный
     обработчик `handle_agy_preinvocation` (видны objective, decisions, next action, client=codex).
   - agy выполняет работу и сохраняет обновленный чекпоинт с частичным прогрессом и блокером
     (source_client="agy", status="partial", blockers=[...], next_safe_action="...").
   - Codex запускает новую сессию и через реальный `handle_codex_session_start` получает
     обновленный контекст (видны новый objective, новые решения, блокер, следующий шаг и client=agy).
2. Изоляция проектов (отсутствие cross-project leaks):
   - Заметки первого проекта не возвращаются при запросе контекста для второго зарегистрированного проекта.
3. Отказ бэкенда (fail-closed behavior):
   - Ошибка записи на уровне Basic Memory раннера ни в коем случае не заявляется успехом
     (происходит после успешного чтения, состояние памяти остается неизменным).
4. Безопасность передачи через stdin и порядок слоев валидации:
   - Проверка безопасного парсинга JSON payload из stdin без shell interpolation.
   - Сканер секретов отсекает токены и приватные ключи в разрешенных скалярах (SecretsError).
   - Структурный валидатор отсекает некорректные контрольные символы и неразрешенные переводы строк (ValidationError).
5. Отказ при незарегистрированном проекте:
   - Попытка извлечь контекст или записать чекпоинт в незарегистрированном каталоге
     завершается ошибкой (автоматическая регистрация запрещена).
"""

import argparse
import io
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from personal_tideway.cli.bridge import (
    ALLOWED_CHECKPOINT_FIELDS,
    parse_checkpoint_payload,
)
from personal_tideway.cli.main import ExitCode, handle_checkpoint
from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import (
    CLIENT_AGY,
    CLIENT_CODEX,
    CODEX_HOOK_EVENT,
)
from personal_tideway.core.basic_memory_installer import BasicMemoryRunnerResult
from personal_tideway.core.basic_memory_runtime import get_basic_memory_layout
from personal_tideway.core.checkpoint import (
    CheckpointPayload,
    CheckpointRequest,
    write_checkpoint,
)
from personal_tideway.core.hooks import (
    handle_agy_preinvocation,
    handle_codex_session_start,
)
from personal_tideway.core.project_resolver import register_directory
from personal_tideway.exceptions import (
    RuntimeProbeError,
    SecretsError,
    ValidationError,
)


class StatefulFakeBasicMemoryRunner:
    """Хранилище заметок в памяти, эмулирующее CLI Basic Memory 0.23.2.

    Точно соответствует реальным форматам команд Basic Memory:
    - tool read-note IDENTIFIER --json --project PROJECT --local (позиционный IDENTIFIER)
    - tool write-note --title TITLE --folder FOLDER --project PROJECT --overwrite --local
    - tool search-notes [--permalink PATTERN] --json --project PROJECT --local

    Не мокирует логику write_checkpoint или retrieve_context напрямую!
    """

    def __init__(self, *, fail_write: bool = False, fail_read: bool = False) -> None:
        # Структура: project_name -> { permalink -> { title, content, frontmatter, updated_at, status } }
        self.projects_notes: dict[str, dict[str, dict[str, Any]]] = {}
        self.read_calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
        self.write_calls: list[tuple[tuple[str, ...], dict[str, str], str]] = []
        self.search_calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
        self.fail_write = fail_write
        self.fail_read = fail_read

    def __call__(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: float,
        stdin: str = "",
    ) -> BasicMemoryRunnerResult:
        args = list(argv)
        if len(args) < 3 or args[1] != "tool":
            return BasicMemoryRunnerResult(
                returncode=1, stdout="", stderr="Unknown tool command"
            )

        subcmd = args[2]

        if subcmd == "read-note":
            self.read_calls.append((tuple(args), dict(env)))
            if self.fail_read:
                return BasicMemoryRunnerResult(
                    returncode=1, stdout="", stderr="Simulated disk read failure"
                )

            project_name = self._get_flag_value(args, "--project")
            # Basic Memory 0.23.2 read-note принимает идентификатор как позиционный аргумент (args[3])
            permalink = self._get_flag_value(args, "--permalink")
            if not permalink and len(args) > 3 and not args[3].startswith("-"):
                permalink = args[3]

            if not project_name or not permalink:
                return BasicMemoryRunnerResult(
                    returncode=1,
                    stdout="",
                    stderr="Missing required --project or note identifier",
                )

            proj_notes = self.projects_notes.get(project_name, {})
            note = proj_notes.get(permalink)
            if note is None:
                # Basic Memory возвращает rc=0 со всеми полями null, если заметка отсутствует
                null_payload = json.dumps({
                    "title": None,
                    "permalink": None,
                    "file_path": None,
                    "content": None,
                    "frontmatter": None,
                })
                return BasicMemoryRunnerResult(
                    returncode=0, stdout=null_payload, stderr=""
                )

            out_data = {
                "title": note["title"],
                "permalink": note["permalink"],
                "file_path": f"{permalink}.md",
                "content": note["content"],
                "frontmatter": note.get("frontmatter"),
            }
            return BasicMemoryRunnerResult(
                returncode=0, stdout=json.dumps(out_data), stderr=""
            )

        elif subcmd == "write-note":
            self.write_calls.append((tuple(args), dict(env), stdin))
            if self.fail_write:
                return BasicMemoryRunnerResult(
                    returncode=1,
                    stdout="",
                    stderr="Simulated disk write failure: permission denied or I/O error",
                )

            project_name = self._get_flag_value(args, "--project")
            title = self._get_flag_value(args, "--title") or "Current State"
            if not project_name:
                return BasicMemoryRunnerResult(
                    returncode=1, stdout="", stderr="Missing required --project"
                )

            frontmatter = self._parse_frontmatter(stdin)
            permalink = "current-state"

            if project_name not in self.projects_notes:
                self.projects_notes[project_name] = {}

            self.projects_notes[project_name][permalink] = {
                "title": title,
                "permalink": permalink,
                "content": stdin,
                "frontmatter": frontmatter,
                "updated_at": frontmatter.get("updated_at") if frontmatter else None,
                "status": frontmatter.get("status") if frontmatter else None,
            }

            resp = {
                "title": title,
                "permalink": permalink,
                "file_path": f"{permalink}.md",
                "content": stdin,
            }
            return BasicMemoryRunnerResult(
                returncode=0, stdout=json.dumps(resp), stderr=""
            )

        elif subcmd == "search-notes":
            self.search_calls.append((tuple(args), dict(env)))
            project_name = self._get_flag_value(args, "--project")
            if not project_name:
                return BasicMemoryRunnerResult(
                    returncode=1, stdout="", stderr="Missing required --project"
                )

            permalink_pat = self._get_flag_value(args, "--permalink")
            proj_notes = self.projects_notes.get(project_name, {})

            matched_items: list[dict[str, Any]] = []
            for perm, note in proj_notes.items():
                if permalink_pat and not self._matches_permalink(perm, permalink_pat):
                    continue
                matched_items.append({
                    "title": note["title"],
                    "permalink": note["permalink"],
                    "content": note["content"],
                    "updated_at": note.get("updated_at"),
                    "metadata": {
                        "note_type": "state",
                        "status": note.get("status") or "verified",
                    },
                })

            out = json.dumps({"results": matched_items})
            return BasicMemoryRunnerResult(returncode=0, stdout=out, stderr="")

        return BasicMemoryRunnerResult(
            returncode=1, stdout="", stderr=f"Unknown tool subcommand: {subcmd}"
        )

    @staticmethod
    def _matches_permalink(perm: str, pat: str) -> bool:
        clean_pat = pat.strip()
        if clean_pat == "*/current-state":
            return perm == "current-state" or perm.endswith("/current-state")
        if clean_pat.startswith("*/"):
            suffix = clean_pat[2:]
            return perm == suffix or perm.endswith(f"/{suffix}")
        if clean_pat.endswith("*"):
            prefix = clean_pat.rstrip("*")
            return perm.startswith(prefix)
        return perm == clean_pat

    @staticmethod
    def _get_flag_value(args: list[str], flag: str) -> str | None:
        for i, a in enumerate(args):
            if a == flag and i + 1 < len(args):
                return args[i + 1]
        return None

    @staticmethod
    def _parse_frontmatter(text: str) -> dict[str, Any] | None:
        if not text.startswith("---"):
            return None
        parts = text.split("---", 2)
        if len(parts) < 3:
            return None
        fm_lines = parts[1].strip().splitlines()
        res: dict[str, Any] = {}
        for line in fm_lines:
            if ":" in line:
                k, v = line.split(":", 1)
                res[k.strip()] = v.strip().strip("'\"")
        return res


def make_backend_executable(cfg: PersonalTidewayConfig) -> None:
    """Создает исполняемый файл-заглушку для валидации изолированного сервиса Basic Memory."""
    executable = get_basic_memory_layout(cfg).primary_executable
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)


def setup_handoff_environment(
    cfg: PersonalTidewayConfig,
    tmp_path: Path,
) -> tuple[Path, Path, str, str]:
    """Регистрирует два изолированных временных проекта: основной и дополнительный."""
    make_backend_executable(cfg)

    primary_dir = tmp_path / "primary_project"
    primary_dir.mkdir(parents=True, exist_ok=True)
    (primary_dir / "README.md").write_text(
        "# Primary Project\n", encoding="utf-8"
    )

    secondary_dir = tmp_path / "secondary_project"
    secondary_dir.mkdir(parents=True, exist_ok=True)
    (secondary_dir / "README.md").write_text(
        "# Secondary Project\n", encoding="utf-8"
    )

    dummy_git_runner = lambda cmd, cwd, timeout: (
        128,
        "",
        "fatal: not a git repository\n",
    )

    reg_primary = register_directory(
        primary_dir,
        cfg=cfg,
        display_name="Primary Project",
        git_runner=dummy_git_runner,
    )
    reg_secondary = register_directory(
        secondary_dir,
        cfg=cfg,
        display_name="Secondary Project",
        git_runner=dummy_git_runner,
    )

    return (
        primary_dir,
        secondary_dir,
        reg_primary.project_id,
        reg_secondary.project_id,
    )


# ============================================================================
# 1. Сквозной интеграционный тест полного цикла: Codex -> agy -> Codex
# ============================================================================


def test_codex_to_agy_to_codex_full_handoff_roundtrip(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Проверка полного цикла передачи контекста между клиентами на одном зарегистрированном проекте.

    Шаги сценария:
    1. Codex сохраняет чекпоинт с завершенной задачей:
       - source_client="codex", verification_status="verified", objective="Implement OAuth2 service".
       - Проверяется: written=True, вычислен fingerprint, runner.write_calls == 1.
    2. Повторный запуск того же чекпоинта Codex:
       - Проверяется: written=False, unchanged=True, runner.write_calls остается 1 (идемпотентность).
    3. Клиент agy открывает проект и запрашивает начальный контекст:
       - Вызывается реальный `handle_agy_preinvocation` (invocationNum=0, workspacePaths=[primary_dir]).
       - Проверяется: exit code 0, injectSteps содержит ephemeralMessage с объективом,
         решениями, следующим шагом и статусом верификации.
    4. Клиент agy продолжает разработку и сохраняет измененный чекпоинт:
       - source_client="agy", verification_status="partial", objective="Connect OAuth to database",
         blockers=["PostgreSQL pool exhaust under concurrent load"], next_safe_action="Increase pool size".
       - Проверяется: written=True, fingerprint обновился, runner.write_calls == 2.
    5. Клиент Codex стартует новую сессию в том же проекте:
       - Вызывается реальный `handle_codex_session_start` (SessionStart, startup, cwd=primary_dir).
       - Проверяется: exit code 0, additionalContext содержит обновленный объектив, решения agy,
         активный блокер, следующий шаг и обновленный статус.
    """
    cfg = personal_tideway_config
    primary_dir, _, primary_id, _ = setup_handoff_environment(cfg, tmp_path)
    runner = StatefulFakeBasicMemoryRunner()

    # --- Шаг 1: Codex фиксирует завершенную работу ---
    codex_payload = CheckpointPayload(
        condition="Clean build, auth service scaffolded, cryptography validated",
        objective="Implement OAuth2 token authentication service",
        completed=[
            "Configured asymmetric RSA keypair rotation",
            "Implemented JWT minting and revocation checks",
        ],
        blockers=[],
        verification_status="verified",
        next_safe_action="Integrate auth handler into HTTP router middleware",
        source_client=CLIENT_CODEX,
        evidence=["tests/test_auth.py:32", "src/auth/tokens.py"],
    )

    req1 = CheckpointRequest(project=primary_id, payload=codex_payload)
    res1 = write_checkpoint(cfg, req1, runner=runner)

    assert res1.written is True
    assert res1.unchanged is False
    assert res1.dry_run is False
    assert len(res1.fingerprint) == 64
    assert len(runner.write_calls) == 1

    # --- Шаг 2: Повторный запуск того же чекпоинта (Идемпотентность) ---
    res1_repeat = write_checkpoint(cfg, req1, runner=runner)

    assert res1_repeat.written is False
    assert res1_repeat.unchanged is True
    assert res1_repeat.fingerprint == res1.fingerprint
    # Проверка: счетчик реальных записей в бэкенд НЕ увеличился!
    assert len(runner.write_calls) == 1

    # --- Шаг 3: Запуск agy и получение начального контекста через PreInvocation ---
    agy_payload = {
        "invocationNum": 0,
        "workspacePaths": [str(primary_dir)],
    }
    agy_code, agy_resp = handle_agy_preinvocation(
        cfg,
        raw_payload=json.dumps(agy_payload),
        runner=runner,
    )

    assert agy_code == ExitCode.SUCCESS
    assert "injectSteps" in agy_resp
    assert len(agy_resp["injectSteps"]) == 1
    agy_msg = agy_resp["injectSteps"][0]["ephemeralMessage"]

    # Проверяем видимость всех ключевых данных, оставленных Codex
    assert "Current State: Current State (current-state)" in agy_msg
    assert "Active Objective" in agy_msg
    assert "Implement OAuth2 token authentication service" in agy_msg
    assert "Completed & Decisions" in agy_msg
    assert "Implemented JWT minting and revocation checks" in agy_msg
    assert "Next Safe Action" in agy_msg
    assert "Integrate auth handler into HTTP router middleware" in agy_msg
    assert "Status: verified" in agy_msg

    # --- Шаг 4: agy выполняет работу и сохраняет обновленный чекпоинт с блокером ---
    agy_checkpoint = CheckpointPayload(
        condition="Router middleware mounted, database connection pool starving",
        objective="Connect auth handler to persistent database pool",
        completed=[
            "Mounted auth handler routes under /api/v1/auth",
            "Added bearer token header extraction",
        ],
        blockers=[
            "PostgreSQL pool exhaust under concurrent load",
            "Missing pool keepalive setting in configuration",
        ],
        verification_status="partial",
        next_safe_action="Adjust max_connections and enable pool keepalive",
        source_client=CLIENT_AGY,
        evidence=["logs/auth_pool.log:18", "config/database.yaml"],
    )

    req2 = CheckpointRequest(project=primary_id, payload=agy_checkpoint)
    res2 = write_checkpoint(cfg, req2, runner=runner)

    assert res2.written is True
    assert res2.unchanged is False
    assert res2.fingerprint != res1.fingerprint
    # Проверка: вторая запись произошла успешно
    assert len(runner.write_calls) == 2

    # --- Шаг 5: Codex возвращается и запускает сессию через SessionStart hook ---
    codex_session_payload = {
        "hook_event_name": CODEX_HOOK_EVENT,
        "source": "startup",
        "cwd": str(primary_dir),
    }
    codex_code, codex_resp = handle_codex_session_start(
        cfg,
        raw_payload=json.dumps(codex_session_payload),
        runner=runner,
    )

    assert codex_code == ExitCode.SUCCESS
    assert "hookSpecificOutput" in codex_resp
    assert (
        codex_resp["hookSpecificOutput"]["hookEventName"] == CODEX_HOOK_EVENT
    )
    codex_ctx = codex_resp["hookSpecificOutput"]["additionalContext"]

    # Проверяем видимость обновлений, сделанных agy
    assert "Active Objective" in codex_ctx
    assert "Connect auth handler to persistent database pool" in codex_ctx
    assert "Completed & Decisions" in codex_ctx
    assert "Mounted auth handler routes under /api/v1/auth" in codex_ctx
    assert "Blockers" in codex_ctx
    assert "PostgreSQL pool exhaust under concurrent load" in codex_ctx
    assert "Next Safe Action" in codex_ctx
    assert "Adjust max_connections and enable pool keepalive" in codex_ctx
    assert "Status: partial" in codex_ctx


# ============================================================================
# 2. Изоляция проектов (Отсутствие утечек контекста между проектами)
# ============================================================================


def test_cross_project_isolation_prevents_context_leak(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Заметки состояния одного проекта никогда не попадают в контекст другого проекта."""
    cfg = personal_tideway_config
    _primary_dir, secondary_dir, primary_id, _secondary_id = (
        setup_handoff_environment(cfg, tmp_path)
    )
    runner = StatefulFakeBasicMemoryRunner()

    # Записываем чекпоинт только для основного проекта
    payload = CheckpointPayload(
        condition="Sensitive production data pipeline configured",
        objective="Deploy internal pipeline sentinel",
        completed=["Secret pipeline deployed to private cluster"],
        blockers=[],
        verification_status="verified",
        next_safe_action="Monitor private cluster metrics",
        source_client=CLIENT_CODEX,
    )
    write_checkpoint(
        cfg, CheckpointRequest(project=primary_id, payload=payload), runner=runner
    )

    # Запрашиваем контекст для второго проекта через Codex SessionStart
    codex_payload = {
        "hook_event_name": CODEX_HOOK_EVENT,
        "source": "startup",
        "cwd": str(secondary_dir),
    }
    code, resp = handle_codex_session_start(
        cfg,
        raw_payload=json.dumps(codex_payload),
        runner=runner,
    )
    assert code == ExitCode.SUCCESS
    ctx = resp["hookSpecificOutput"]["additionalContext"]

    # Убеждаемся, что контекст пуст или сообщает об отсутствии заметок, и не содержит данных первого проекта
    assert "Sensitive production data pipeline configured" not in ctx
    assert "Secret pipeline deployed to private cluster" not in ctx
    assert "Deploy internal pipeline sentinel" not in ctx

    # Запрашиваем контекст для второго проекта через agy PreInvocation
    agy_payload = {
        "invocationNum": 0,
        "workspacePaths": [str(secondary_dir)],
    }
    code_agy, resp_agy = handle_agy_preinvocation(
        cfg,
        raw_payload=json.dumps(agy_payload),
        runner=runner,
    )
    assert code_agy == ExitCode.SUCCESS
    msg_agy = resp_agy["injectSteps"][0]["ephemeralMessage"]

    assert "Sensitive production data pipeline configured" not in msg_agy
    assert "Secret pipeline deployed to private cluster" not in msg_agy


# ============================================================================
# 3. Отказ бэкенда не заявляется успехом (Fail-closed behavior)
# ============================================================================


def test_backend_write_failure_fails_closed_and_never_claims_success(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Сбой бэкенда приводит к ошибке, происходит после чтения и не изменяет память."""
    cfg = personal_tideway_config
    _, _, primary_id, _ = setup_handoff_environment(cfg, tmp_path)

    # Раннер в режиме отказа записи
    failing_runner = StatefulFakeBasicMemoryRunner(fail_write=True)

    payload = CheckpointPayload(
        condition="Local environment healthy",
        objective="Test backend write failure handling",
        completed=["Prepared migration script"],
        blockers=[],
        verification_status="verified",
        next_safe_action="Execute migration",
        source_client=CLIENT_CODEX,
    )

    req = CheckpointRequest(project=primary_id, payload=payload)

    # 1. Прямой вызов write_checkpoint выбрасывает RuntimeProbeError
    with pytest.raises(RuntimeProbeError):
        write_checkpoint(cfg, req, runner=failing_runner)

    # Проверяем, что попытка записи произошла после успешного чтения, и состояние в runner не изменилось
    assert len(failing_runner.read_calls) == 1
    assert len(failing_runner.write_calls) == 1
    assert not failing_runner.projects_notes

    # 2. Вызов через CLI-обработчик handle_checkpoint с monkeypatching sys.stdin
    raw_json = json.dumps(payload.to_dict())
    args = argparse.Namespace(
        project=primary_id,
        dry_run=False,
        json=True,
        file=None,
        stdin=True,
    )

    fake_stdin = io.TextIOWrapper(
        io.BytesIO(raw_json.encode("utf-8")), encoding="utf-8"
    )
    monkeypatch.setattr("sys.stdin", fake_stdin)

    with pytest.raises(RuntimeProbeError):
        handle_checkpoint(
            cfg,
            args,
            runner=failing_runner,
        )

    # Убеждаемся, что повторная попытка через CLI также не создала записей
    assert not failing_runner.projects_notes
    assert len(failing_runner.read_calls) == 2
    assert len(failing_runner.write_calls) == 2


# ============================================================================
# 4. Безопасность передачи через stdin и порядок слоев валидации
# ============================================================================


def test_safe_stdin_protocol_parsing_and_secret_rejection(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Чекпоинт через stdin валидирует JSON по схеме и отклоняет секреты и невалидный ввод."""
    # 0. Проверка белого списка допустимых полей схемы
    assert ALLOWED_CHECKPOINT_FIELDS == frozenset({
        "condition",
        "objective",
        "completed",
        "blockers",
        "verification_status",
        "next_safe_action",
        "source_client",
        "evidence",
    })

    # 1. Валидный JSON с полным набором полей парсится без ошибок
    valid_json = json.dumps({
        "condition": "Staging environment deployed",
        "objective": "Complete validation test",
        "completed": ["Step 1", "Step 2"],
        "blockers": ["Waiting for approval"],
        "verification_status": "partial",
        "next_safe_action": "Request security signoff",
        "source_client": "codex",
        "evidence": ["tests/test_audit.py:10"],
    })
    parsed = parse_checkpoint_payload(valid_json)
    assert parsed.condition == "Staging environment deployed"
    assert parsed.objective == "Complete validation test"
    assert parsed.source_client == "codex"
    assert parsed.verification_status == "partial"
    assert parsed.completed == ("Step 1", "Step 2")
    assert parsed.blockers == ("Waiting for approval",)

    # 2. Неизвестные поля отбрасываются (защита схемы)
    bad_fields_json = json.dumps({
        "condition": "Ready",
        "objective": "Test",
        "unknown_extra_field": "dangerous_data",
    })
    with pytest.raises(ValidationError, match="Unknown field"):
        parse_checkpoint_payload(bad_fields_json)

    # 3. Отсутствие обязательных полей (condition или objective)
    no_cond_json = json.dumps({"objective": "Missing condition"})
    with pytest.raises(ValidationError, match="Missing required field 'condition'"):
        parse_checkpoint_payload(no_cond_json)

    # 4. Попытка передать секрет (токен GitHub) немедленно отклоняется сканером секретов
    secret_payload_json = json.dumps({
        "condition": "Server configured with token",
        "objective": "Test secrets detection",
        "completed": ["Configured token: ghp_111122223333444455556666777788889999"],
    })
    with pytest.raises(SecretsError):
        parse_checkpoint_payload(secret_payload_json)

    # 5. Попытка внедрения приватного ключа RSA в разрешенное многострочное скалярное поле (condition)
    # отлавливается именно сканером секретов (SecretsError)
    rsa_payload_json = json.dumps({
        "condition": "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----",
        "objective": "Test private key rejection",
        "completed": ["Configured authentication"],
    })
    with pytest.raises(SecretsError):
        parse_checkpoint_payload(rsa_payload_json)

    # 6. Недопустимые переносы строк / контрольные символы в строках списка (completed)
    # безопасно отсекаются структурной валидацией символов (ValidationError) ДО сканирования секретов
    bad_control_json = json.dumps({
        "condition": "Valid condition",
        "objective": "Valid objective",
        "completed": ["Line with illegal\nnewline in item"],
    })
    with pytest.raises(ValidationError, match="must be a single line without newlines"):
        parse_checkpoint_payload(bad_control_json)

    # 7. Невалидный JSON (синтаксическая ошибка)
    with pytest.raises(ValidationError, match="not valid JSON"):
        parse_checkpoint_payload("{condition: malformed json")


# ============================================================================
# 5. Отказ при незарегистрированном проекте
# ============================================================================


def test_unregistered_project_fails_closed_without_auto_registration(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Попытка работы с незарегистрированным каталогом отклоняется."""
    cfg = personal_tideway_config
    make_backend_executable(cfg)
    runner = StatefulFakeBasicMemoryRunner()

    unregistered_dir = tmp_path / "unregistered_dir"
    unregistered_dir.mkdir(parents=True, exist_ok=True)

    # Codex SessionStart в незарегистрированном каталоге
    codex_payload = {
        "hook_event_name": CODEX_HOOK_EVENT,
        "source": "startup",
        "cwd": str(unregistered_dir),
    }
    with pytest.raises(ValidationError, match="No registered project found|unregistered project candidate"):
        handle_codex_session_start(
            cfg,
            raw_payload=json.dumps(codex_payload),
            runner=runner,
        )

    # agy PreInvocation в незарегистрированном каталоге
    agy_payload = {
        "invocationNum": 0,
        "workspacePaths": [str(unregistered_dir)],
    }
    with pytest.raises(ValidationError, match="No registered project found|unregistered project candidate"):
        handle_agy_preinvocation(
            cfg,
            raw_payload=json.dumps(agy_payload),
            runner=runner,
        )
