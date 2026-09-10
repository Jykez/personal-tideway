"""Детерминированная синхронизация (reconciliation) ProjectRegistry в конфигурацию Basic Memory и центральные каталоги памяти.

Реализация Work Package 10a для Personal Tideway v2:
- ProjectRegistry является единственным источником правды для проектов (git, directory, external).
- Валидация реестра и отказ при дубликатах memory.project_name (без учета регистра).
- Строгая проверка путей памяти (отсутствие symlink-компонентов, расположение строго внутри cfg.home).
- Иммутабельный чистый API планирования (plan_basic_memory_projects) без побочных эффектов на ФС.
- Безопасный preview/to_dict, исключающий утечку данных и учетных записей Basic Memory.
- Сохранение неизменными всех сторонних настроек верхнего уровня существующей конфигурации Basic Memory.
- Безопасное применение (reconcile_basic_memory_projects) с захватом блокировки install_lock и атомарной записью 0600.
- Идемпотентность: повторный запуск на уже синхронизированном состоянии не меняет байты и mtime.
- Удаление проектов затрагивает только привязки в конфигурации: файлы заметок и каталоги памяти никогда не удаляются.
- Полное отсутствие загрязнения репозиториев (source roots) служебными файлами Personal Tideway.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import stat
from types import MappingProxyType
from typing import Any
import uuid

from personal_tideway.config import PersonalTidewayConfig, validate_owned_path
from personal_tideway.core.basic_memory_installer import (
    acquire_basic_memory_lock,
    read_safe_basic_memory_config,
)
from personal_tideway.core.basic_memory_runtime import get_basic_memory_layout
from personal_tideway.core.registry import ProjectRecord, ProjectRegistry, load_registry
from personal_tideway.exceptions import BoundaryError, ConfigError, ValidationError

# Режим проекта в изолированном Basic Memory
BASIC_MEMORY_PROJECT_MODE: str = "local"

# Тип пользовательского инжектируемого писателя конфигурации
ConfigWriter = Callable[[Path, str], Any]


def _validate_config_parent_chain(cfg: PersonalTidewayConfig) -> Path:
    """Валидировать полную цепочку родительских каталогов конфигурации Basic Memory без перехода по симлинкам.

    Проверяет:
    1. cfg.home не является симлинком.
    2. Все существующие компоненты между cfg.home и layout.config_dir являются настоящими директориями.
    3. Любой симлинк в цепочке вызывает фиксированный BoundaryError:
       'Layout path component is a symlink, which is not permitted.'
    4. Любой другой объект (файл, сокет, fifo) вызывает фиксированный ConfigError:
       'Existing layout path is not a directory.'
    5. validate_owned_path применяется строго после всех lstat-проверок.
    6. Исключения никогда не содержат путей или конфиденциального содержимого.

    Возвращает путь к config_file.
    """
    if cfg.home.is_symlink():
        raise BoundaryError("Workspace root is a symlink, which is not permitted.")

    config_dir = cfg.basic_memory_dir / "config"
    config_file = config_dir / "config.json"

    chain: list[Path] = []
    curr = config_dir
    while curr != cfg.home and curr != curr.parent:
        chain.append(curr)
        curr = curr.parent

    if curr != cfg.home:
        raise BoundaryError("Layout path component is a symlink, which is not permitted.")

    chain.reverse()

    for comp in chain:
        try:
            st = os.lstat(comp)
            exists = True
        except (FileNotFoundError, NotADirectoryError):
            exists = False
        except OSError:
            raise ConfigError("Failed to inspect layout directory.") from None

        if exists:
            if stat.S_ISLNK(st.st_mode):
                raise BoundaryError("Layout path component is a symlink, which is not permitted.")
            if not stat.S_ISDIR(st.st_mode):
                raise ConfigError("Existing layout path is not a directory.")

    validate_owned_path(config_dir, root=cfg.home, allow_root=False)
    return config_file


def _validate_projects_dir_layout(cfg: PersonalTidewayConfig) -> None:
    """Валидировать cfg.projects_dir без перехода по симлинкам перед резолвом.

    Проверяет:
    1. cfg.home не является симлинком.
    2. Все существующие компоненты пути от cfg.home до cfg.projects_dir являются настоящими директориями.
    3. Любой симлинк вызывает BoundaryError('Layout path component is a symlink, which is not permitted.').
    4. Любой другой объект вызывает ConfigError('Existing layout path is not a directory.').
    5. validate_owned_path применяется строго после всех lstat-проверок.
    """
    if cfg.home.is_symlink():
        raise BoundaryError("Workspace root is a symlink, which is not permitted.")

    chain: list[Path] = []
    curr = cfg.projects_dir
    while curr != cfg.home and curr != curr.parent:
        chain.append(curr)
        curr = curr.parent

    if curr != cfg.home:
        raise BoundaryError("Layout path component is a symlink, which is not permitted.")

    chain.reverse()

    for comp in chain:
        try:
            st = os.lstat(comp)
            exists = True
        except (FileNotFoundError, NotADirectoryError):
            exists = False
        except OSError:
            raise ConfigError("Failed to inspect layout directory.") from None

        if exists:
            if stat.S_ISLNK(st.st_mode):
                raise BoundaryError("Layout path component is a symlink, which is not permitted.")
            if not stat.S_ISDIR(st.st_mode):
                raise ConfigError("Existing layout path is not a directory.")

    validate_owned_path(cfg.projects_dir, root=cfg.home, allow_root=False)


def validate_registry_for_basic_memory(cfg: PersonalTidewayConfig, registry: ProjectRegistry) -> None:
    """Валидировать реестр проектов для безопасной интеграции с Basic Memory.

    Проверяет:
    1. Отсутствие симлинков в корне рабочего пространства cfg.home.
    2. Безусловная валидация центрального каталога проектов cfg.projects_dir до цикла по проектам.
    3. Базовую валидность схемы реестра (registry.validate()).
    4. Регистронезависимую уникальность memory.project_name среди всех проектов.
    5. Строгое соответствие каждого derived note root пути cfg.home / 'projects' / rec.id / 'memory'.
    6. Нахождение каждого note root строго внутри cfg.home без побегов.
    7. Полное отсутствие symlink-компонентов во всей цепочке путей до каждого note root.
    8. Все существующие компоненты layout являются настоящими директориями (не регулярными файлами/FIFO/сокетами).
    """
    if cfg.home.is_symlink():
        raise BoundaryError("Workspace root is a symlink, which is not permitted.")

    # 1. Безусловная валидация cfg.projects_dir ДО цикла по проектам (исключает empty-registry bypass)
    _validate_projects_dir_layout(cfg)

    # 2. Валидация базовых инвариантов реестра
    registry.validate()

    seen_mem_names: dict[str, str] = {}
    for rec in registry.projects:
        mem_name = rec.memory.project_name
        if not mem_name or not isinstance(mem_name, str) or not mem_name.strip():
            raise ValidationError(f"Project '{rec.slug}' has empty memory.project_name.")
        clean_name = mem_name.strip()
        lower_name = clean_name.lower()
        if lower_name in seen_mem_names:
            raise ValidationError(
                f"Duplicate memory.project_name '{clean_name}' (case-insensitive collision with '{seen_mem_names[lower_name]}')."
            )
        seen_mem_names[lower_name] = clean_name

        # Проверка derived note root
        expected_rel_path = f"projects/{rec.id}/memory"
        if rec.memory.path != expected_rel_path:
            raise ValidationError(
                f"Invalid memory path for project '{rec.id}': expected '{expected_rel_path}', got '{rec.memory.path}'."
            )

        note_root = cfg.home / rec.memory.path
        expected_note_root = cfg.projects_dir / rec.id / "memory"
        if note_root != expected_note_root:
            raise ValidationError(
                f"Derived note root '{note_root}' does not match expected '{expected_note_root}'."
            )

        # 3. Проверка отсутствия симлинков и подтверждение, что существующие компоненты пути — директории
        # ДО проверки границ владения через validate_owned_path
        # cfg.projects_dir уже валидирован безусловно, проверяем proj_dir и note_root без повторного сканирования базы
        proj_dir = cfg.projects_dir / rec.id
        path_targets = (
            proj_dir,
            note_root,
        )
        for target in path_targets:
            curr = target
            while curr != cfg.projects_dir and curr != cfg.home and curr != curr.parent:
                try:
                    st = os.lstat(curr)
                    exists = True
                except (FileNotFoundError, NotADirectoryError):
                    exists = False
                except OSError:
                    raise ConfigError("Failed to inspect memory path component.") from None

                if exists:
                    if stat.S_ISLNK(st.st_mode):
                        raise BoundaryError("Memory path component is a symlink, which is not permitted.")
                    if not stat.S_ISDIR(st.st_mode):
                        raise ConfigError("Existing layout path is not a directory.")
                curr = curr.parent

        # 4. Только после подтверждения отсутствия симлинков и проверки типов директорий проверяем границы владения
        validate_owned_path(note_root, root=cfg.home, allow_root=False)


def _compute_desired_basic_memory_projects_unchecked(
    cfg: PersonalTidewayConfig,
    registry: ProjectRegistry,
) -> dict[str, dict[str, str]]:
    """Внутреннее построение отображения проектов Basic Memory без повторной валидации."""
    desired: dict[str, dict[str, str]] = {}
    for rec in registry.projects:
        note_root = cfg.home / rec.memory.path
        desired[rec.memory.project_name] = {
            "path": str(note_root),
            "mode": BASIC_MEMORY_PROJECT_MODE,
        }
    return desired


def compute_desired_basic_memory_projects(
    cfg: PersonalTidewayConfig,
    registry: ProjectRegistry,
) -> dict[str, dict[str, str]]:
    """Построить желаемое отображение проектов Basic Memory на основе ProjectRegistry.

    Каждая валидная запись вида git, directory или external отображается в ровно один
    локальный проект Basic Memory с ключом record.memory.project_name и значением
    {'path': str(cfg.home / record.memory.path), 'mode': 'local'}.
    Внешние проекты включаются на общих основаниях, несмотря на отсутствие привязок к репозиторию.

    Всегда выполняет строгую валидацию реестра через validate_registry_for_basic_memory.
    Публичный API не позволяет обойти валидацию.
    """
    validate_registry_for_basic_memory(cfg, registry)
    return _compute_desired_basic_memory_projects_unchecked(cfg, registry)


@dataclass(frozen=True)
class BasicMemoryProjectPlan:
    """Чистый иммутабельный план синхронизации ProjectRegistry с Basic Memory."""

    desired_projects: Mapping[str, Mapping[str, str]]
    added_projects: tuple[str, ...] = field(default_factory=tuple)
    updated_projects: tuple[str, ...] = field(default_factory=tuple)
    removed_projects: tuple[str, ...] = field(default_factory=tuple)
    memory_roots_to_create: tuple[Path, ...] = field(default_factory=tuple)
    layout_paths_to_create: tuple[Path, ...] = field(default_factory=tuple)
    permissions_to_normalize: tuple[Path, ...] = field(default_factory=tuple)
    config_path: Path = field(default=Path())
    config_needs_write: bool = False

    def __post_init__(self) -> None:
        # Глубокое защитное замораживание структур для гарантированной иммутабельности
        frozen_desired: dict[str, Mapping[str, str]] = {
            k: MappingProxyType(dict(v)) for k, v in self.desired_projects.items()
        }
        object.__setattr__(self, "desired_projects", MappingProxyType(frozen_desired))
        object.__setattr__(self, "added_projects", tuple(self.added_projects))
        object.__setattr__(self, "updated_projects", tuple(self.updated_projects))
        object.__setattr__(self, "removed_projects", tuple(self.removed_projects))
        object.__setattr__(self, "memory_roots_to_create", tuple(self.memory_roots_to_create))
        object.__setattr__(self, "layout_paths_to_create", tuple(self.layout_paths_to_create))
        object.__setattr__(self, "permissions_to_normalize", tuple(self.permissions_to_normalize))

    @property
    def has_changes(self) -> bool:
        """Возвращает True, если план предполагает какие-либо изменения на диске."""
        return bool(
            self.config_needs_write
            or self.added_projects
            or self.updated_projects
            or self.removed_projects
            or self.memory_roots_to_create
            or self.layout_paths_to_create
            or self.permissions_to_normalize
        )

    @property
    def changed(self) -> bool:
        """Совместимый алиас для has_changes."""
        return self.has_changes

    def to_dict(self) -> dict[str, Any]:
        """Безопасное представление плана без раскрытия посторонних настроек или секретов."""
        return {
            "desired_projects_count": len(self.desired_projects),
            "added_projects": list(self.added_projects),
            "updated_projects": list(self.updated_projects),
            "removed_projects": list(self.removed_projects),
            "memory_roots_to_create": [str(p) for p in self.memory_roots_to_create],
            "layout_paths_to_create": [str(p) for p in self.layout_paths_to_create],
            "permissions_to_normalize": [str(p) for p in self.permissions_to_normalize],
            "config_path": str(self.config_path),
            "config_needs_write": self.config_needs_write,
            "has_changes": self.has_changes,
            "changed": self.changed,
        }


@dataclass(frozen=True)
class BasicMemoryProjectReconciliationResult:
    """Иммутабельный результат выполнения синхронизации проектов с Basic Memory."""

    dry_run: bool
    changed: bool
    config_written: bool
    created_memory_roots: tuple[Path, ...] = field(default_factory=tuple)
    created_layout_paths: tuple[Path, ...] = field(default_factory=tuple)
    normalized_permissions: tuple[Path, ...] = field(default_factory=tuple)
    added_projects: tuple[str, ...] = field(default_factory=tuple)
    updated_projects: tuple[str, ...] = field(default_factory=tuple)
    removed_projects: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_memory_roots", tuple(self.created_memory_roots))
        object.__setattr__(self, "created_layout_paths", tuple(self.created_layout_paths))
        object.__setattr__(self, "normalized_permissions", tuple(self.normalized_permissions))
        object.__setattr__(self, "added_projects", tuple(self.added_projects))
        object.__setattr__(self, "updated_projects", tuple(self.updated_projects))
        object.__setattr__(self, "removed_projects", tuple(self.removed_projects))

    def to_dict(self) -> dict[str, Any]:
        """Безопасное представление результата в виде словаря."""
        return {
            "dry_run": self.dry_run,
            "changed": self.changed,
            "config_written": self.config_written,
            "created_memory_roots": [str(p) for p in self.created_memory_roots],
            "created_layout_paths": [str(p) for p in self.created_layout_paths],
            "normalized_permissions": [str(p) for p in self.normalized_permissions],
            "added_projects": list(self.added_projects),
            "updated_projects": list(self.updated_projects),
            "removed_projects": list(self.removed_projects),
        }


def _ensure_directory_mode_700(
    target: Path,
    home_boundary: Path,
    *,
    is_layout: bool = False,
) -> tuple[bool, bool]:
    """Убедиться в существовании директории с правами 0700 и отсутствием симлинков в цепочке.

    Возвращает:
        (created: bool, chmodded: bool)
    """
    if home_boundary.is_symlink():
        raise BoundaryError("Workspace root is a symlink, which is not permitted.")

    symlink_err = (
        "Layout path component is a symlink, which is not permitted."
        if is_layout
        else "Memory path component is a symlink, which is not permitted."
    )

    curr = target
    while curr != home_boundary and curr != curr.parent:
        try:
            st_curr = os.lstat(curr)
            curr_exists = True
        except (FileNotFoundError, NotADirectoryError):
            curr_exists = False
        except OSError:
            raise ConfigError("Failed to inspect directory.") from None

        if curr_exists:
            if stat.S_ISLNK(st_curr.st_mode):
                raise BoundaryError(symlink_err)
            if not stat.S_ISDIR(st_curr.st_mode):
                raise ConfigError("Existing layout path is not a directory.")
        curr = curr.parent

    validate_owned_path(target, root=home_boundary, allow_root=False)

    try:
        st = os.lstat(target)
        exists = True
    except FileNotFoundError:
        exists = False
    except OSError:
        raise ConfigError("Failed to inspect directory.") from None

    if exists:
        if stat.S_ISLNK(st.st_mode):
            raise BoundaryError(symlink_err)
        if not stat.S_ISDIR(st.st_mode):
            raise ConfigError("Existing layout path is not a directory.")
        if stat.S_IMODE(st.st_mode) != 0o700:
            try:
                os.chmod(target, 0o700)
            except OSError:
                raise ConfigError("Failed to normalize permissions on directory.") from None
            return False, True
        return False, False
    else:
        try:
            target.mkdir(mode=0o700, parents=False, exist_ok=False)
            os.chmod(target, 0o700)
        except OSError:
            raise ConfigError("Failed to create directory.") from None
        return True, False


def write_safe_basic_memory_config(config_file: Path, content: str) -> None:
    """Атомарно записать конфигурацию Basic Memory с правами 0600, сохраняя исходную при сбое."""
    config_dir = config_file.parent
    tmp_file = config_dir / f".tmp_config_{uuid.uuid4().hex}"
    content_bytes = content.encode("utf-8")

    try:
        tmp_fd = os.open(
            tmp_file,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            total_written = 0
            while total_written < len(content_bytes):
                written = os.write(tmp_fd, content_bytes[total_written:])
                if written <= 0:
                    raise ConfigError("Failed to write safe config file.")
                total_written += written
            os.fsync(tmp_fd)
            try:
                os.fchmod(tmp_fd, 0o600)
            except OSError:
                pass
        finally:
            os.close(tmp_fd)

        # Атомарная замена существующего файла с правами 0600
        os.replace(tmp_file, config_file)
        try:
            os.chmod(config_file, 0o600)
        except OSError:
            pass
    except ConfigError:
        if tmp_file.exists():
            try:
                tmp_file.unlink()
            except OSError:
                pass
        raise
    except Exception:
        if tmp_file.exists():
            try:
                tmp_file.unlink()
            except OSError:
                pass
        raise ConfigError("Failed to write Basic Memory project configuration.") from None


def plan_basic_memory_projects(
    cfg: PersonalTidewayConfig,
    registry: ProjectRegistry | None = None,
) -> BasicMemoryProjectPlan:
    """Построить чистый иммутабельный план синхронизации реестра проектов с Basic Memory.

    Выполняет нулевые операции записи, не создает каталогов, не меняет права/mtime существующих
    файлов и не захватывает блокировок.
    Загружает существующий конфиг строго только для чтения с normalize_permissions=False.
    """
    if cfg.home.is_symlink():
        raise BoundaryError("Workspace root is a symlink, which is not permitted.")

    # 1. Валидация статических компонентов layout (projects_dir и цепочка конфига) до чтения файлов
    _validate_projects_dir_layout(cfg)
    config_file = _validate_config_parent_chain(cfg)

    if registry is None:
        registry = load_registry(cfg.projects_yaml)

    # 2. Валидируем реестр и вычисляем желаемые проекты (включая безусловную проверку cfg.projects_dir)
    desired_projects = compute_desired_basic_memory_projects(cfg, registry)

    layout = get_basic_memory_layout(cfg)

    # Чистое чтение без нормализации прав (zero mutation)
    existing_config = read_safe_basic_memory_config(config_file, normalize_permissions=False)
    existing_projects = existing_config.get("projects", {}) if existing_config is not None else {}

    added: list[str] = []
    updated: list[str] = []
    for name, desired_val in desired_projects.items():
        if name not in existing_projects:
            added.append(name)
        elif existing_projects[name] != desired_val:
            updated.append(name)

    removed: list[str] = [name for name in existing_projects if name not in desired_projects]

    if existing_config is None:
        config_needs_write = True
    else:
        config_converged = (
            existing_config.get("auto_update") is False
            and existing_config.get("default_project") is None
            and existing_config.get("projects") == desired_projects
        )
        config_needs_write = not config_converged

    # Проверка каталогов и прав доступа строго только для чтения (через lstat)
    missing_roots: list[Path] = []
    paths_to_create: list[Path] = []
    perms_to_normalize: list[Path] = []

    # 1. Проверяем cfg.projects_dir: никогда не классифицируем симлинк/не-директорию как кандидата на chmod
    try:
        st_pdir = os.lstat(cfg.projects_dir)
        if stat.S_ISLNK(st_pdir.st_mode):
            raise BoundaryError("Layout path component is a symlink, which is not permitted.")
        if not stat.S_ISDIR(st_pdir.st_mode):
            raise ConfigError("Existing layout path is not a directory.")
        if stat.S_IMODE(st_pdir.st_mode) != 0o700:
            perms_to_normalize.append(cfg.projects_dir)
    except FileNotFoundError:
        paths_to_create.append(cfg.projects_dir)

    # 2. Проверяем каталоги каждого проекта и каталоги заметок
    for rec in registry.projects:
        proj_dir = cfg.projects_dir / rec.id
        try:
            st_proj = os.lstat(proj_dir)
            if stat.S_ISLNK(st_proj.st_mode):
                raise BoundaryError("Memory path component is a symlink, which is not permitted.")
            if not stat.S_ISDIR(st_proj.st_mode):
                raise ConfigError("Existing layout path is not a directory.")
            if stat.S_IMODE(st_proj.st_mode) != 0o700:
                perms_to_normalize.append(proj_dir)
        except FileNotFoundError:
            paths_to_create.append(proj_dir)

        note_root = cfg.home / rec.memory.path
        try:
            st_note = os.lstat(note_root)
            if stat.S_ISLNK(st_note.st_mode):
                raise BoundaryError("Memory path component is a symlink, which is not permitted.")
            if not stat.S_ISDIR(st_note.st_mode):
                raise ConfigError("Existing layout path is not a directory.")
            if stat.S_IMODE(st_note.st_mode) != 0o700:
                perms_to_normalize.append(note_root)
        except FileNotFoundError:
            paths_to_create.append(note_root)
            missing_roots.append(note_root)

    # 3. Если требуется запись конфига, проверяем его родительские каталоги
    if config_needs_write:
        for parent_d in (cfg.services_dir, cfg.basic_memory_dir, config_file.parent):
            try:
                st_par = os.lstat(parent_d)
                if stat.S_ISLNK(st_par.st_mode):
                    raise BoundaryError("Layout path component is a symlink, which is not permitted.")
                if not stat.S_ISDIR(st_par.st_mode):
                    raise ConfigError("Existing layout path is not a directory.")
                if stat.S_IMODE(st_par.st_mode) != 0o700:
                    if parent_d not in perms_to_normalize:
                        perms_to_normalize.append(parent_d)
            except FileNotFoundError:
                if parent_d not in paths_to_create:
                    paths_to_create.append(parent_d)

    # 4. Проверяем права существующего config_file (если он есть)
    if existing_config is not None:
        try:
            st_cfg = os.lstat(config_file)
            if stat.S_ISLNK(st_cfg.st_mode):
                raise ConfigError("Existing config file is a symlink, which is not permitted.")
            if not stat.S_ISREG(st_cfg.st_mode):
                raise ConfigError("Existing config file is not a regular file.")
            if stat.S_IMODE(st_cfg.st_mode) != 0o600:
                perms_to_normalize.append(config_file)
        except (FileNotFoundError, OSError):
            pass

    return BasicMemoryProjectPlan(
        desired_projects=desired_projects,
        added_projects=tuple(sorted(added)),
        updated_projects=tuple(sorted(updated)),
        removed_projects=tuple(sorted(removed)),
        memory_roots_to_create=tuple(missing_roots),
        layout_paths_to_create=tuple(paths_to_create),
        permissions_to_normalize=tuple(perms_to_normalize),
        config_path=config_file,
        config_needs_write=config_needs_write,
    )


# Канонический алиас для планирования
plan_project_reconciliation = plan_basic_memory_projects


def reconcile_basic_memory_projects(
    cfg: PersonalTidewayConfig,
    registry: ProjectRegistry | None = None,
    *,
    dry_run: bool = False,
    writer: ConfigWriter | None = None,
) -> BasicMemoryProjectReconciliationResult:
    """Выполнить детерминированную синхронизацию ProjectRegistry в конфигурацию Basic Memory.

    Гарантии:
    - Реестр является единственным источником правды.
    - В режиме dry_run=True: ноль записей, блокировка не берется, writer не вызывается, каталоги не создаются, права не меняются.
    - В режиме применения: берется эксклюзивная блокировка cfg.locks_dir/basic-memory-install.lock.
    - Реестр с диска читается и валидируется строго под блокировкой при registry=None.
    - Создаются только центральные каталоги проектов и заметок с правами 0700.
    - Заменяется только секция 'projects' в config.json, подтверждаются auto_update=false и default_project=null.
    - Все сторонние настройки Basic Memory сохраняются неизменными.
    - Идемпотентность: если состояние уже конвергировано, байты и mtime не перезаписываются.
    - Атомарная запись конфигурации с правами 0600. При ошибках старая конфигурация и заметки сохраняются.
    - Исходные репозитории (source roots) остаются абсолютно нетронутыми.
    - Удаление проектов убирает их из Basic Memory конфигурации, но не удаляет заметки и каталоги памяти.
    """
    if cfg.home.is_symlink():
        raise BoundaryError("Workspace root is a symlink, which is not permitted.")

    # 1. Чистые статические проверки границ layout ДО захвата блокировки и до режима dry-run
    _validate_projects_dir_layout(cfg)
    config_file = _validate_config_parent_chain(cfg)

    if dry_run:
        # Режим предварительного просмотра: ноль записей, ноль блокировок, ноль мутаций ФС
        if registry is None:
            registry = load_registry(cfg.projects_yaml)
        plan = plan_basic_memory_projects(cfg, registry)
        return BasicMemoryProjectReconciliationResult(
            dry_run=True,
            changed=plan.has_changes,
            config_written=False,
            created_memory_roots=(),
            created_layout_paths=(),
            normalized_permissions=(),
            added_projects=plan.added_projects,
            updated_projects=plan.updated_projects,
            removed_projects=plan.removed_projects,
        )

    # Реальный режим (apply): захватываем эксклюзивную блокировку Basic Memory ДО загрузки реестра с диска
    caller_supplied_registry = registry is not None

    with acquire_basic_memory_lock(cfg):
        # 2. Повторная валидация под блокировкой для защиты от гонок
        _validate_projects_dir_layout(cfg)
        _validate_config_parent_chain(cfg)

        if not caller_supplied_registry:
            registry = load_registry(cfg.projects_yaml)

        # Валидация под блокировкой и вычисление целевых проектов
        validate_registry_for_basic_memory(cfg, registry)
        desired_projects = _compute_desired_basic_memory_projects_unchecked(cfg, registry)

        created_layout_paths: list[Path] = []
        normalized_permissions: list[Path] = []

        # Безопасно проверяем режим config_file до чтения с нормализацией
        config_mode_needs_norm = False
        try:
            st_cfg_pre = os.lstat(config_file)
            if stat.S_ISREG(st_cfg_pre.st_mode) and stat.S_IMODE(st_cfg_pre.st_mode) != 0o600:
                config_mode_needs_norm = True
        except (FileNotFoundError, OSError):
            pass

        # Под блокировкой разрешена нормализация прав существующего файла к 0600
        existing_config = read_safe_basic_memory_config(config_file, normalize_permissions=True)
        if config_mode_needs_norm and existing_config is not None:
            normalized_permissions.append(config_file)

        existing_projects = existing_config.get("projects", {}) if existing_config is not None else {}

        added: list[str] = []
        updated: list[str] = []
        for name, desired_val in desired_projects.items():
            if name not in existing_projects:
                added.append(name)
            elif existing_projects[name] != desired_val:
                updated.append(name)

        removed: list[str] = [name for name in existing_projects if name not in desired_projects]

        # Создаем / нормализуем центральные каталоги проектов и заметок с правами 0700
        pdir_created, pdir_chmod = _ensure_directory_mode_700(cfg.projects_dir, cfg.home, is_layout=True)
        if pdir_created:
            created_layout_paths.append(cfg.projects_dir)
        if pdir_chmod:
            normalized_permissions.append(cfg.projects_dir)

        created_roots: list[Path] = []
        for rec in registry.projects:
            proj_dir = cfg.projects_dir / rec.id
            mem_dir = proj_dir / "memory"

            p_created, p_chmod = _ensure_directory_mode_700(proj_dir, cfg.home, is_layout=False)
            if p_created:
                created_layout_paths.append(proj_dir)
            if p_chmod:
                normalized_permissions.append(proj_dir)

            m_created, m_chmod = _ensure_directory_mode_700(mem_dir, cfg.home, is_layout=False)
            if m_created:
                created_layout_paths.append(mem_dir)
                created_roots.append(mem_dir)
            if m_chmod:
                normalized_permissions.append(mem_dir)

        # Проверяем семантическую конвергентность существующего конфига
        config_converged = (
            existing_config is not None
            and existing_config.get("auto_update") is False
            and existing_config.get("default_project") is None
            and existing_config.get("projects") == desired_projects
        )

        config_written = False
        if not config_converged:
            # 3. Валидация цепочки каталогов конфигурации перед созданием/записью родительских каталогов
            _validate_config_parent_chain(cfg)

            # Формируем обновленный словарь с сохранением всех сторонних параметров
            new_config = dict(existing_config) if existing_config is not None else {}
            new_config["auto_update"] = False
            new_config["default_project"] = None
            new_config["projects"] = desired_projects

            content_str = json.dumps(new_config, indent=2, sort_keys=True) + "\n"

            # Гарантируем наличие родительских каталогов конфигурации с 0700
            for parent_d in (cfg.services_dir, cfg.basic_memory_dir, config_file.parent):
                d_created, d_chmod = _ensure_directory_mode_700(parent_d, cfg.home, is_layout=True)
                if d_created:
                    created_layout_paths.append(parent_d)
                if d_chmod:
                    normalized_permissions.append(parent_d)

            if writer is not None:
                try:
                    writer(config_file, content_str)
                except Exception:
                    raise ConfigError("Failed to write Basic Memory project configuration.") from None
                config_written = True
            else:
                try:
                    write_safe_basic_memory_config(config_file, content_str)
                except Exception:
                    raise ConfigError("Failed to write Basic Memory project configuration.") from None
                config_written = True

        changed = bool(
            config_written
            or created_roots
            or created_layout_paths
            or normalized_permissions
            or added
            or updated
            or removed
        )

        return BasicMemoryProjectReconciliationResult(
            dry_run=False,
            changed=changed,
            config_written=config_written,
            created_memory_roots=tuple(created_roots),
            created_layout_paths=tuple(created_layout_paths),
            normalized_permissions=tuple(normalized_permissions),
            added_projects=tuple(sorted(added)),
            updated_projects=tuple(sorted(updated)),
            removed_projects=tuple(sorted(removed)),
        )


# Канонический алиас для примирения проектов
reconcile_projects = reconcile_basic_memory_projects

__all__ = [
    "BASIC_MEMORY_PROJECT_MODE",
    "BasicMemoryProjectPlan",
    "BasicMemoryProjectReconciliationResult",
    "ConfigWriter",
    "compute_desired_basic_memory_projects",
    "plan_basic_memory_projects",
    "plan_project_reconciliation",
    "reconcile_basic_memory_projects",
    "reconcile_projects",
    "validate_registry_for_basic_memory",
    "write_safe_basic_memory_config",
]
