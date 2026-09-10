"""Тесты детерминированной синхронизации ProjectRegistry с Basic Memory (WP10a).

Проверяет 14 обязательных контрактов:
1. Пустой реестр (empty registry).
2. Все три вида проектов (git, directory, external).
3. Точный формат имени, пути и режима (exact name/path/mode).
4. Запрет дубликатов memory.project_name без учета регистра (duplicate names).
5. Нулевая мутация ФС в режиме dry-run (dry-run zero mutation).
6. Идемпотентность по mtime и содержимому (idempotent mtime).
7. Добавление, обновление и удаление проектов (add/update/remove).
8. Сохранение заметок и каталогов памяти при удалении проектов (removed memory preserved).
9. Сохранение сторонних настроек верхнего уровня Basic Memory (extras preserved).
10. Безопасность при некорректных формах конфигурации без утечки секретов (unsafe config forms).
11. Отказ при наличии symlink в цепочке каталогов памяти (symlink memory path rejection).
12. Полное отсутствие загрязнения исходных репозиториев (source-root no-pollution).
13. Сохранение старой конфигурации при сбое записи через инжектированный писатель (injected write failure).
14. Корректное использование и освобождение блокировки concurrency lock (concurrent lock use).
"""

from contextlib import contextmanager
import fcntl
import inspect
import json
import os
from pathlib import Path
import stat
from typing import Any
import pytest

from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.core.basic_memory_installer import (
    acquire_basic_memory_lock,
    read_safe_basic_memory_config,
)
from personal_tideway.core.basic_memory_reconciliation import (
    BASIC_MEMORY_PROJECT_MODE,
    BasicMemoryProjectPlan,
    BasicMemoryProjectReconciliationResult,
    compute_desired_basic_memory_projects,
    plan_basic_memory_projects,
    reconcile_basic_memory_projects,
    validate_registry_for_basic_memory,
)
from personal_tideway.core.basic_memory_runtime import get_basic_memory_layout
from personal_tideway.core.registry import ProjectRecord, ProjectRegistry
from personal_tideway.exceptions import BoundaryError, ConfigError, ValidationError


def snapshot_filesystem(root: Path) -> dict[str, tuple[int, int, int]]:
    """Сделать снимок состояния файлов и каталогов: (size, mtime_ns, mode)."""
    if not root.exists():
        return {}
    entries: dict[str, tuple[int, int, int]] = {}
    for p in sorted(root.rglob("*")):
        try:
            st = p.lstat()
            entries[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns, st.st_mode)
        except OSError:
            pass
    return entries


def test_reconcile_empty_registry(personal_tideway_config: PersonalTidewayConfig):
    """Контракт 1: Пустой реестр создает валидную пустую конфигурацию Basic Memory и идемпотентен."""
    reg = ProjectRegistry.empty()
    layout = get_basic_memory_layout(personal_tideway_config)

    # Первый запуск: создается config.json с projects={}
    res1 = reconcile_basic_memory_projects(personal_tideway_config, reg)
    assert res1.dry_run is False
    assert res1.changed is True
    assert res1.config_written is True
    assert res1.created_memory_roots == ()
    assert res1.added_projects == ()
    assert res1.updated_projects == ()
    assert res1.removed_projects == ()

    assert layout.config_file.is_file()
    assert (layout.config_file.stat().st_mode & 0o777) == 0o600

    cfg_data = json.loads(layout.config_file.read_text(encoding="utf-8"))
    assert cfg_data == {"auto_update": False, "default_project": None, "projects": {}}

    mtime_before = layout.config_file.stat().st_mtime_ns
    bytes_before = layout.config_file.read_bytes()

    # Второй запуск: уже конвергировано, mtime и байты не меняются
    res2 = reconcile_basic_memory_projects(personal_tideway_config, reg)
    assert res2.dry_run is False
    assert res2.changed is False
    assert res2.config_written is False
    assert res2.created_memory_roots == ()
    assert layout.config_file.stat().st_mtime_ns == mtime_before
    assert layout.config_file.read_bytes() == bytes_before


def test_reconcile_all_three_kinds(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Контракт 2: Все 3 вида проектов (git, directory, external) корректно синхронизируются."""
    git_dir = tmp_path / "git_source"
    git_dir.mkdir()
    dir_dir = tmp_path / "dir_source"
    dir_dir.mkdir()

    p_git = ProjectRecord.create(
        slug="git-proj",
        display_name="Git Project",
        kind="git",
        paths=[str(git_dir)],
    )
    p_dir = ProjectRecord.create(
        slug="dir-proj",
        display_name="Directory Project",
        kind="directory",
        paths=[str(dir_dir)],
    )
    # Внешний проект без source bindings
    p_ext = ProjectRecord.create(
        slug="ext-proj",
        display_name="External Project",
        kind="external",
    )
    assert p_ext.bindings.paths == []
    assert p_ext.bindings.git_common_dirs == []
    assert p_ext.bindings.git_remotes == []

    reg = ProjectRegistry(projects=[p_git, p_dir, p_ext])
    res = reconcile_basic_memory_projects(personal_tideway_config, reg)

    assert res.changed is True
    assert res.config_written is True
    assert len(res.created_memory_roots) == 3
    assert len(res.added_projects) == 3
    assert res.updated_projects == ()
    assert res.removed_projects == ()

    layout = get_basic_memory_layout(personal_tideway_config)
    cfg_data = json.loads(layout.config_file.read_text(encoding="utf-8"))
    projects_mapping = cfg_data["projects"]

    # Проверяем наличие всех трех проектов, включая внешний
    assert p_git.memory.project_name in projects_mapping
    assert p_dir.memory.project_name in projects_mapping
    assert p_ext.memory.project_name in projects_mapping

    for p in (p_git, p_dir, p_ext):
        mem_dir = personal_tideway_config.projects_dir / p.id / "memory"
        assert mem_dir.is_dir()
        assert (mem_dir.stat().st_mode & 0o777) == 0o700
        assert (mem_dir.parent.stat().st_mode & 0o777) == 0o700
        assert projects_mapping[p.memory.project_name] == {
            "path": str(mem_dir),
            "mode": "local",
        }


def test_reconcile_exact_name_path_mode(personal_tideway_config: PersonalTidewayConfig):
    """Контракт 3: Точное совпадение ключа project_name и структуры {path, mode: local}."""
    rec = ProjectRecord.create(slug="single-app", display_name="Single App", kind="git")
    reg = ProjectRegistry(projects=[rec])

    res = reconcile_basic_memory_projects(personal_tideway_config, reg)
    assert res.config_written is True

    layout = get_basic_memory_layout(personal_tideway_config)
    cfg_data = json.loads(layout.config_file.read_text(encoding="utf-8"))
    projects_dict = cfg_data["projects"]

    # Точный ключ
    assert rec.memory.project_name in projects_dict
    val = projects_dict[rec.memory.project_name]

    # Точное значение без лишних полей
    expected_path = str(personal_tideway_config.home / "projects" / rec.id / "memory")
    assert val == {
        "path": expected_path,
        "mode": BASIC_MEMORY_PROJECT_MODE,
    }
    assert val["mode"] == "local"


def test_reconcile_duplicate_memory_project_names_rejected(
    personal_tideway_config: PersonalTidewayConfig,
):
    """Контракт 4: Дубликаты memory.project_name отклоняются без учета регистра."""
    p1 = ProjectRecord.create(
        slug="proj-one",
        display_name="Project One",
        kind="git",
        memory_project_name="ptw-common-name",
    )
    p2 = ProjectRecord.create(
        slug="proj-two",
        display_name="Project Two",
        kind="directory",
        memory_project_name="PTW-COMMON-NAME",
    )

    reg = ProjectRegistry(projects=[p1, p2])

    with pytest.raises(ValidationError) as exc:
        reconcile_basic_memory_projects(personal_tideway_config, reg)

    assert "duplicate memory.project_name" in str(exc.value).lower()
    assert "ptw-common-name" in str(exc.value).lower()


def test_reconcile_dry_run_zero_mutation(
    personal_tideway_config: PersonalTidewayConfig,
):
    """Контракт 5: Режим dry_run не производит никаких изменений и не захватывает блокировку."""
    p = ProjectRecord.create(slug="dry-app", display_name="Dry App", kind="git")
    reg = ProjectRegistry(projects=[p])

    lock_file = personal_tideway_config.locks_dir / "basic-memory-install.lock"
    before_snapshot = snapshot_filesystem(personal_tideway_config.home)

    plan = plan_basic_memory_projects(personal_tideway_config, reg)
    assert plan.has_changes is True
    assert p.memory.project_name in plan.added_projects
    assert len(plan.memory_roots_to_create) == 1

    res = reconcile_basic_memory_projects(personal_tideway_config, reg, dry_run=True)
    assert res.dry_run is True
    assert res.changed is True
    assert res.config_written is False
    assert res.created_memory_roots == ()
    assert p.memory.project_name in res.added_projects

    after_snapshot = snapshot_filesystem(personal_tideway_config.home)
    # Полная неизменность файловой системы
    assert before_snapshot == after_snapshot
    assert not lock_file.exists()


def test_reconcile_idempotent_mtime(personal_tideway_config: PersonalTidewayConfig):
    """Контракт 6: Повторное применение к уже синхронизированному состоянию не меняет mtime и байты."""
    p = ProjectRecord.create(slug="idemp-app", display_name="Idempotent App", kind="git")
    reg = ProjectRegistry(projects=[p])

    res1 = reconcile_basic_memory_projects(personal_tideway_config, reg)
    assert res1.config_written is True
    assert len(res1.created_memory_roots) == 1

    layout = get_basic_memory_layout(personal_tideway_config)
    mtime1 = layout.config_file.stat().st_mtime_ns
    bytes1 = layout.config_file.read_bytes()

    res2 = reconcile_basic_memory_projects(personal_tideway_config, reg)
    assert res2.config_written is False
    assert res2.changed is False
    assert res2.created_memory_roots == ()
    assert res2.added_projects == ()
    assert res2.updated_projects == ()
    assert res2.removed_projects == ()

    mtime2 = layout.config_file.stat().st_mtime_ns
    bytes2 = layout.config_file.read_bytes()

    assert mtime1 == mtime2
    assert bytes1 == bytes2


def test_reconcile_add_update_remove(personal_tideway_config: PersonalTidewayConfig):
    """Контракт 7: Корректное отслеживание и применение добавления, обновления и удаления проектов."""
    p1 = ProjectRecord.create(slug="proj-1", display_name="Proj 1", kind="git")
    p2 = ProjectRecord.create(slug="proj-2", display_name="Proj 2", kind="directory")
    reg_initial = ProjectRegistry(projects=[p1, p2])

    res_init = reconcile_basic_memory_projects(personal_tideway_config, reg_initial)
    assert set(res_init.added_projects) == {p1.memory.project_name, p2.memory.project_name}

    # 1. Добавление нового проекта p3
    p3 = ProjectRecord.create(slug="proj-3", display_name="Proj 3", kind="external")
    reg_added = ProjectRegistry(projects=[p1, p2, p3])
    res_add = reconcile_basic_memory_projects(personal_tideway_config, reg_added)
    assert res_add.added_projects == (p3.memory.project_name,)
    assert res_add.updated_projects == ()
    assert res_add.removed_projects == ()

    # 2. Обновление проекта (например, p1 на диске имеет другой path)
    layout = get_basic_memory_layout(personal_tideway_config)
    cfg_data = json.loads(layout.config_file.read_text(encoding="utf-8"))
    cfg_data["projects"][p1.memory.project_name]["path"] = "/nonexistent/old_path"
    layout.config_file.write_text(json.dumps(cfg_data, indent=2) + "\n", encoding="utf-8")

    res_update = reconcile_basic_memory_projects(personal_tideway_config, reg_added)
    assert res_update.updated_projects == (p1.memory.project_name,)
    assert res_update.added_projects == ()
    assert res_update.removed_projects == ()

    # 3. Удаление проекта p2 из реестра
    reg_removed = ProjectRegistry(projects=[p1, p3])
    res_rem = reconcile_basic_memory_projects(personal_tideway_config, reg_removed)
    assert res_rem.removed_projects == (p2.memory.project_name,)
    assert res_rem.added_projects == ()
    assert res_rem.updated_projects == ()

    cfg_after = json.loads(layout.config_file.read_text(encoding="utf-8"))
    assert p2.memory.project_name not in cfg_after["projects"]
    assert p1.memory.project_name in cfg_after["projects"]
    assert p3.memory.project_name in cfg_after["projects"]


def test_reconcile_removed_memory_preserved(
    personal_tideway_config: PersonalTidewayConfig,
):
    """Контракт 8: Удаление проекта из реестра убирает привязку, но сохраняет каталог заметок и файлы."""
    p = ProjectRecord.create(slug="persistent-notes", display_name="Persistent", kind="git")
    reg = ProjectRegistry(projects=[p])

    reconcile_basic_memory_projects(personal_tideway_config, reg)

    note_dir = personal_tideway_config.projects_dir / p.id / "memory"
    assert note_dir.is_dir()

    # Создаем файл заметки в каталоге памяти проекта
    note_file = note_dir / "current-state.md"
    note_content = "# Project Status\n\nActive critical notes that must never be lost."
    note_file.write_text(note_content, encoding="utf-8")

    # Удаляем проект из реестра и синхронизируем
    empty_reg = ProjectRegistry.empty()
    res = reconcile_basic_memory_projects(personal_tideway_config, empty_reg)
    assert res.removed_projects == (p.memory.project_name,)

    layout = get_basic_memory_layout(personal_tideway_config)
    cfg_data = json.loads(layout.config_file.read_text(encoding="utf-8"))
    assert p.memory.project_name not in cfg_data["projects"]

    # Каталог памяти и файл заметок остаются нетронутыми
    assert note_dir.is_dir()
    assert note_file.is_file()
    assert note_file.read_text(encoding="utf-8") == note_content


def test_reconcile_upstream_extras_preserved(
    personal_tideway_config: PersonalTidewayConfig,
):
    """Контракт 9: Сохранение сторонних настроек верхнего уровня существующей конфигурации Basic Memory."""
    layout = get_basic_memory_layout(personal_tideway_config)
    layout.config_dir.mkdir(parents=True, exist_ok=True)

    # Исходная конфигурация со сторонними ключами (включая секреты/токены)
    initial_config = {
        "auto_update": False,
        "default_project": None,
        "projects": {},
        "semantic_search_enabled": True,
        "semantic_min_similarity": 0.82,
        "update_check_interval": 86400,
        "extra_custom_token": "SENSITIVE_SECRET_TOKEN_VALUE",
    }
    layout.config_file.write_text(json.dumps(initial_config, indent=2) + "\n", encoding="utf-8")
    layout.config_file.chmod(0o600)

    p = ProjectRecord.create(slug="extra-test", display_name="Extra Test", kind="git")
    reg = ProjectRegistry(projects=[p])

    res = reconcile_basic_memory_projects(personal_tideway_config, reg)
    assert res.config_written is True

    cfg_after = json.loads(layout.config_file.read_text(encoding="utf-8"))
    # Обязательные инварианты подтверждены
    assert cfg_after["auto_update"] is False
    assert cfg_after["default_project"] is None
    assert p.memory.project_name in cfg_after["projects"]

    # Сторонние настройки сохранены в точности
    assert cfg_after["semantic_search_enabled"] is True
    assert cfg_after["semantic_min_similarity"] == 0.82
    assert cfg_after["update_check_interval"] == 86400
    assert cfg_after["extra_custom_token"] == "SENSITIVE_SECRET_TOKEN_VALUE"


def test_reconcile_unsafe_config_forms_rejected_secret_safe(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Контракт 10: Отказ при некорректных формах конфигурации (symlink, FIFO, hardlink, oversize, invalid JSON) без утечки секретов."""
    layout = get_basic_memory_layout(personal_tideway_config)
    layout.config_dir.mkdir(parents=True, exist_ok=True)
    config_file = layout.config_file

    canary_target = tmp_path / "protected_target.txt"
    canary_content = "DO_NOT_LEAK_CANARY_SECRET_77777"
    canary_target.write_text(canary_content, encoding="utf-8")

    def clean_config():
        if config_file.is_symlink() or config_file.exists():
            config_file.unlink()

    reg = ProjectRegistry.empty()

    # 1. Hardlink (st_nlink != 1)
    clean_config()
    os.link(canary_target, config_file)
    with pytest.raises(ConfigError) as exc_hl:
        reconcile_basic_memory_projects(personal_tideway_config, reg)
    assert "invalid link count" in str(exc_hl.value).lower()
    assert canary_content not in str(exc_hl.value)

    # 2. FIFO (не регулярный файл)
    clean_config()
    os.mkfifo(config_file)
    with pytest.raises(ConfigError) as exc_fifo:
        reconcile_basic_memory_projects(personal_tideway_config, reg)
    assert "not a regular file" in str(exc_fifo.value).lower()

    # 3. Symlink
    clean_config()
    config_file.symlink_to(canary_target)
    with pytest.raises((BoundaryError, ConfigError)) as exc_sym:
        reconcile_basic_memory_projects(personal_tideway_config, reg)
    assert "symlink" in str(exc_sym.value).lower()
    assert canary_content not in str(exc_sym.value)
    assert str(canary_target) not in str(exc_sym.value)

    # 4. Oversized файл (> 64KiB)
    clean_config()
    config_file.write_text("x" * (65 * 1024), encoding="utf-8")
    with pytest.raises(ConfigError) as exc_over:
        reconcile_basic_memory_projects(personal_tideway_config, reg)
    assert "exceeds size limit" in str(exc_over.value).lower()

    # 5. Невалидный синтаксис JSON
    clean_config()
    config_file.write_text("{ unclosed invalid json", encoding="utf-8")
    with pytest.raises(ConfigError) as exc_json:
        reconcile_basic_memory_projects(personal_tideway_config, reg)
    assert "invalid json" in str(exc_json.value).lower()

    # 6. Небезопасный auto_update=True
    clean_config()
    config_file.write_text(json.dumps({"auto_update": True, "default_project": None, "projects": {}}), encoding="utf-8")
    with pytest.raises(ConfigError) as exc_au:
        reconcile_basic_memory_projects(personal_tideway_config, reg)
    assert "does not match safe bootstrap" in str(exc_au.value).lower()

    # 7. Небезопасный default_project
    clean_config()
    config_file.write_text(json.dumps({"auto_update": False, "default_project": "accidental", "projects": {}}), encoding="utf-8")
    with pytest.raises(ConfigError) as exc_dp:
        reconcile_basic_memory_projects(personal_tideway_config, reg)
    assert "does not match safe bootstrap" in str(exc_dp.value).lower()

    # 8. projects не словарь
    clean_config()
    config_file.write_text(json.dumps({"auto_update": False, "default_project": None, "projects": []}), encoding="utf-8")
    with pytest.raises(ConfigError) as exc_pr:
        reconcile_basic_memory_projects(personal_tideway_config, reg)
    assert "does not match safe bootstrap" in str(exc_pr.value).lower()


def test_reconcile_symlink_memory_path_rejected(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Контракт 11: Отказ при наличии symlink в любом компоненте пути памяти без утечки целевого пути."""
    outside_dir = tmp_path / "outside_sensitive_dir"
    outside_dir.mkdir()

    p = ProjectRecord.create(slug="symlink-test", display_name="Symlink Test", kind="git")
    reg = ProjectRegistry(projects=[p])

    proj_dir = personal_tideway_config.projects_dir / p.id
    proj_dir.mkdir(parents=True, exist_ok=True)
    bad_mem = proj_dir / "memory"
    bad_mem.symlink_to(outside_dir)

    with pytest.raises(BoundaryError) as exc:
        reconcile_basic_memory_projects(personal_tideway_config, reg)

    assert str(exc.value) == "Memory path component is a symlink, which is not permitted."
    assert str(outside_dir) not in str(exc.value)

    # Проверяем также случай, когда сам proj_dir является симлинком
    bad_mem.unlink()
    proj_dir.rmdir()
    proj_dir.symlink_to(outside_dir)

    with pytest.raises(BoundaryError) as exc2:
        reconcile_basic_memory_projects(personal_tideway_config, reg)

    assert str(exc2.value) == "Memory path component is a symlink, which is not permitted."
    assert str(outside_dir) not in str(exc2.value)


def test_reconcile_source_root_no_pollution(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Контракт 12: Реконсиляция никогда не пишет в source roots привязок и не создает там служебных файлов."""
    source_git = tmp_path / "my_source_repo"
    source_git.mkdir()
    canary_git = source_git / "canary_repo_file.txt"
    canary_git.write_text("IMPORTANT_SOURCE_DATA", encoding="utf-8")

    source_dir = tmp_path / "my_dir_repo"
    source_dir.mkdir()
    canary_dir = source_dir / "canary_dir_file.txt"
    canary_dir.write_text("IMPORTANT_DIR_DATA", encoding="utf-8")

    p1 = ProjectRecord.create(slug="my-git", display_name="My Git", kind="git", paths=[str(source_git)])
    p2 = ProjectRecord.create(slug="my-dir", display_name="My Dir", kind="directory", paths=[str(source_dir)])
    reg = ProjectRegistry(projects=[p1, p2])

    snap_git_before = snapshot_filesystem(source_git)
    snap_dir_before = snapshot_filesystem(source_dir)

    res = reconcile_basic_memory_projects(personal_tideway_config, reg)
    assert res.changed is True

    snap_git_after = snapshot_filesystem(source_git)
    snap_dir_after = snapshot_filesystem(source_dir)

    # Исходные каталоги абсолютно неизменны
    assert snap_git_before == snap_git_after
    assert snap_dir_before == snap_dir_after

    # Проверка отсутствия сгенерированных служебных файлов
    forbidden_names = [".personal-tideway", ".personal-tideway.yaml", ".basic-memory", "AGENTS.md", "GEMINI.md"]
    for s_root in (source_git, source_dir):
        for name in forbidden_names:
            assert not (s_root / name).exists()


def test_reconcile_injected_write_failure_preserves_old_config(
    personal_tideway_config: PersonalTidewayConfig,
):
    """Контракт 13: Сбой при записи через инжектированный писатель сохраняет старую конфигурацию и не утекает секреты."""
    layout = get_basic_memory_layout(personal_tideway_config)
    layout.config_dir.mkdir(parents=True, exist_ok=True)

    old_config_text = (
        json.dumps(
            {
                "auto_update": False,
                "default_project": None,
                "projects": {"old-proj": {"path": "/old", "mode": "local"}},
            },
            indent=2,
        )
        + "\n"
    )
    layout.config_file.write_text(old_config_text, encoding="utf-8")
    layout.config_file.chmod(0o600)
    old_bytes = layout.config_file.read_bytes()

    p = ProjectRecord.create(slug="fail-write", display_name="Fail Write", kind="git")
    reg = ProjectRegistry(projects=[p])

    canary = "INJECTED_WRITER_CANARY_SECRET_78945612"

    def failing_writer(path: Path, content: str) -> None:
        raise OSError(f"Simulated atomic storage write failure with canary: {canary}")

    with pytest.raises(ConfigError) as exc:
        reconcile_basic_memory_projects(personal_tideway_config, reg, writer=failing_writer)

    assert str(exc.value) == "Failed to write Basic Memory project configuration."
    assert exc.value.__cause__ is None
    assert canary not in str(exc.value)
    assert "Simulated atomic storage write failure" not in str(exc.value)

    # Старая конфигурация сохранилась байт-в-байт
    assert layout.config_file.read_bytes() == old_bytes

    # Временные файлы отсутствуют
    tmp_files = list(layout.config_dir.glob(".tmp_config_*"))
    assert tmp_files == []


def test_reconcile_concurrent_lock_use_via_monkeypatch(
    personal_tideway_config: PersonalTidewayConfig,
    monkeypatch: pytest.MonkeyPatch,
):
    """Контракт 14: Reconcile берет блокировку basic-memory-install.lock и освобождает её после завершения."""
    p = ProjectRecord.create(slug="lock-test", display_name="Lock Test", kind="git")
    reg = ProjectRegistry(projects=[p])

    lock_file = personal_tideway_config.locks_dir / "basic-memory-install.lock"
    lock_was_held = False

    def checking_writer(path: Path, content: str) -> None:
        nonlocal lock_was_held
        assert lock_file.exists()
        fd = os.open(lock_file, os.O_RDWR)
        try:
            try:
                # Попытка неблокирующего захвата должна провалиться, так как блокировка удерживается
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                lock_was_held = False
            except (BlockingIOError, OSError):
                lock_was_held = True
        finally:
            os.close(fd)
        # Записываем валидный файл
        path.write_text(content, encoding="utf-8")

    res = reconcile_basic_memory_projects(personal_tideway_config, reg, writer=checking_writer)
    assert res.config_written is True
    assert lock_was_held is True

    # После выхода из reconcile блокировка освобождена
    fd2 = os.open(lock_file, os.O_RDWR)
    try:
        fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd2, fcntl.LOCK_UN)
    finally:
        os.close(fd2)


def test_plan_and_dry_run_preserve_0644_config_mode_and_snapshot(
    personal_tideway_config: PersonalTidewayConfig,
):
    """Регрессия Finding 2: План и dry-run не мутируют ФС и сохраняют mode 0644, а real reconcile нормализует к 0600."""
    layout = get_basic_memory_layout(personal_tideway_config)
    layout.config_dir.mkdir(parents=True, exist_ok=True)

    valid_config_text = (
        json.dumps(
            {
                "auto_update": False,
                "default_project": None,
                "projects": {},
            },
            indent=2,
        )
        + "\n"
    )
    layout.config_file.write_text(valid_config_text, encoding="utf-8")
    layout.config_file.chmod(0o644)
    assert (layout.config_file.stat().st_mode & 0o777) == 0o644

    p = ProjectRecord.create(slug="mode-test", display_name="Mode Test", kind="git")
    reg = ProjectRegistry(projects=[p])

    before_snapshot = snapshot_filesystem(personal_tideway_config.home)

    # 1. Прямое планирование не меняет mode 0644 и не меняет ФС
    plan = plan_basic_memory_projects(personal_tideway_config, reg)
    assert plan.has_changes is True
    assert (layout.config_file.stat().st_mode & 0o777) == 0o644
    assert snapshot_filesystem(personal_tideway_config.home) == before_snapshot

    # 2. reconcile в режиме dry_run=True не меняет mode 0644 и не меняет ФС
    res_dry = reconcile_basic_memory_projects(personal_tideway_config, reg, dry_run=True)
    assert res_dry.dry_run is True
    assert res_dry.changed is True
    assert (layout.config_file.stat().st_mode & 0o777) == 0o644
    assert snapshot_filesystem(personal_tideway_config.home) == before_snapshot

    # 3. Реальный reconcile под блокировкой нормализует права к 0600
    res_real = reconcile_basic_memory_projects(personal_tideway_config, reg, dry_run=False)
    assert res_real.dry_run is False
    assert res_real.changed is True
    assert layout.config_file in res_real.normalized_permissions
    assert (layout.config_file.stat().st_mode & 0o777) == 0o600

    # Повторный запуск на уже конвергированном состоянии
    res_real2 = reconcile_basic_memory_projects(personal_tideway_config, reg, dry_run=False)
    assert res_real2.dry_run is False
    assert res_real2.changed is False
    assert res_real2.config_written is False
    assert res_real2.normalized_permissions == ()

    # 4. Проверка install bootstrap: ensure_safe_bootstrap_config также нормализует 0644 к 0600
    from personal_tideway.core.basic_memory_installer import ensure_safe_bootstrap_config
    from personal_tideway.core.basic_memory_runtime import BasicMemoryBootstrapConfig

    layout.config_file.chmod(0o644)
    assert (layout.config_file.stat().st_mode & 0o777) == 0o644
    created = ensure_safe_bootstrap_config(layout.config_file, BasicMemoryBootstrapConfig())
    assert created is False
    assert (layout.config_file.stat().st_mode & 0o777) == 0o600


def test_compute_desired_basic_memory_projects_direct_call_validates(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Регрессия Finding 4: Прямой вызов compute_desired_basic_memory_projects валидирует реестр и отклоняет нарушения."""
    # 1. Дубликаты project_name
    p1 = ProjectRecord.create(slug="p1", display_name="P1", kind="git", memory_project_name="ptw-dup-name")
    p2 = ProjectRecord.create(slug="p2", display_name="P2", kind="directory", memory_project_name="PTW-DUP-NAME")
    reg_dup = ProjectRegistry(projects=[p1, p2])

    with pytest.raises(ValidationError) as exc_dup:
        compute_desired_basic_memory_projects(personal_tideway_config, reg_dup)
    assert "duplicate memory.project_name" in str(exc_dup.value).lower()

    # 2. Неверный путь памяти (несоответствие projects/<id>/memory)
    p3 = ProjectRecord.create(slug="p3", display_name="P3", kind="git")
    object.__setattr__(p3.memory, "path", "projects/invalid-id/memory")
    reg_bad_path = ProjectRegistry(projects=[p3])

    with pytest.raises(ValidationError) as exc_path:
        compute_desired_basic_memory_projects(personal_tideway_config, reg_bad_path)
    assert "invalid memory path" in str(exc_path.value).lower()

    # 3. Симлинк в каталоге памяти
    outside = tmp_path / "outside_sym"
    outside.mkdir()
    p4 = ProjectRecord.create(slug="p4", display_name="P4", kind="git")
    reg_sym = ProjectRegistry(projects=[p4])

    p4_mem = personal_tideway_config.projects_dir / p4.id / "memory"
    p4_mem.parent.mkdir(parents=True, exist_ok=True)
    p4_mem.symlink_to(outside)

    with pytest.raises(BoundaryError) as exc_sym:
        compute_desired_basic_memory_projects(personal_tideway_config, reg_sym)
    assert str(exc_sym.value) == "Memory path component is a symlink, which is not permitted."
    assert str(outside) not in str(exc_sym.value)


def test_reconcile_apply_post_lock_registry_freshness_boundary(
    personal_tideway_config: PersonalTidewayConfig,
    monkeypatch: pytest.MonkeyPatch,
):
    """Дефект 1: Реестр с диска читается строго под блокировкой при registry=None.

    Мутация projects.yaml непосредственно перед yield в блокировке приводит к тому,
    что побеждает пост-локовый реестр. Явный snapshot реестра не перезагружается.
    """
    from personal_tideway.core.registry import save_registry
    import personal_tideway.core.basic_memory_reconciliation as bmr

    p_initial = ProjectRecord.create(slug="initial-proj", display_name="Initial Proj", kind="git")
    reg_initial = ProjectRegistry(projects=[p_initial])
    save_registry(reg_initial, personal_tideway_config)

    p_post_lock = ProjectRecord.create(slug="post-lock-proj", display_name="Post Lock Proj", kind="git")
    reg_post_lock = ProjectRegistry(projects=[p_post_lock])

    original_acquire = bmr.acquire_basic_memory_lock

    @contextmanager
    def mutating_acquire(cfg):
        with original_acquire(cfg):
            # Внутри блокировки до yield мутируем projects.yaml на диске
            save_registry(reg_post_lock, cfg)
            yield

    monkeypatch.setattr(bmr, "acquire_basic_memory_lock", mutating_acquire)

    # 1. apply mode с registry=None: обязан загрузить projects.yaml под блокировкой и победит reg_post_lock
    res = bmr.reconcile_basic_memory_projects(personal_tideway_config, registry=None)
    assert res.changed is True
    assert p_post_lock.memory.project_name in res.added_projects
    assert p_initial.memory.project_name not in res.added_projects

    layout = get_basic_memory_layout(personal_tideway_config)
    cfg_data = json.loads(layout.config_file.read_text(encoding="utf-8"))
    assert p_post_lock.memory.project_name in cfg_data["projects"]
    assert p_initial.memory.project_name not in cfg_data["projects"]

    # 2. apply mode с явно переданным registry: не перезагружает с диска, использует переданный snapshot
    p_explicit = ProjectRecord.create(slug="explicit-proj", display_name="Explicit Proj", kind="git")
    reg_explicit = ProjectRegistry(projects=[p_explicit])

    res_explicit = bmr.reconcile_basic_memory_projects(personal_tideway_config, registry=reg_explicit)
    assert p_explicit.memory.project_name in res_explicit.added_projects
    cfg_data_explicit = json.loads(layout.config_file.read_text(encoding="utf-8"))
    assert p_explicit.memory.project_name in cfg_data_explicit["projects"]
    assert p_post_lock.memory.project_name not in cfg_data_explicit["projects"]


def test_compute_desired_basic_memory_projects_no_validate_bypass(
    personal_tideway_config: PersonalTidewayConfig,
):
    """Дефект 2: Устранение bypass валидации в compute_desired_basic_memory_projects."""
    sig = inspect.signature(compute_desired_basic_memory_projects)
    assert "validate" not in sig.parameters
    assert tuple(sig.parameters.keys()) == ("cfg", "registry")

    # Публичный вызов всегда валидирует: дубликаты memory.project_name
    p1 = ProjectRecord.create(slug="p1", display_name="P1", kind="git", memory_project_name="dup-name")
    p2 = ProjectRecord.create(slug="p2", display_name="P2", kind="git", memory_project_name="DUP-NAME")
    reg_dup = ProjectRegistry(projects=[p1, p2])

    with pytest.raises(ValidationError):
        compute_desired_basic_memory_projects(personal_tideway_config, reg_dup)

    # Публичный вызов всегда валидирует: неверный путь памяти
    p3 = ProjectRecord.create(slug="p3", display_name="P3", kind="git")
    object.__setattr__(p3.memory, "path", "projects/corrupted-id/memory")
    reg_bad = ProjectRegistry(projects=[p3])

    with pytest.raises(ValidationError):
        compute_desired_basic_memory_projects(personal_tideway_config, reg_bad)


def test_layout_component_existing_file_fails_fail_closed(
    personal_tideway_config: PersonalTidewayConfig,
):
    """Дефект 3: Любой существующий компонент layout, не являющийся директорией, приводит к ConfigError."""
    p = ProjectRecord.create(slug="bad-type", display_name="Bad Type", kind="git")
    reg = ProjectRegistry(projects=[p])

    proj_dir = personal_tideway_config.projects_dir / p.id
    personal_tideway_config.projects_dir.mkdir(parents=True, exist_ok=True)
    canary_text = "SECRET_CANARY_FILE_CONTENT_77777"
    proj_dir.write_text(canary_text, encoding="utf-8")

    expected_msg = "Existing layout path is not a directory."

    # 1. План должен упасть с ConfigError
    with pytest.raises(ConfigError) as exc_plan:
        plan_basic_memory_projects(personal_tideway_config, reg)
    assert str(exc_plan.value) == expected_msg
    assert canary_text not in str(exc_plan.value)
    assert str(proj_dir) not in str(exc_plan.value)

    # 2. Dry-run должен упасть с ConfigError
    with pytest.raises(ConfigError) as exc_dry:
        reconcile_basic_memory_projects(personal_tideway_config, reg, dry_run=True)
    assert str(exc_dry.value) == expected_msg
    assert canary_text not in str(exc_dry.value)

    # 3. Apply должен упасть с ConfigError
    with pytest.raises(ConfigError) as exc_apply:
        reconcile_basic_memory_projects(personal_tideway_config, reg, dry_run=False)
    assert str(exc_apply.value) == expected_msg
    assert canary_text not in str(exc_apply.value)

    # 4. Проверяем также случай, когда memory root является файлом
    proj_dir.unlink()
    proj_dir.mkdir()
    note_root = proj_dir / "memory"
    note_root.write_text("NOTE_CANARY", encoding="utf-8")

    with pytest.raises(ConfigError) as exc_mem_plan:
        plan_basic_memory_projects(personal_tideway_config, reg)
    assert str(exc_mem_plan.value) == expected_msg
    assert "NOTE_CANARY" not in str(exc_mem_plan.value)

    with pytest.raises(ConfigError) as exc_mem_apply:
        reconcile_basic_memory_projects(personal_tideway_config, reg, dry_run=False)
    assert str(exc_mem_apply.value) == expected_msg

    # 5. Случай, когда сам projects_dir является файлом
    note_root.unlink()
    proj_dir.rmdir()
    personal_tideway_config.projects_dir.rmdir()
    personal_tideway_config.projects_dir.write_text("PROJECTS_DIR_CANARY", encoding="utf-8")

    with pytest.raises(ConfigError) as exc_pdir_plan:
        plan_basic_memory_projects(personal_tideway_config, reg)
    assert str(exc_pdir_plan.value) == expected_msg
    assert "PROJECTS_DIR_CANARY" not in str(exc_pdir_plan.value)

    with pytest.raises(ConfigError) as exc_pdir_apply:
        reconcile_basic_memory_projects(personal_tideway_config, reg, dry_run=False)
    assert str(exc_pdir_apply.value) == expected_msg


def test_truthful_reporting_for_permissions_and_layout_paths(
    personal_tideway_config: PersonalTidewayConfig,
):
    """Дефект 4: Правдивая отчетность has_changes и changed для всех мутаций ФС."""
    p = ProjectRecord.create(slug="truthful-proj", display_name="Truthful Proj", kind="git")
    reg = ProjectRegistry(projects=[p])
    layout = get_basic_memory_layout(personal_tideway_config)

    proj_dir = personal_tideway_config.projects_dir / p.id
    mem_dir = proj_dir / "memory"

    personal_tideway_config.projects_dir.rmdir()
    assert not personal_tideway_config.projects_dir.exists()

    # Шаг 1: Создание путей layout
    plan1 = plan_basic_memory_projects(personal_tideway_config, reg)
    assert plan1.has_changes is True
    assert plan1.changed is True
    assert personal_tideway_config.projects_dir in plan1.layout_paths_to_create
    assert proj_dir in plan1.layout_paths_to_create
    assert mem_dir in plan1.layout_paths_to_create
    assert mem_dir in plan1.memory_roots_to_create

    dict_plan1 = plan1.to_dict()
    assert str(proj_dir) in dict_plan1["layout_paths_to_create"]
    assert str(mem_dir) in dict_plan1["memory_roots_to_create"]

    res1 = reconcile_basic_memory_projects(personal_tideway_config, reg)
    assert res1.changed is True
    assert personal_tideway_config.projects_dir in res1.created_layout_paths
    assert proj_dir in res1.created_layout_paths
    assert mem_dir in res1.created_layout_paths
    assert mem_dir in res1.created_memory_roots

    dict_res1 = res1.to_dict()
    assert str(proj_dir) in dict_res1["created_layout_paths"]
    assert str(mem_dir) in dict_res1["created_memory_roots"]

    # Шаг 2: Нормализация прав config.json с 0644 до 0600
    layout.config_file.chmod(0o644)
    assert (layout.config_file.stat().st_mode & 0o777) == 0o644

    plan2 = plan_basic_memory_projects(personal_tideway_config, reg)
    assert plan2.has_changes is True
    assert plan2.changed is True
    # Важно: config_needs_write НЕ взводится исключительно из-за прав
    assert plan2.config_needs_write is False
    assert layout.config_file in plan2.permissions_to_normalize

    res2 = reconcile_basic_memory_projects(personal_tideway_config, reg)
    assert res2.changed is True
    assert res2.config_written is False
    assert layout.config_file in res2.normalized_permissions
    assert (layout.config_file.stat().st_mode & 0o777) == 0o600

    # Шаг 3: Нормализация прав каталогов с 0755 до 0700
    proj_dir.chmod(0o755)
    mem_dir.chmod(0o755)
    assert (proj_dir.stat().st_mode & 0o777) == 0o755
    assert (mem_dir.stat().st_mode & 0o777) == 0o755

    plan3 = plan_basic_memory_projects(personal_tideway_config, reg)
    assert plan3.has_changes is True
    assert proj_dir in plan3.permissions_to_normalize
    assert mem_dir in plan3.permissions_to_normalize

    res3 = reconcile_basic_memory_projects(personal_tideway_config, reg)
    assert res3.changed is True
    assert proj_dir in res3.normalized_permissions
    assert mem_dir in res3.normalized_permissions
    assert (proj_dir.stat().st_mode & 0o777) == 0o700
    assert (mem_dir.stat().st_mode & 0o777) == 0o700

    # Шаг 4: Полностью конвергированное состояние: changed=False, no writes, no mtime changes
    mtime_before = layout.config_file.stat().st_mtime_ns
    bytes_before = layout.config_file.read_bytes()

    plan4 = plan_basic_memory_projects(personal_tideway_config, reg)
    assert plan4.has_changes is False
    assert plan4.changed is False
    assert plan4.layout_paths_to_create == ()
    assert plan4.permissions_to_normalize == ()

    res4 = reconcile_basic_memory_projects(personal_tideway_config, reg)
    assert res4.changed is False
    assert res4.config_written is False
    assert res4.created_memory_roots == ()
    assert res4.created_layout_paths == ()
    assert res4.normalized_permissions == ()
    assert layout.config_file.stat().st_mtime_ns == mtime_before
    assert layout.config_file.read_bytes() == bytes_before


def test_config_path_intermediate_symlink_escape_rejected_separate_targets(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Блокер 1: Отказ при symlink в services_dir, basic_memory_dir и config_dir для plan, dry-run, apply.

    Гарантирует:
    - Проверка цепочки каталогов конфигурации до любого чтения или создания родителей.
    - Фиксированная ошибка BoundaryError("Layout path component is a symlink, which is not permitted.").
    - Внешний валидный 0644 config.json остается байт-, mode- и mtime-идентичным.
    - Никакие внешние пути или секретные канарейки не попадают в сообщения об ошибках.
    """
    import shutil

    external_canary_dir = tmp_path / "external_canary_dir_secret_canary_99999"
    external_canary_dir.mkdir(parents=True, exist_ok=True)
    external_config = external_canary_dir / "config.json"
    valid_content = (
        json.dumps(
            {
                "auto_update": False,
                "default_project": None,
                "projects": {},
            },
            indent=2,
        )
        + "\n"
    )
    external_config.write_text(valid_content, encoding="utf-8")
    external_config.chmod(0o644)

    external_bytes = external_config.read_bytes()
    assert (external_config.stat().st_mode & 0o777) == 0o644
    external_mtime = external_config.stat().st_mtime_ns

    canary = "secret_canary_99999"
    expected_err = "Layout path component is a symlink, which is not permitted."

    # --- 1. Симлинк на services_dir ---
    if personal_tideway_config.services_dir.exists():
        shutil.rmtree(personal_tideway_config.services_dir)
    personal_tideway_config.services_dir.symlink_to(external_canary_dir)

    # 1.a Plan
    with pytest.raises(BoundaryError) as exc_p1:
        plan_basic_memory_projects(personal_tideway_config)
    assert str(exc_p1.value) == expected_err
    assert canary not in str(exc_p1.value)
    assert str(external_canary_dir) not in str(exc_p1.value)

    # 1.b Dry-run
    with pytest.raises(BoundaryError) as exc_d1:
        reconcile_basic_memory_projects(personal_tideway_config, dry_run=True)
    assert str(exc_d1.value) == expected_err
    assert canary not in str(exc_d1.value)
    assert str(external_canary_dir) not in str(exc_d1.value)

    # 1.c Apply
    with pytest.raises(BoundaryError) as exc_a1:
        reconcile_basic_memory_projects(personal_tideway_config, dry_run=False)
    assert str(exc_a1.value) == expected_err
    assert canary not in str(exc_a1.value)
    assert str(external_canary_dir) not in str(exc_a1.value)

    # Проверка неизменности внешнего файла
    assert external_config.read_bytes() == external_bytes
    assert (external_config.stat().st_mode & 0o777) == 0o644
    assert external_config.stat().st_mtime_ns == external_mtime

    # --- 2. Симлинк на basic_memory_dir ---
    personal_tideway_config.services_dir.unlink()
    personal_tideway_config.services_dir.mkdir(mode=0o700, exist_ok=True)
    personal_tideway_config.basic_memory_dir.symlink_to(external_canary_dir)

    # 2.a Plan
    with pytest.raises(BoundaryError) as exc_p2:
        plan_basic_memory_projects(personal_tideway_config)
    assert str(exc_p2.value) == expected_err
    assert canary not in str(exc_p2.value)
    assert str(external_canary_dir) not in str(exc_p2.value)

    # 2.b Dry-run
    with pytest.raises(BoundaryError) as exc_d2:
        reconcile_basic_memory_projects(personal_tideway_config, dry_run=True)
    assert str(exc_d2.value) == expected_err
    assert canary not in str(exc_d2.value)
    assert str(external_canary_dir) not in str(exc_d2.value)

    # 2.c Apply
    with pytest.raises(BoundaryError) as exc_a2:
        reconcile_basic_memory_projects(personal_tideway_config, dry_run=False)
    assert str(exc_a2.value) == expected_err
    assert canary not in str(exc_a2.value)
    assert str(external_canary_dir) not in str(exc_a2.value)

    # Проверка неизменности внешнего файла
    assert external_config.read_bytes() == external_bytes
    assert (external_config.stat().st_mode & 0o777) == 0o644
    assert external_config.stat().st_mtime_ns == external_mtime

    # --- 3. Симлинк на config_dir ---
    personal_tideway_config.basic_memory_dir.unlink()
    personal_tideway_config.basic_memory_dir.mkdir(mode=0o700, exist_ok=True)
    cfg_dir = personal_tideway_config.basic_memory_dir / "config"
    cfg_dir.symlink_to(external_canary_dir)

    # 3.a Plan
    with pytest.raises(BoundaryError) as exc_p3:
        plan_basic_memory_projects(personal_tideway_config)
    assert str(exc_p3.value) == expected_err
    assert canary not in str(exc_p3.value)
    assert str(external_canary_dir) not in str(exc_p3.value)

    # 3.b Dry-run
    with pytest.raises(BoundaryError) as exc_d3:
        reconcile_basic_memory_projects(personal_tideway_config, dry_run=True)
    assert str(exc_d3.value) == expected_err
    assert canary not in str(exc_d3.value)
    assert str(external_canary_dir) not in str(exc_d3.value)

    # 3.c Apply
    with pytest.raises(BoundaryError) as exc_a3:
        reconcile_basic_memory_projects(personal_tideway_config, dry_run=False)
    assert str(exc_a3.value) == expected_err
    assert canary not in str(exc_a3.value)
    assert str(external_canary_dir) not in str(exc_a3.value)

    # Проверка неизменности внешнего файла
    assert external_config.read_bytes() == external_bytes
    assert (external_config.stat().st_mode & 0o777) == 0o644
    assert external_config.stat().st_mtime_ns == external_mtime


def test_config_path_intermediate_regular_file_rejected(
    personal_tideway_config: PersonalTidewayConfig,
):
    """Блокер 1: Отказ при наличии обычного файла вместо промежуточного каталога конфигурации.

    Гарантирует:
    - Фиксированная ошибка ConfigError("Existing layout path is not a directory.").
    - Никакие пути или содержимое файла не попадают в ошибку.
    """
    import shutil

    expected_err = "Existing layout path is not a directory."
    canary_content = "CANARY_SERVICES_REGULAR_FILE_SECRET_11111"

    # 1. services_dir как регулярный файл
    if personal_tideway_config.services_dir.exists():
        shutil.rmtree(personal_tideway_config.services_dir)
    personal_tideway_config.services_dir.write_text(canary_content, encoding="utf-8")

    with pytest.raises(ConfigError) as exc_plan:
        plan_basic_memory_projects(personal_tideway_config)
    assert str(exc_plan.value) == expected_err
    assert canary_content not in str(exc_plan.value)

    with pytest.raises(ConfigError) as exc_dry:
        reconcile_basic_memory_projects(personal_tideway_config, dry_run=True)
    assert str(exc_dry.value) == expected_err
    assert canary_content not in str(exc_dry.value)

    with pytest.raises(ConfigError) as exc_apply:
        reconcile_basic_memory_projects(personal_tideway_config, dry_run=False)
    assert str(exc_apply.value) == expected_err
    assert canary_content not in str(exc_apply.value)

    # 2. basic_memory_dir как регулярный файл
    personal_tideway_config.services_dir.unlink()
    personal_tideway_config.services_dir.mkdir(mode=0o700, exist_ok=True)
    canary_bm = "CANARY_BM_REGULAR_FILE_SECRET_22222"
    personal_tideway_config.basic_memory_dir.write_text(canary_bm, encoding="utf-8")

    with pytest.raises(ConfigError) as exc_bm_plan:
        plan_basic_memory_projects(personal_tideway_config)
    assert str(exc_bm_plan.value) == expected_err
    assert canary_bm not in str(exc_bm_plan.value)

    with pytest.raises(ConfigError) as exc_bm_dry:
        reconcile_basic_memory_projects(personal_tideway_config, dry_run=True)
    assert str(exc_bm_dry.value) == expected_err
    assert canary_bm not in str(exc_bm_dry.value)

    with pytest.raises(ConfigError) as exc_bm_apply:
        reconcile_basic_memory_projects(personal_tideway_config, dry_run=False)
    assert str(exc_bm_apply.value) == expected_err
    assert canary_bm not in str(exc_bm_apply.value)


def test_empty_registry_projects_dir_symlink_and_file_rejected_zero_mutation(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Блокер 2: Пустой реестр безусловно валидирует cfg.projects_dir (symlink и file) с нулевой мутацией."""
    reg = ProjectRegistry.empty()

    # --- 1. projects_dir как симлинк на внешний каталог ---
    outside_dir = tmp_path / "outside_projects_canary_dir_88888"
    outside_dir.mkdir(parents=True, exist_ok=True)
    canary_file = outside_dir / "external_canary.txt"
    canary_file.write_text("OUTSIDE_CANARY_SECRET_DATA_77777", encoding="utf-8")
    canary_file.chmod(0o644)
    outside_snap_before = snapshot_filesystem(outside_dir)

    if personal_tideway_config.projects_dir.exists():
        personal_tideway_config.projects_dir.rmdir()
    personal_tideway_config.projects_dir.symlink_to(outside_dir)

    home_snap_before = snapshot_filesystem(personal_tideway_config.home)
    expected_sym_err = "Layout path component is a symlink, which is not permitted."

    # 1.a Plan
    with pytest.raises(BoundaryError) as exc_plan:
        plan_basic_memory_projects(personal_tideway_config, reg)
    assert str(exc_plan.value) == expected_sym_err
    assert "88888" not in str(exc_plan.value)
    assert "77777" not in str(exc_plan.value)
    assert str(outside_dir) not in str(exc_plan.value)

    # 1.b Dry-run
    with pytest.raises(BoundaryError) as exc_dry:
        reconcile_basic_memory_projects(personal_tideway_config, reg, dry_run=True)
    assert str(exc_dry.value) == expected_sym_err
    assert "88888" not in str(exc_dry.value)
    assert "77777" not in str(exc_dry.value)
    assert str(outside_dir) not in str(exc_dry.value)

    # 1.c Apply
    with pytest.raises(BoundaryError) as exc_apply:
        reconcile_basic_memory_projects(personal_tideway_config, reg, dry_run=False)
    assert str(exc_apply.value) == expected_sym_err
    assert "88888" not in str(exc_apply.value)
    assert "77777" not in str(exc_apply.value)
    assert str(outside_dir) not in str(exc_apply.value)

    # Нулевая мутация (zero mutation)
    assert snapshot_filesystem(outside_dir) == outside_snap_before
    assert snapshot_filesystem(personal_tideway_config.home) == home_snap_before

    # --- 2. projects_dir как обычный файл ---
    personal_tideway_config.projects_dir.unlink()
    canary_file_content = "PROJECTS_DIR_REGULAR_FILE_SECRET_CANARY_55555"
    personal_tideway_config.projects_dir.write_text(canary_file_content, encoding="utf-8")
    personal_tideway_config.projects_dir.chmod(0o644)

    pdir_bytes_before = personal_tideway_config.projects_dir.read_bytes()
    pdir_mode_before = personal_tideway_config.projects_dir.stat().st_mode & 0o777
    pdir_mtime_before = personal_tideway_config.projects_dir.stat().st_mtime_ns
    home_file_snap_before = snapshot_filesystem(personal_tideway_config.home)

    expected_file_err = "Existing layout path is not a directory."

    # 2.a Plan
    with pytest.raises(ConfigError) as exc_file_plan:
        plan_basic_memory_projects(personal_tideway_config, reg)
    assert str(exc_file_plan.value) == expected_file_err
    assert canary_file_content not in str(exc_file_plan.value)
    assert "55555" not in str(exc_file_plan.value)

    # 2.b Dry-run
    with pytest.raises(ConfigError) as exc_file_dry:
        reconcile_basic_memory_projects(personal_tideway_config, reg, dry_run=True)
    assert str(exc_file_dry.value) == expected_file_err
    assert canary_file_content not in str(exc_file_dry.value)
    assert "55555" not in str(exc_file_dry.value)

    # 2.c Apply
    with pytest.raises(ConfigError) as exc_file_apply:
        reconcile_basic_memory_projects(personal_tideway_config, reg, dry_run=False)
    assert str(exc_file_apply.value) == expected_file_err
    assert canary_file_content not in str(exc_file_apply.value)
    assert "55555" not in str(exc_file_apply.value)

    # Нулевая мутация (zero mutation)
    assert personal_tideway_config.projects_dir.read_bytes() == pdir_bytes_before
    assert (personal_tideway_config.projects_dir.stat().st_mode & 0o777) == pdir_mode_before
    assert personal_tideway_config.projects_dir.stat().st_mtime_ns == pdir_mtime_before
    assert snapshot_filesystem(personal_tideway_config.home) == home_file_snap_before
