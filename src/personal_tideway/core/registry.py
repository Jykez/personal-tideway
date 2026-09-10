"""Personal Tideway v2 project registry schema, models, and persistence.

Ordering policy:
Stable insertion order is strictly preserved. Projects are stored, iterated,
and serialized in the exact order they were registered. Updates preserve
the project's index in the list. This guarantees deterministic presentation
and idempotent round-trips.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from pathlib import Path
import re
from typing import Any
import uuid
import yaml

from personal_tideway.config import PersonalTidewayConfig, validate_owned_path
from personal_tideway.constants import (
    DEFAULT_MEMORY_BACKEND,
    PROJECT_KIND_DIRECTORY,
    PROJECT_KIND_EXTERNAL,
    PROJECT_KIND_GIT,
    SCHEMA_VERSION,
    SUPPORTED_PROJECT_KINDS,
)
from personal_tideway.exceptions import ConfigError, ValidationError
from personal_tideway.utils import atomic_write_text


SLUG_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]+$")
ALLOWED_RECORD_KEYS = {
    "id",
    "slug",
    "display_name",
    "kind",
    "aliases",
    "bindings",
    "memory",
    "created_at",
    "updated_at",
}
ALLOWED_BINDINGS_KEYS = {"paths", "git_common_dirs", "git_remotes"}
ALLOWED_MEMORY_KEYS = {"backend", "project_name", "path"}
ALLOWED_REGISTRY_KEYS = {"version", "projects"}


def now_rfc3339() -> str:
    """Return current UTC timestamp in RFC3339 format ending with 'Z'."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_rfc3339_utc(ts: Any, field_name: str) -> str:
    """Validate that timestamp is a non-empty RFC3339 UTC string."""
    if not isinstance(ts, str) or not ts.strip():
        raise ValidationError(f"Field '{field_name}' must be a non-empty RFC3339 UTC timestamp string.")
    clean = ts.strip()
    if "T" not in clean:
        raise ValidationError(f"Field '{field_name}' must be an RFC3339 timestamp with 'T' separator, got '{clean}'.")
    if not (clean.endswith("Z") or clean.endswith("+00:00")):
        raise ValidationError(
            f"Field '{field_name}' must be in UTC timezone (ending with 'Z' or '+00:00'), got '{clean}'."
        )
    iso_str = clean[:-1] + "+00:00" if clean.endswith("Z") else clean
    try:
        dt = datetime.fromisoformat(iso_str)
    except Exception:
        raise ValidationError(f"Invalid RFC3339 format for '{field_name}': '{clean}'.")
    if dt.tzinfo is None or dt.utcoffset() is None or dt.utcoffset().total_seconds() != 0:
        raise ValidationError(f"Field '{field_name}' must have UTC offset 0, got '{clean}'.")
    return clean


def validate_uuid4(val: Any, field_name: str = "id") -> str:
    """Validate that value is a valid UUID4 string."""
    if not isinstance(val, str) or not val.strip():
        raise ValidationError(f"Field '{field_name}' must be a non-empty UUID4 string.")
    clean = val.strip().lower()
    try:
        u = uuid.UUID(clean)
    except (ValueError, AttributeError):
        raise ValidationError(f"Field '{field_name}' is not a valid UUID: '{val}'.")
    if u.version != 4:
        raise ValidationError(f"Field '{field_name}' must be UUID version 4, got version {u.version}.")
    if str(u) != clean:
        raise ValidationError(f"Field '{field_name}' must be a canonical formatted UUID string: '{val}'.")
    return clean


def validate_token_format(token: Any, field_name: str) -> str:
    """Validate slug or alias token format (alphanumeric, dashes, underscores)."""
    if not isinstance(token, str) or not token.strip():
        raise ValidationError(f"Field '{field_name}' must be a non-empty string.")
    clean = token.strip()
    if not SLUG_PATTERN.match(clean):
        raise ValidationError(
            f"Field '{field_name}' '{clean}' is invalid. Must contain only letters, numbers, dashes, or underscores."
        )
    return clean


@dataclass
class ProjectBindings:
    """Filesystem and Git bindings for a project."""
    paths: list[str] = field(default_factory=list)
    git_common_dirs: list[str] = field(default_factory=list)
    git_remotes: list[str] = field(default_factory=list)

    def validate(self, kind: str, project_slug: str) -> None:
        """Validate bindings consistency and constraints."""
        if not isinstance(self.paths, list):
            raise ValidationError(f"Project '{project_slug}' bindings.paths must be a list.")
        if not isinstance(self.git_common_dirs, list):
            raise ValidationError(f"Project '{project_slug}' bindings.git_common_dirs must be a list.")
        if not isinstance(self.git_remotes, list):
            raise ValidationError(f"Project '{project_slug}' bindings.git_remotes must be a list.")

        # External projects must have no path or git bindings
        if kind == PROJECT_KIND_EXTERNAL:
            if self.paths:
                raise ValidationError(f"External project '{project_slug}' must not have path bindings.")
            if self.git_common_dirs or self.git_remotes:
                raise ValidationError(f"External project '{project_slug}' must not have git bindings.")
            return

        # Paths validation
        seen_paths: set[str] = set()
        for p in self.paths:
            if not isinstance(p, str) or not p.strip():
                raise ValidationError(f"Project '{project_slug}' path binding must be a non-empty string.")
            clean_p = p.strip()
            if not Path(clean_p).is_absolute():
                raise ValidationError(
                    f"Path binding '{clean_p}' in project '{project_slug}' must be an absolute path, got relative path."
                )
            norm_p = os.path.normpath(clean_p)
            if norm_p in seen_paths:
                raise ValidationError(f"Duplicate path '{clean_p}' in bindings for project '{project_slug}'.")
            seen_paths.add(norm_p)

        # Git common dirs validation
        seen_gcds: set[str] = set()
        for gcd in self.git_common_dirs:
            if not isinstance(gcd, str) or not gcd.strip():
                raise ValidationError(
                    f"Project '{project_slug}' git_common_dir binding must be a non-empty string."
                )
            clean_gcd = gcd.strip()
            if not Path(clean_gcd).is_absolute():
                raise ValidationError(
                    f"git_common_dir binding '{clean_gcd}' in project '{project_slug}' must be an absolute path, got relative path."
                )
            norm_gcd = os.path.normpath(clean_gcd)
            if norm_gcd in seen_gcds:
                raise ValidationError(
                    f"Duplicate git_common_dir '{clean_gcd}' in bindings for project '{project_slug}'."
                )
            seen_gcds.add(norm_gcd)

        # Git remotes validation
        for rem in self.git_remotes:
            if not isinstance(rem, str) or not rem.strip():
                raise ValidationError(f"Project '{project_slug}' git_remote binding must be a non-empty string.")

    def to_dict(self) -> dict[str, list[str]]:
        """Serialize bindings to dictionary."""
        return {
            "paths": list(self.paths),
            "git_common_dirs": list(self.git_common_dirs),
            "git_remotes": list(self.git_remotes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], kind: str, project_slug: str) -> "ProjectBindings":
        """Construct and validate ProjectBindings from raw mapping."""
        if not isinstance(data, dict):
            raise ValidationError(f"Project '{project_slug}' bindings must be a mapping.")
        unknown = set(data.keys()) - ALLOWED_BINDINGS_KEYS
        if unknown:
            raise ValidationError(f"Unknown bindings field(s) in project '{project_slug}': {sorted(unknown)}")
        missing = ALLOWED_BINDINGS_KEYS - set(data.keys())
        if missing:
            raise ValidationError(f"Missing required bindings field(s) in project '{project_slug}': {sorted(missing)}")

        raw_paths = data["paths"]
        if not isinstance(raw_paths, list):
            raise ValidationError(f"Project '{project_slug}' bindings.paths must be a list.")
        norm_paths = []
        for p in raw_paths:
            if not isinstance(p, str) or not p.strip():
                raise ValidationError(f"Project '{project_slug}' path binding must be a non-empty string.")
            if not Path(p.strip()).is_absolute():
                raise ValidationError(
                    f"Path binding '{p}' in project '{project_slug}' must be an absolute path, got relative path."
                )
            norm_paths.append(os.path.normpath(p.strip()))

        raw_gcds = data["git_common_dirs"]
        if not isinstance(raw_gcds, list):
            raise ValidationError(f"Project '{project_slug}' bindings.git_common_dirs must be a list.")
        norm_gcds = []
        for gcd in raw_gcds:
            if not isinstance(gcd, str) or not gcd.strip():
                raise ValidationError(
                    f"Project '{project_slug}' git_common_dir binding must be a non-empty string."
                )
            if not Path(gcd.strip()).is_absolute():
                raise ValidationError(
                    f"git_common_dir binding '{gcd}' in project '{project_slug}' must be an absolute path, got relative path."
                )
            norm_gcds.append(os.path.normpath(gcd.strip()))

        raw_remotes = data["git_remotes"]
        if not isinstance(raw_remotes, list):
            raise ValidationError(f"Project '{project_slug}' bindings.git_remotes must be a list.")
        clean_remotes = []
        for r in raw_remotes:
            if not isinstance(r, str) or not r.strip():
                raise ValidationError(f"Project '{project_slug}' git_remote binding must be a non-empty string.")
            clean_remotes.append(r.strip())

        bindings = cls(
            paths=norm_paths,
            git_common_dirs=norm_gcds,
            git_remotes=clean_remotes,
        )
        bindings.validate(kind, project_slug)
        return bindings


@dataclass
class ProjectMemory:
    """Project memory backend configuration and relative note root."""
    backend: str = DEFAULT_MEMORY_BACKEND
    project_name: str = ""
    path: str = ""

    def validate(self, project_id: str) -> None:
        """Validate memory configuration and safety of memory path."""
        if self.backend != DEFAULT_MEMORY_BACKEND:
            raise ValidationError(
                f"Unsupported memory backend '{self.backend}' for project '{project_id}'. "
                f"Only '{DEFAULT_MEMORY_BACKEND}' is supported in v2."
            )
        if not isinstance(self.project_name, str) or not self.project_name.strip():
            raise ValidationError(f"Project '{project_id}' memory.project_name must be a non-empty string.")

        expected_path = f"projects/{project_id}/memory"
        if not isinstance(self.path, str) or not self.path.strip():
            raise ValidationError(f"Project '{project_id}' memory.path must be a non-empty string.")

        clean_path = self.path.strip()
        path_obj = Path(clean_path)
        if (
            clean_path != expected_path
            or path_obj.is_absolute()
            or ".." in path_obj.parts
            or path_obj.as_posix() != expected_path
        ):
            raise ValidationError(
                f"Invalid memory path '{self.path}' for project '{project_id}'. "
                f"Must be relative and exactly rooted at '{expected_path}' without escaping."
            )

    def to_dict(self) -> dict[str, str]:
        """Serialize memory configuration to dictionary."""
        return {
            "backend": self.backend,
            "project_name": self.project_name,
            "path": self.path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], project_id: str) -> "ProjectMemory":
        """Construct and validate ProjectMemory from raw mapping."""
        if not isinstance(data, dict):
            raise ValidationError(f"Project '{project_id}' memory must be a mapping.")
        unknown = set(data.keys()) - ALLOWED_MEMORY_KEYS
        if unknown:
            raise ValidationError(f"Unknown memory field(s) in project '{project_id}': {sorted(unknown)}")
        missing = ALLOWED_MEMORY_KEYS - set(data.keys())
        if missing:
            raise ValidationError(f"Missing required memory field(s) in project '{project_id}': {sorted(missing)}")

        backend_val = data["backend"]
        if not isinstance(backend_val, str):
            raise ValidationError(f"Project '{project_id}' memory.backend must be a string, got {type(backend_val).__name__}.")

        name_val = data["project_name"]
        if not isinstance(name_val, str):
            raise ValidationError(f"Project '{project_id}' memory.project_name must be a string, got {type(name_val).__name__}.")

        path_val = data["path"]
        if not isinstance(path_val, str):
            raise ValidationError(f"Project '{project_id}' memory.path must be a string, got {type(path_val).__name__}.")

        mem = cls(
            backend=backend_val,
            project_name=name_val,
            path=path_val,
        )
        mem.validate(project_id)
        return mem


@dataclass
class ProjectRecord:
    """Typed canonical project record stored in registry/projects.yaml."""
    id: str
    slug: str
    display_name: str
    kind: str
    aliases: list[str] = field(default_factory=list)
    bindings: ProjectBindings = field(default_factory=ProjectBindings)
    memory: ProjectMemory = field(default_factory=ProjectMemory)
    created_at: str = field(default_factory=now_rfc3339)
    updated_at: str = field(default_factory=now_rfc3339)

    def validate(self) -> None:
        """Validate all fields and internal consistency of this project record."""
        # Validate ID
        validate_uuid4(self.id, "id")

        # Validate slug
        validate_token_format(self.slug, "slug")

        # Validate display name
        if not isinstance(self.display_name, str) or not self.display_name.strip():
            raise ValidationError(f"Project '{self.slug}' display_name must be a non-empty string.")

        # Validate kind
        if self.kind not in SUPPORTED_PROJECT_KINDS:
            raise ValidationError(
                f"Invalid project kind '{self.kind}' for project '{self.slug}'. "
                f"Supported kinds: {SUPPORTED_PROJECT_KINDS}"
            )

        # Validate aliases
        if not isinstance(self.aliases, list):
            raise ValidationError(f"Project '{self.slug}' aliases must be a list.")
        seen_aliases: set[str] = set()
        for a in self.aliases:
            clean_a = validate_token_format(a, f"alias in project '{self.slug}'")
            lower_a = clean_a.lower()
            if lower_a in seen_aliases:
                raise ValidationError(f"Duplicate alias '{clean_a}' within project '{self.slug}'.")
            seen_aliases.add(lower_a)

        # Internal token collision check
        if self.slug.lower() == self.id.lower():
            raise ValidationError(f"Project slug '{self.slug}' cannot be identical to project ID.")
        if self.slug.lower() in seen_aliases:
            raise ValidationError(f"Project slug '{self.slug}' cannot also be declared as an alias.")
        if self.id.lower() in seen_aliases:
            raise ValidationError(f"Project ID '{self.id}' cannot also be declared as an alias.")

        # Validate bindings and memory
        self.bindings.validate(self.kind, self.slug)
        self.memory.validate(self.id)

        # Validate timestamps
        validate_rfc3339_utc(self.created_at, "created_at")
        validate_rfc3339_utc(self.updated_at, "updated_at")

    def to_dict(self) -> dict[str, Any]:
        """Serialize project record to dictionary in deterministic key order."""
        return {
            "id": self.id,
            "slug": self.slug,
            "display_name": self.display_name,
            "kind": self.kind,
            "aliases": list(self.aliases),
            "bindings": self.bindings.to_dict(),
            "memory": self.memory.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectRecord":
        """Construct and strictly validate ProjectRecord from dictionary."""
        if not isinstance(data, dict):
            raise ValidationError("Project record must be a mapping.")

        unknown = set(data.keys()) - ALLOWED_RECORD_KEYS
        if unknown:
            raise ValidationError(f"Unknown field(s) in project record: {sorted(unknown)}")

        missing = ALLOWED_RECORD_KEYS - set(data.keys())
        if missing:
            raise ValidationError(f"Missing required field(s) in project record: {sorted(missing)}")

        pid = validate_uuid4(data["id"], "id")
        pslug = validate_token_format(data["slug"], "slug")
        raw_kind = data["kind"]
        if not isinstance(raw_kind, str):
            raise ValidationError(f"Field 'kind' in project '{pslug}' must be a string, got {type(raw_kind).__name__}.")
        pkind = raw_kind.strip()
        if pkind not in SUPPORTED_PROJECT_KINDS:
            raise ValidationError(
                f"Invalid project kind '{pkind}' for project '{pslug}'. Supported kinds: {SUPPORTED_PROJECT_KINDS}"
            )

        raw_display_name = data["display_name"]
        if not isinstance(raw_display_name, str):
            raise ValidationError(
                f"Field 'display_name' in project '{pslug}' must be a string, got {type(raw_display_name).__name__}."
            )

        raw_created_at = data["created_at"]
        if not isinstance(raw_created_at, str):
            raise ValidationError(
                f"Field 'created_at' in project '{pslug}' must be a string, got {type(raw_created_at).__name__}."
            )

        raw_updated_at = data["updated_at"]
        if not isinstance(raw_updated_at, str):
            raise ValidationError(
                f"Field 'updated_at' in project '{pslug}' must be a string, got {type(raw_updated_at).__name__}."
            )

        bindings = ProjectBindings.from_dict(data["bindings"], kind=pkind, project_slug=pslug)
        memory = ProjectMemory.from_dict(data["memory"], project_id=pid)

        raw_aliases = data["aliases"]
        if not isinstance(raw_aliases, list):
            raise ValidationError(f"Project '{pslug}' aliases must be a list.")
        aliases: list[str] = []
        for a in raw_aliases:
            aliases.append(validate_token_format(a, f"alias in project '{pslug}'"))

        record = cls(
            id=pid,
            slug=pslug,
            display_name=raw_display_name,
            kind=pkind,
            aliases=aliases,
            bindings=bindings,
            memory=memory,
            created_at=raw_created_at,
            updated_at=raw_updated_at,
        )
        record.validate()
        return record

    @classmethod
    def create(
        cls,
        *,
        slug: str,
        display_name: str,
        kind: str = PROJECT_KIND_GIT,
        project_id: str | None = None,
        aliases: list[str] | None = None,
        paths: list[str] | None = None,
        git_common_dirs: list[str] | None = None,
        git_remotes: list[str] | None = None,
        memory_project_name: str | None = None,
        created_at: str | None = None,
        updated_at: str | None = None,
    ) -> "ProjectRecord":
        """Factory method to construct a valid ProjectRecord with canonical defaults."""
        actual_id = project_id if project_id is not None else str(uuid.uuid4())
        actual_id = validate_uuid4(actual_id, "project_id")
        actual_slug = validate_token_format(slug, "slug")

        mem_name = memory_project_name or f"ptw-{actual_slug}-{actual_id[:8]}"
        mem = ProjectMemory(
            backend=DEFAULT_MEMORY_BACKEND,
            project_name=mem_name,
            path=f"projects/{actual_id}/memory",
        )

        bindings = ProjectBindings.from_dict(
            {
                "paths": [] if paths is None else paths,
                "git_common_dirs": [] if git_common_dirs is None else git_common_dirs,
                "git_remotes": [] if git_remotes is None else git_remotes,
            },
            kind=kind,
            project_slug=actual_slug,
        )

        now = now_rfc3339()
        c_at = created_at or now
        u_at = updated_at or c_at

        if aliases is not None and not isinstance(aliases, list):
            raise ValidationError(f"Project '{actual_slug}' aliases must be a list.")
        clean_aliases = [validate_token_format(a, "alias") for a in (aliases or [])]

        record = cls(
            id=actual_id,
            slug=actual_slug,
            display_name=display_name,
            kind=kind,
            aliases=clean_aliases,
            bindings=bindings,
            memory=mem,
            created_at=c_at,
            updated_at=u_at,
        )
        record.validate()
        return record

    def with_bindings(
        self,
        *,
        paths: list[str] | None = None,
        git_common_dirs: list[str] | None = None,
        git_remotes: list[str] | None = None,
        updated_at: str | None = None,
    ) -> "ProjectRecord":
        """Return a valid copy of this project record with modified bindings and updated timestamp."""
        new_paths = (
            list(self.bindings.paths)
            if paths is None
            else [os.path.normpath(p) for p in paths]
        )
        new_gcds = (
            list(self.bindings.git_common_dirs)
            if git_common_dirs is None
            else [os.path.normpath(g) for g in git_common_dirs]
        )
        new_remotes = (
            list(self.bindings.git_remotes)
            if git_remotes is None
            else list(git_remotes)
        )
        new_bindings = ProjectBindings(
            paths=new_paths,
            git_common_dirs=new_gcds,
            git_remotes=new_remotes,
        )
        rec = ProjectRecord(
            id=self.id,
            slug=self.slug,
            display_name=self.display_name,
            kind=self.kind,
            aliases=list(self.aliases),
            bindings=new_bindings,
            memory=self.memory,
            created_at=self.created_at,
            updated_at=updated_at or self.updated_at,
        )
        rec.validate()
        return rec



@dataclass
class ProjectRegistry:
    """Canonical registry storing full project records in version 2 format.

    Ordering policy:
    Maintains deterministic stable insertion order. Projects are persisted,
    iterated, and dumped in the exact order they were inserted. Updates preserve
    index positions.
    """
    version: int = SCHEMA_VERSION
    projects: list[ProjectRecord] = field(default_factory=list)

    def validate(self) -> None:
        """Validate entire registry schema and cross-project uniqueness invariants."""
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise ConfigError(f"Registry version must be an integer, got {type(self.version).__name__}.")
        if self.version > SCHEMA_VERSION:
            raise ConfigError(
                f"Unsupported schema version {self.version} in registry: newer than supported version {SCHEMA_VERSION}."
            )
        if self.version < 2:
            raise ConfigError(
                f"Unsupported schema version {self.version} in registry: version must be >= 2."
            )

        if not isinstance(self.projects, list):
            raise ValidationError("Field 'projects' must be a list in registry.")

        seen_tokens: dict[str, str] = {}
        seen_paths: dict[str, str] = {}
        seen_gcds: dict[str, str] = {}

        for project in self.projects:
            if not isinstance(project, ProjectRecord):
                raise ValidationError(f"Invalid item in projects list: expected ProjectRecord, got {type(project).__name__}.")
            project.validate()

            # 1. ID uniqueness (case-insensitive)
            id_lower = project.id.lower()
            if id_lower in seen_tokens:
                raise ValidationError(
                    f"Registry collision: token '{id_lower}' (id of project '{project.slug}') "
                    f"conflicts with {seen_tokens[id_lower]}."
                )
            seen_tokens[id_lower] = f"id of project '{project.slug}'"

            # 2. Slug uniqueness (case-insensitive)
            slug_lower = project.slug.lower()
            if slug_lower in seen_tokens:
                raise ValidationError(
                    f"Registry collision: token '{slug_lower}' (slug of project '{project.slug}') "
                    f"conflicts with {seen_tokens[slug_lower]}."
                )
            seen_tokens[slug_lower] = f"slug of project '{project.slug}'"

            # 3. Aliases uniqueness (case-insensitive)
            for alias in project.aliases:
                alias_lower = alias.lower()
                if alias_lower in seen_tokens:
                    raise ValidationError(
                        f"Registry collision: token '{alias_lower}' (alias in project '{project.slug}') "
                        f"conflicts with {seen_tokens[alias_lower]}."
                    )
                seen_tokens[alias_lower] = f"alias in project '{project.slug}'"

            # 4. Bindings paths uniqueness (exact normalized paths)
            for p in project.bindings.paths:
                norm_p = os.path.normpath(p)
                if norm_p in seen_paths:
                    raise ValidationError(
                        f"Duplicate path binding '{p}' claimed by both project '{seen_paths[norm_p]}' "
                        f"and project '{project.slug}'."
                    )
                seen_paths[norm_p] = project.slug

            # 5. Git common dirs uniqueness (exact normalized paths)
            for gcd in project.bindings.git_common_dirs:
                norm_gcd = os.path.normpath(gcd)
                if norm_gcd in seen_gcds:
                    raise ValidationError(
                        f"Duplicate git_common_dir binding '{gcd}' claimed by both project '{seen_gcds[norm_gcd]}' "
                        f"and project '{project.slug}'."
                    )
                seen_gcds[norm_gcd] = project.slug

            # Note: duplicate remotes across projects are explicitly allowed by design.

    def get(self, token: str) -> ProjectRecord | None:
        """Lookup project by ID, slug, or unique alias case-insensitively."""
        if not token or not isinstance(token, str):
            return None
        target = token.strip().lower()
        for p in self.projects:
            if p.id.lower() == target or p.slug.lower() == target:
                return p
            if any(a.lower() == target for a in p.aliases):
                return p
        return None

    def add_project(self, project: ProjectRecord) -> None:
        """Add a project to registry in stable insertion order after cross-validation."""
        candidate = ProjectRegistry(
            version=self.version,
            projects=list(self.projects) + [project],
        )
        candidate.validate()
        self.projects.append(project)

    def update_project(self, project: ProjectRecord) -> None:
        """Update an existing project in-place preserving its position after validation."""
        target_id = project.id.lower()
        idx = None
        for i, p in enumerate(self.projects):
            if p.id.lower() == target_id:
                idx = i
                break
        if idx is None:
            raise ValidationError(f"Cannot update: project with ID '{project.id}' not found in registry.")

        candidate_list = list(self.projects)
        candidate_list[idx] = project
        candidate = ProjectRegistry(version=self.version, projects=candidate_list)
        candidate.validate()
        self.projects[idx] = project

    def update_project_bindings(
        self,
        project_id: str,
        *,
        paths: list[str] | None = None,
        git_common_dirs: list[str] | None = None,
        git_remotes: list[str] | None = None,
        updated_at: str | None = None,
    ) -> ProjectRecord:
        """Update bindings of an existing project in-place and return the updated record."""
        project = self.get(project_id)
        if project is None:
            raise ValidationError(f"Cannot update bindings: project '{project_id}' not found in registry.")
        updated = project.with_bindings(
            paths=paths,
            git_common_dirs=git_common_dirs,
            git_remotes=git_remotes,
            updated_at=updated_at,
        )
        self.update_project(updated)
        return updated


    def remove_project(self, token: str) -> ProjectRecord:
        """Remove a project from the registry by ID, slug, or alias.

        Modifies only in-memory registry state; does not delete memory or workspace files.
        """
        project = self.get(token)
        if project is None:
            raise ValidationError(f"Cannot remove: project '{token}' not found in registry.")
        self.projects.remove(project)
        return project

    def to_dict(self) -> dict[str, Any]:
        """Convert registry to dictionary in deterministic format."""
        return {
            "version": self.version,
            "projects": [p.to_dict() for p in self.projects],
        }

    def to_yaml(self) -> str:
        """Dump registry to deterministic YAML with stable order."""
        self.validate()
        return yaml.safe_dump(
            self.to_dict(),
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
            width=1_000_000,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectRegistry":
        """Construct and validate ProjectRegistry from raw dictionary."""
        if not isinstance(data, dict):
            raise ConfigError(f"Registry root must be a mapping, got {type(data).__name__}.")

        unknown = set(data.keys()) - ALLOWED_REGISTRY_KEYS
        if unknown:
            raise ConfigError(f"Unknown root field(s) in registry: {sorted(unknown)}")

        if "version" not in data:
            raise ConfigError("Missing required 'version' field in registry.")
        v = data["version"]
        if isinstance(v, bool) or not isinstance(v, int):
            raise ConfigError(f"Registry version must be an integer, got {type(v).__name__}.")
        if v > SCHEMA_VERSION:
            raise ConfigError(
                f"Unsupported schema version {v} in registry: newer than supported version {SCHEMA_VERSION}."
            )
        if v < 2:
            raise ConfigError(
                f"Unsupported schema version {v} in registry: expected version >= 2."
            )

        if "projects" not in data:
            raise ConfigError("Missing required 'projects' field in registry.")
        raw_projects = data["projects"]
        if not isinstance(raw_projects, list):
            raise ValidationError(f"'projects' in registry must be a list, got {type(raw_projects).__name__}.")

        projects: list[ProjectRecord] = []
        for p in raw_projects:
            if not isinstance(p, dict):
                raise ValidationError(f"Project record in registry must be a mapping, got {type(p).__name__}.")
            projects.append(ProjectRecord.from_dict(p))

        reg = cls(version=v, projects=projects)
        reg.validate()
        return reg

    @classmethod
    def empty(cls) -> "ProjectRegistry":
        """Return a fresh empty v2 registry."""
        return cls(version=SCHEMA_VERSION, projects=[])

    def __len__(self) -> int:
        return len(self.projects)

    def __iter__(self):
        return iter(self.projects)

    def __getitem__(self, token: str) -> ProjectRecord:
        res = self.get(token)
        if res is None:
            raise KeyError(f"Project '{token}' not found in registry.")
        return res


def load_registry(path: Path | str) -> ProjectRegistry:
    """Read-only load of project registry from the specified path.

    Returns an empty v2 registry if the file does not exist.
    Performs no disk writes.
    """
    reg_path = Path(path).expanduser()
    if not reg_path.exists():
        return ProjectRegistry.empty()

    if not reg_path.is_file():
        raise ConfigError(f"Registry path '{reg_path}' is not a regular file.")

    try:
        raw_text = reg_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ConfigError(f"Failed to read registry file at '{reg_path}'.")

    try:
        loaded = yaml.safe_load(raw_text)
    except yaml.YAMLError:
        raise ConfigError(f"Malformed YAML in registry file at '{reg_path}'.")

    if not isinstance(loaded, dict):
        raise ConfigError(f"Registry root must be a mapping, got {type(loaded).__name__}.")

    return ProjectRegistry.from_dict(loaded)


def save_registry(
    registry: ProjectRegistry,
    cfg: PersonalTidewayConfig,
    target_path: Path | None = None,
    dry_run: bool = False,
) -> Path:
    """Atomically save project registry to canonical projects.yaml with strict boundary checks.

    Rejects:
    - targets escaping cfg.home (PERSONAL_TIDEWAY_HOME)
    - targets that are symlinks
    - existing files with an unknown/newer schema version

    Does not modify any filesystem paths when dry_run=True.
    """
    target = Path(target_path).expanduser() if target_path is not None else cfg.projects_yaml

    # 1. Boundary check: only the configured canonical registry target is writable.
    canonical_target = validate_owned_path(cfg.projects_yaml, root=cfg.home, allow_root=False)
    resolved_target = validate_owned_path(target, root=cfg.home, allow_root=False)
    if resolved_target != canonical_target:
        raise ConfigError(
            f"Registry target '{target}' is not the configured canonical registry '{cfg.projects_yaml}'."
        )

    # 2. Reject symlink target
    if target.is_symlink():
        raise ConfigError(f"Registry target '{target}' is a symlink, which is not permitted.")

    if target.exists() and not target.is_file():
        raise ConfigError(f"Registry target '{target}' exists but is not a regular file.")

    # 3. Full validation of registry before attempting write
    registry.validate()

    # 4. Refuse to overwrite any unreadable, malformed, or unsupported registry.
    if target.is_file():
        try:
            load_registry(target)
        except ConfigError as exc:
            if "newer than supported version" in str(exc):
                raise ConfigError(
                    f"Cannot overwrite {target}: file has a newer schema version (supported: {SCHEMA_VERSION})."
                )
            raise ConfigError(f"Cannot overwrite invalid registry at '{target}'.")
        except ValidationError:
            raise ConfigError(f"Cannot overwrite invalid registry at '{target}'.")

    # 5. Handle dry-run: return target without writing
    if dry_run:
        return target

    # 6. Atomic write
    content = registry.to_yaml()
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, content)
    return target
