"""Safe Basic Memory 0.23.2 read-only context retrieval adapter.

Work Package Context Loop 1 implementation for Personal Tideway v2:
- Immutable typed request, plan, preview, item, bundle, and result models.
- Safe to_dict serialization without leaking host paths, raw stderr, raw queries, or backend metadata.
- Pure planner producing exact argv tuples for Basic Memory 0.23.2 CLI.
- Current-state lookup via project-scoped --permalink */current-state wildcard search.
- Case-insensitive permalink deduplication with current-state ordered first.
- Strict bounded budgeting for max_items and max_chars with Unicode safety.
- Strict validation of backend output, schema, scalar types, frontmatter, and status values.
- Fail-closed execution requiring installed isolated executable and canonical layout.
- Secret-safe error translations with suppressed cause chaining.
"""

import json
import math
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.core.basic_memory_backend import (
    compute_canonical_basic_memory_env_overrides,
)
from personal_tideway.core.basic_memory_installer import (
    BasicMemoryRunner,
    BasicMemoryRunnerResult,
    build_subprocess_env,
    validate_isolated_executable,
)
from personal_tideway.core.basic_memory_runtime import (
    BasicMemoryLayout,
    get_basic_memory_layout,
)
from personal_tideway.core.registry import (
    ProjectRecord,
    ProjectRegistry,
    load_registry,
)
from personal_tideway.exceptions import (
    BoundaryError,
    RuntimeProbeError,
    ValidationError,
)

# Canonical query and permalink for project current-state
CURRENT_STATE_PERMALINK: str = "current-state"
CURRENT_STATE_PERMALINK_PATTERN: str = "*/current-state"

# Default and bounded limits
DEFAULT_MAX_ITEMS: int = 5
HARD_MAX_ITEMS: int = 50

DEFAULT_MAX_CHARS: int = 8000
HARD_MAX_CHARS: int = 100_000

DEFAULT_RETRIEVAL_TIMEOUT: float = 15.0
MAX_RETRIEVAL_OUTPUT_BYTES: int = 2 * 1024 * 1024  # 2 MiB bounded output limit

# Truncation indicator
TRUNCATION_MARKER: str = "\n... [truncated]"

# Allowed validated status/confidence values
VALID_STATUSES: frozenset[str] = frozenset({"verified", "partial", "planned", "blocked", "stale"})


def is_current_state_permalink(permalink: str) -> bool:
    """Check if permalink represents the canonical project current-state."""
    clean = permalink.strip().lower()
    return clean == CURRENT_STATE_PERMALINK.lower() or clean.endswith(
        f"/{CURRENT_STATE_PERMALINK.lower()}"
    )


@dataclass(frozen=True)
class ContextRetrievalRequest:
    """Immutable request specification for project-scoped read-only context retrieval."""

    project: str | ProjectRecord
    query: str | None = None
    max_items: int = DEFAULT_MAX_ITEMS
    max_chars: int = DEFAULT_MAX_CHARS
    include_current_state: bool = True
    timeout: float = DEFAULT_RETRIEVAL_TIMEOUT

    def __post_init__(self) -> None:
        if isinstance(self.project, str):
            if not self.project.strip():
                raise ValidationError("Project identifier cannot be empty.")
        elif isinstance(self.project, ProjectRecord):
            self.project.validate()
        else:
            raise ValidationError("Project must be a string identifier or ProjectRecord.")

        if self.query is not None:
            if not isinstance(self.query, str):
                raise ValidationError("Query must be a string or None.")
            if self.query.strip().startswith("-"):
                raise ValidationError("Query cannot begin with a dash.")

        if (
            isinstance(self.max_items, bool)
            or not isinstance(self.max_items, int)
            or self.max_items < 1
            or self.max_items > HARD_MAX_ITEMS
        ):
            raise ValidationError(
                f"max_items must be an integer between 1 and {HARD_MAX_ITEMS}."
            )

        if (
            isinstance(self.max_chars, bool)
            or not isinstance(self.max_chars, int)
            or self.max_chars < 1
            or self.max_chars > HARD_MAX_CHARS
        ):
            raise ValidationError(
                f"max_chars must be an integer between 1 and {HARD_MAX_CHARS}."
            )

        if not isinstance(self.include_current_state, bool):
            raise ValidationError("include_current_state must be a boolean.")

        if (
            isinstance(self.timeout, bool)
            or not isinstance(self.timeout, (int, float))
            or math.isnan(self.timeout)
            or math.isinf(self.timeout)
            or self.timeout <= 0
        ):
            raise ValidationError("timeout must be a positive finite number.")

    def to_dict(self) -> dict[str, Any]:
        """Convert request to safe dictionary representation without leaking raw query."""
        project_val = (
            self.project.id
            if isinstance(self.project, ProjectRecord)
            else self.project
        )
        return {
            "project": project_val,
            "has_query": bool(self.query and self.query.strip()),
            "max_items": self.max_items,
            "max_chars": self.max_chars,
            "include_current_state": self.include_current_state,
            "timeout": self.timeout,
        }


@dataclass(frozen=True)
class ContextItem:
    """Safe, immutable representation of a single retrieved context note."""

    project_id: str
    project_name: str
    title: str
    permalink: str
    content: str
    is_current_state: bool = False
    note_type: str | None = None
    status: str | None = None
    updated_at: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, str) or not self.project_id.strip():
            raise ValidationError("project_id must be a non-empty string.")
        if not isinstance(self.project_name, str) or not self.project_name.strip():
            raise ValidationError("project_name must be a non-empty string.")
        if not isinstance(self.title, str):
            raise ValidationError("title must be a string.")
        if not isinstance(self.permalink, str) or not self.permalink.strip():
            raise ValidationError("permalink must be a non-empty string.")
        if not isinstance(self.content, str):
            raise ValidationError("content must be a string.")
        if not isinstance(self.is_current_state, bool):
            raise ValidationError("is_current_state must be a boolean.")
        if self.note_type is not None and not isinstance(self.note_type, str):
            raise ValidationError("note_type must be a string or None.")
        if self.status is not None and (
            not isinstance(self.status, str) or self.status not in VALID_STATUSES
        ):
            raise ValidationError(
                f"status must be one of {sorted(VALID_STATUSES)} or None."
            )
        if self.updated_at is not None and not isinstance(self.updated_at, str):
            raise ValidationError("updated_at must be a string or None.")

    def to_dict(self) -> dict[str, Any]:
        """Convert item to safe dictionary without leaking file paths or backend metadata."""
        res: dict[str, Any] = {
            "project_id": self.project_id,
            "project_name": self.project_name,
            "title": self.title,
            "permalink": self.permalink,
            "content": self.content,
            "is_current_state": self.is_current_state,
        }
        if self.note_type is not None:
            res["note_type"] = self.note_type
        if self.status is not None:
            res["status"] = self.status
        if self.updated_at is not None:
            res["updated_at"] = self.updated_at
        return res


@dataclass(frozen=True)
class ContextBundle:
    """Deterministic, immutable bundle of retrieved context notes for a project."""

    project_id: str
    project_name: str
    items: tuple[ContextItem, ...] = field(default_factory=tuple)
    rendered_text: str = ""
    total_items: int = 0
    total_chars: int = 0
    truncated: bool = False
    has_current_state: bool = False

    def __post_init__(self) -> None:
        items_tuple = tuple(self.items)
        object.__setattr__(self, "items", items_tuple)
        object.__setattr__(self, "total_items", len(items_tuple))
        has_cs = any(it.is_current_state for it in items_tuple)
        object.__setattr__(self, "has_current_state", has_cs)

        rendered = self._compute_rendered_text(items_tuple)
        object.__setattr__(self, "rendered_text", rendered)
        object.__setattr__(self, "total_chars", len(rendered))

    @staticmethod
    def _compute_rendered_text(items: tuple[ContextItem, ...]) -> str:
        if not items:
            return ""
        blocks: list[str] = [_render_single_item(it) for it in items]
        return "\n\n".join(blocks).strip()

    def to_dict(self) -> dict[str, Any]:
        """Convert bundle to safe dictionary representation."""
        return {
            "project_id": self.project_id,
            "project_name": self.project_name,
            "items": [it.to_dict() for it in self.items],
            "rendered_text": self.rendered_text,
            "total_items": self.total_items,
            "total_chars": self.total_chars,
            "truncated": self.truncated,
            "has_current_state": self.has_current_state,
        }


@dataclass(frozen=True)
class ContextRetrievalPlanPreview:
    """Safe, immutable preview of planned context retrieval commands and limits."""

    project_id: str
    project_name: str
    operation: str
    limits: Mapping[str, Any]
    commands: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "commands", tuple(self.commands))
        object.__setattr__(self, "limits", MappingProxyType(dict(self.limits)))

    def to_dict(self) -> dict[str, Any]:
        """Convert preview to safe dictionary without exposing host environment, paths, or query."""
        return {
            "operation": self.operation,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "limits": dict(self.limits),
            "commands": list(self.commands),
        }


@dataclass(frozen=True)
class ContextRetrievalPlan:
    """Immutable plan for isolated Basic Memory context retrieval."""

    request: ContextRetrievalRequest
    layout: BasicMemoryLayout
    executable: Path
    project_record: ProjectRecord
    current_state_argv: tuple[str, ...] | None
    query_argv: tuple[str, ...] | None
    search_argvs: tuple[tuple[str, ...], ...]
    env_overrides: Mapping[str, str]
    timeout: float
    is_noop: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "search_argvs", tuple(tuple(a) for a in self.search_argvs)
        )
        object.__setattr__(
            self, "env_overrides", MappingProxyType(dict(self.env_overrides))
        )

    def preview(self) -> ContextRetrievalPlanPreview:
        """Return safe preview of planned context retrieval execution with redacted command intent."""
        redacted_cmds: list[str] = []
        if self.current_state_argv is not None:
            redacted_cmds.append(
                f"basic-memory tool search-notes --permalink {CURRENT_STATE_PERMALINK_PATTERN} "
                f"--page-size 1 --json --project {self.project_record.memory.project_name} --local"
            )
        if self.query_argv is not None:
            redacted_cmds.append(
                f"basic-memory tool search-notes <query_redacted> "
                f"--page-size {self.request.max_items} --json --project {self.project_record.memory.project_name} --local"
            )
        return ContextRetrievalPlanPreview(
            project_id=self.project_record.id,
            project_name=self.project_record.memory.project_name,
            operation="context_retrieval",
            limits={
                "max_items": self.request.max_items,
                "max_chars": self.request.max_chars,
                "include_current_state": self.request.include_current_state,
                "timeout": self.timeout,
            },
            commands=tuple(redacted_cmds),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert plan to safe dictionary representation without leaking host paths, env, or query."""
        d = self.preview().to_dict()
        d["is_noop"] = self.is_noop
        return d


@dataclass(frozen=True)
class ContextRetrievalResult:
    """Immutable result of context retrieval execution."""

    bundle: ContextBundle
    dry_run: bool
    preview: ContextRetrievalPlanPreview | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert result to safe dictionary representation."""
        return {
            "bundle": self.bundle.to_dict(),
            "dry_run": self.dry_run,
            "preview": self.preview.to_dict() if self.preview is not None else None,
        }


# ---------------------------------------------------------------------------
# Rendering and Budgeting Helpers
# ---------------------------------------------------------------------------


def _render_single_item(item: ContextItem) -> str:
    """Render a single ContextItem deterministically into markdown."""
    prefix = "Current State" if item.is_current_state else "Note"
    header = f"### {prefix}: {item.title} ({item.permalink})"
    meta_parts: list[str] = []
    if item.note_type:
        meta_parts.append(f"Type: {item.note_type}")
    if item.status:
        meta_parts.append(f"Status: {item.status}")
    if item.updated_at:
        meta_parts.append(f"Updated: {item.updated_at}")
    meta_line = " | ".join(meta_parts)
    if meta_line:
        if item.content:
            return f"{header}\n{meta_line}\n\n{item.content}".strip()
        return f"{header}\n{meta_line}".strip()
    if item.content:
        return f"{header}\n\n{item.content}".strip()
    return header.strip()


def _truncate_item_to_fit(item: ContextItem, max_chars: int) -> ContextItem | None:
    """Deterministically truncate ContextItem content so its rendered representation fits max_chars.

    Returns None if even a minimal safe item (header + metadata) cannot fit into max_chars.
    """
    prefix = "Current State" if item.is_current_state else "Note"
    header = f"### {prefix}: {item.title} ({item.permalink})"
    meta_parts: list[str] = []
    if item.note_type:
        meta_parts.append(f"Type: {item.note_type}")
    if item.status:
        meta_parts.append(f"Status: {item.status}")
    if item.updated_at:
        meta_parts.append(f"Updated: {item.updated_at}")
    meta_line = " | ".join(meta_parts)

    prefix_block = f"{header}\n{meta_line}\n\n" if meta_line else f"{header}\n\n"
    overhead = len(prefix_block)
    marker = TRUNCATION_MARKER

    if overhead > max_chars:
        # Check if minimal header fits exactly without trailing newlines and empty content
        minimal_header = f"{header}\n{meta_line}".strip() if meta_line else header.strip()
        if len(minimal_header) <= max_chars:
            return ContextItem(
                project_id=item.project_id,
                project_name=item.project_name,
                title=item.title,
                permalink=item.permalink,
                content="",
                is_current_state=item.is_current_state,
                note_type=item.note_type,
                status=item.status,
                updated_at=item.updated_at,
            )
        return None

    available_content = max_chars - overhead - len(marker)
    if available_content >= 0:
        truncated_content = item.content[:available_content] + marker
    else:
        truncated_content = ""

    candidate = ContextItem(
        project_id=item.project_id,
        project_name=item.project_name,
        title=item.title,
        permalink=item.permalink,
        content=truncated_content,
        is_current_state=item.is_current_state,
        note_type=item.note_type,
        status=item.status,
        updated_at=item.updated_at,
    )
    if len(_render_single_item(candidate)) <= max_chars:
        return candidate
    return None


def build_bounded_context_bundle(
    project_id: str,
    project_name: str,
    candidate_items: Sequence[ContextItem],
    max_items: int,
    max_chars: int,
) -> ContextBundle:
    """Deterministically budget ContextItems into a ContextBundle.

    Enforces max_items and max_chars strictly for every accepted max_chars value (including 1).
    Prefers dropping later items or applying clearly marked deterministic truncation.
    Omits an item if even a minimal safe item cannot fit into the character budget.
    Guarantees rendered_text length <= max_chars unconditionally.
    """
    truncated_by_items = False
    items_to_budget = list(candidate_items)
    if len(items_to_budget) > max_items:
        items_to_budget = items_to_budget[:max_items]
        truncated_by_items = True

    if not items_to_budget or max_chars <= 0:
        return ContextBundle(
            project_id=project_id,
            project_name=project_name,
            items=(),
            total_items=0,
            total_chars=0,
            truncated=truncated_by_items,
            has_current_state=False,
        )

    final_items: list[ContextItem] = []
    current_rendered_len = 0
    truncated = truncated_by_items

    for item in items_to_budget:
        full_rendered = _render_single_item(item)
        sep_len = 2 if final_items else 0
        needed = current_rendered_len + sep_len + len(full_rendered)

        if needed <= max_chars:
            final_items.append(item)
            current_rendered_len += sep_len + len(full_rendered)
        else:
            truncated = True
            if not final_items:
                # First item cannot fit in full; check if truncated item fits
                trunc_item = _truncate_item_to_fit(item, max_chars)
                if trunc_item is not None:
                    final_items.append(trunc_item)
                # If even minimal safe item cannot fit, trunc_item is None and we omit it
                break
            else:
                # Prefer dropping later items to preserve clean structure
                break

    return ContextBundle(
        project_id=project_id,
        project_name=project_name,
        items=tuple(final_items),
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# Project Record Resolution
# ---------------------------------------------------------------------------


def resolve_registered_project(
    project: str | ProjectRecord,
    registry: ProjectRegistry,
) -> ProjectRecord:
    """Resolve and strictly validate a single registered ProjectRecord.

    Accepts an ID, slug, or alias string, or an already resolved ProjectRecord.
    Fails closed on missing or ambiguous projects.
    A supplied ProjectRecord must match a registered project by ID and agree on slug and memory.
    Supports Git, directory, and external project kinds.
    """
    if isinstance(project, ProjectRecord):
        project.validate()
        canonical = registry.get(project.id)
        if canonical is None:
            raise ValidationError("Project record not found in registry.")
        if (
            canonical.slug != project.slug
            or canonical.memory.project_name != project.memory.project_name
            or canonical.kind != project.kind
        ):
            raise ValidationError("Project record does not match registered project.")
        return canonical

    if not isinstance(project, str):
        raise ValidationError("Project must be a string identifier or ProjectRecord.")

    clean_token = project.strip()
    if not clean_token:
        raise ValidationError("Project identifier cannot be empty.")

    target = clean_token.lower()
    matches: list[ProjectRecord] = []
    for p in registry.projects:
        if p.id.lower() == target or p.slug.lower() == target or any(a.lower() == target for a in p.aliases):
            matches.append(p)

    if not matches:
        raise ValidationError("Project not found in registry.")
    if len(matches) > 1:
        raise ValidationError("Ambiguous project reference matches multiple projects.")

    matched = matches[0]
    matched.validate()
    if (
        not matched.memory
        or not isinstance(matched.memory.project_name, str)
        or not matched.memory.project_name.strip()
    ):
        raise ValidationError("Project memory configuration is invalid.")

    return matched


# ---------------------------------------------------------------------------
# Argv Builders for Basic Memory 0.23.2 CLI
# ---------------------------------------------------------------------------


def build_search_notes_argv(
    executable: Path | str,
    project_name: str,
    query: str | None = None,
    *,
    permalink: str | None = None,
    page_size: int = DEFAULT_MAX_ITEMS,
) -> tuple[str, ...]:
    """Construct exact argv for basic-memory tool search-notes."""
    clean_exec = str(executable)
    clean_proj = project_name.strip()
    clean_query = query.strip() if query is not None else None
    clean_permalink = permalink.strip() if permalink is not None else None

    if clean_query and clean_permalink:
        raise ValidationError("Cannot specify both query and permalink for search-notes.")

    if clean_query and clean_query.startswith("-"):
        raise ValidationError("Query cannot begin with a dash.")

    if clean_permalink and clean_permalink.startswith("-"):
        raise ValidationError("Permalink cannot begin with a dash.")

    cmd: list[str] = [clean_exec, "tool", "search-notes"]
    if clean_permalink:
        cmd.extend(["--permalink", clean_permalink])
    elif clean_query:
        cmd.append(clean_query)
    cmd.extend([
        "--page-size",
        str(page_size),
        "--json",
        "--project",
        clean_proj,
        "--local",
    ])
    return tuple(cmd)


def build_current_state_search_argv(
    executable: Path | str,
    project_name: str,
    page_size: int = 1,
) -> tuple[str, ...]:
    """Construct exact argv for basic-memory tool search-notes targeting current-state."""
    return (
        str(executable),
        "tool",
        "search-notes",
        "--permalink",
        CURRENT_STATE_PERMALINK_PATTERN,
        "--page-size",
        str(page_size),
        "--json",
        "--project",
        project_name.strip(),
        "--local",
    )


def build_read_note_argv(
    executable: Path | str,
    project_name: str,
    identifier: str,
) -> tuple[str, ...]:
    """Construct exact argv for basic-memory tool read-note IDENTIFIER."""
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValidationError("Note identifier cannot be empty.")
    clean_id = identifier.strip()
    if clean_id.startswith("-"):
        raise ValidationError("Note identifier cannot begin with a dash.")
    if clean_id.startswith(("/", "\\")) or Path(clean_id).is_absolute():
        raise ValidationError("Note identifier cannot be an absolute path.")
    norm_parts = clean_id.replace("\\", "/").split("/")
    if any(p == ".." for p in norm_parts):
        raise ValidationError("Note identifier cannot contain '..' path components.")

    clean_exec = str(executable)
    clean_proj = project_name.strip()
    return (
        clean_exec,
        "tool",
        "read-note",
        clean_id,
        "--json",
        "--project",
        clean_proj,
        "--local",
    )


# ---------------------------------------------------------------------------
# Backend Response Parsing & Schema Validation
# ---------------------------------------------------------------------------


def _extract_status(raw_item: dict[str, Any]) -> str | None:
    """Extract and strictly validate status/confidence without upgrading unknown values."""
    raw_status = raw_item.get("status") or raw_item.get("confidence")
    if raw_status is None and isinstance(raw_item.get("metadata"), dict):
        raw_status = (
            raw_item["metadata"].get("status")
            or raw_item["metadata"].get("confidence")
        )
    if isinstance(raw_status, str):
        clean_status = raw_status.strip().lower()
        if clean_status in VALID_STATUSES:
            return clean_status
    return None


def parse_search_notes_response(
    stdout: str,
    project_id: str,
    project_name: str,
    *,
    is_current_state: bool = False,
) -> list[ContextItem]:
    """Parse JSON stdout of basic-memory tool search-notes into safe ContextItems.

    Enforces bounded output size, valid JSON root/fields, strict scalar types,
    rejection of booleans-as-integers, negative counts, exact duplicate deduplication,
    and conflicting duplicate rejection.
    """
    try:
        raw_bytes = stdout.encode("utf-8")
    except (UnicodeEncodeError, UnicodeError, ValueError):
        raise RuntimeProbeError("Basic Memory search output contains invalid encoding.") from None

    if len(raw_bytes) > MAX_RETRIEVAL_OUTPUT_BYTES:
        raise RuntimeProbeError("Basic Memory search output exceeded maximum allowed size.") from None

    try:
        data = json.loads(stdout)
    except Exception:  # noqa: BLE001 - parse boundary
        raise RuntimeProbeError("Basic Memory search returned malformed JSON.") from None

    if not isinstance(data, dict):
        raise RuntimeProbeError("Basic Memory search response must be a JSON object.") from None

    # Validate integer fields strictly (rejecting booleans)
    if "total" in data:
        val = data["total"]
        if isinstance(val, bool) or not isinstance(val, int) or val < 0:
            raise RuntimeProbeError("Basic Memory search response field total is invalid.") from None

    if "page_size" in data:
        val = data["page_size"]
        if isinstance(val, bool) or not isinstance(val, int) or val < 0:
            raise RuntimeProbeError("Basic Memory search response field page_size is invalid.") from None

    if "current_page" in data:
        val = data["current_page"]
        if isinstance(val, bool) or not isinstance(val, int) or val < 0:
            raise RuntimeProbeError("Basic Memory search response field current_page is invalid.") from None

    if "total_is_exact" in data and not isinstance(data["total_is_exact"], bool):
        raise RuntimeProbeError("Basic Memory search response field total_is_exact is invalid.") from None

    if "has_more" in data and not isinstance(data["has_more"], bool):
        raise RuntimeProbeError("Basic Memory search response field has_more is invalid.") from None

    results_raw = data.get("results")
    if not isinstance(results_raw, list):
        raise RuntimeProbeError("Basic Memory search results field must be a list.") from None

    items: list[ContextItem] = []
    seen_response_records: dict[str, tuple[str, str, str | None, str | None, str | None]] = {}

    for raw in results_raw:
        if not isinstance(raw, dict):
            raise RuntimeProbeError("Basic Memory search result item must be a JSON object.") from None

        title = raw.get("title")
        if not isinstance(title, str):
            raise RuntimeProbeError("Basic Memory search result title is invalid.") from None

        permalink = raw.get("permalink")
        if not isinstance(permalink, str) or not permalink.strip():
            raise RuntimeProbeError("Basic Memory search result permalink is invalid.") from None

        content = raw.get("content", "")
        if content is None:
            content = ""
        elif not isinstance(content, str):
            raise RuntimeProbeError("Basic Memory search result content is invalid.") from None

        # Issue G: Parse safe metadata.note_type preferentially
        note_type: str | None = None
        raw_meta = raw.get("metadata")
        if raw_meta is not None:
            if not isinstance(raw_meta, dict):
                raise RuntimeProbeError("Basic Memory search result metadata is invalid.") from None
            raw_nt = raw_meta.get("note_type")
            if raw_nt is not None:
                if not isinstance(raw_nt, str):
                    raise RuntimeProbeError("Basic Memory search result note_type is invalid.") from None
                clean_nt = raw_nt.strip()
                if clean_nt:
                    note_type = clean_nt

        if note_type is None:
            raw_type = raw.get("type")
            if raw_type is not None:
                if not isinstance(raw_type, str):
                    raise RuntimeProbeError("Basic Memory search result type is invalid.") from None
                clean_t = raw_type.strip()
                if clean_t:
                    note_type = clean_t

        updated_at = raw.get("updated_at")
        if updated_at is not None and not isinstance(updated_at, str):
            raise RuntimeProbeError("Basic Memory search result updated_at is invalid.") from None

        status = _extract_status(raw)

        # Issue 1: Exact semantic duplicate => deduplicate deterministically; conflicting duplicate => reject
        # Conflicting-duplicate signature includes updated_at so differing timestamps are not discarded
        p_key = permalink.strip().casefold()
        curr_sig = (title.strip(), content.strip(), note_type, status, updated_at)
        if p_key in seen_response_records:
            if seen_response_records[p_key] != curr_sig:
                raise RuntimeProbeError(
                    "Basic Memory search returned contradictory or ambiguous results."
                ) from None
            # Exact semantic duplicate: deduplicate deterministically
            continue
        seen_response_records[p_key] = curr_sig

        item_is_cs = is_current_state_permalink(permalink)

        item = ContextItem(
            project_id=project_id,
            project_name=project_name,
            title=title,
            permalink=permalink,
            content=content,
            is_current_state=item_is_cs,
            note_type=note_type,
            status=status,
            updated_at=updated_at,
        )
        items.append(item)

    return items


def parse_read_note_response(
    stdout: str,
    project_id: str,
    project_name: str,
) -> ContextItem:
    """Parse JSON stdout of basic-memory tool read-note into a safe ContextItem."""
    try:
        raw_bytes = stdout.encode("utf-8")
    except (UnicodeEncodeError, UnicodeError, ValueError):
        raise RuntimeProbeError("Basic Memory read-note output contains invalid encoding.") from None

    if len(raw_bytes) > MAX_RETRIEVAL_OUTPUT_BYTES:
        raise RuntimeProbeError("Basic Memory read-note output exceeded maximum allowed size.") from None

    try:
        data = json.loads(stdout)
    except Exception:  # noqa: BLE001 - parse boundary
        raise RuntimeProbeError("Basic Memory read-note returned malformed JSON.") from None

    if not isinstance(data, dict):
        raise RuntimeProbeError("Basic Memory read-note response must be a JSON object.") from None

    title = data.get("title")
    if not isinstance(title, str):
        raise RuntimeProbeError("Basic Memory read-note result title is invalid.") from None

    permalink = data.get("permalink")
    if not isinstance(permalink, str) or not permalink.strip():
        raise RuntimeProbeError("Basic Memory read-note result permalink is invalid.") from None

    content = data.get("content", "")
    if content is None:
        content = ""
    elif not isinstance(content, str):
        raise RuntimeProbeError("Basic Memory read-note result content is invalid.") from None

    note_type: str | None = None
    status: str | None = None
    updated_at: str | None = None

    if "frontmatter" in data:
        frontmatter = data["frontmatter"]
        if frontmatter is not None:
            if not isinstance(frontmatter, dict):
                raise RuntimeProbeError("Basic Memory read-note frontmatter must be a JSON object.") from None

            raw_type = frontmatter.get("type")
            if raw_type is not None:
                if not isinstance(raw_type, str):
                    raise RuntimeProbeError("Basic Memory read-note frontmatter type is invalid.") from None
                clean_t = raw_type.strip()
                if clean_t:
                    note_type = clean_t

            raw_nt = frontmatter.get("note_type")
            if raw_nt is not None:
                if not isinstance(raw_nt, str):
                    raise RuntimeProbeError("Basic Memory read-note frontmatter note_type is invalid.") from None
                clean_nt = raw_nt.strip()
                if clean_nt:
                    note_type = clean_nt

            raw_status = frontmatter.get("status")
            if raw_status is not None:
                if not isinstance(raw_status, str):
                    raise RuntimeProbeError("Basic Memory read-note frontmatter status is invalid.") from None
                clean_s = raw_status.strip().lower()
                if clean_s in VALID_STATUSES:
                    status = clean_s

            raw_conf = frontmatter.get("confidence")
            if raw_conf is not None:
                if not isinstance(raw_conf, str):
                    raise RuntimeProbeError("Basic Memory read-note frontmatter confidence is invalid.") from None
                if status is None:
                    clean_c = raw_conf.strip().lower()
                    if clean_c in VALID_STATUSES:
                        status = clean_c

            raw_up = frontmatter.get("updated_at")
            if raw_up is not None:
                if not isinstance(raw_up, str):
                    raise RuntimeProbeError("Basic Memory read-note frontmatter updated_at is invalid.") from None
                clean_up = raw_up.strip()
                if clean_up:
                    updated_at = clean_up

    return ContextItem(
        project_id=project_id,
        project_name=project_name,
        title=title,
        permalink=permalink,
        content=content,
        is_current_state=is_current_state_permalink(permalink),
        note_type=note_type,
        status=status,
        updated_at=updated_at,
    )


# ---------------------------------------------------------------------------
# Runner Execution Boundary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class RetrievalRunnerResult:
    """Immutable result of retrieval command invocation with bounded capture."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


def retrieval_subprocess_runner(
    argv: tuple[str, ...] | Sequence[str],
    env: Mapping[str, str],
    timeout: float = DEFAULT_RETRIEVAL_TIMEOUT,
) -> RetrievalRunnerResult:
    """Default no-shell subprocess runner for retrieval capturing up to MAX_RETRIEVAL_OUTPUT_BYTES + 1."""
    try:
        with tempfile.TemporaryFile(mode="w+b") as stdout_f, tempfile.TemporaryFile(mode="w+b") as stderr_f:
            proc = subprocess.run(
                list(argv),
                env=dict(env),
                stdout=stdout_f,
                stderr=stderr_f,
                timeout=timeout,
                shell=False,
                check=False,
            )
            stdout_f.seek(0)
            stderr_f.seek(0)
            stdout_bytes = stdout_f.read(MAX_RETRIEVAL_OUTPUT_BYTES + 1)
            stderr_bytes = stderr_f.read(MAX_RETRIEVAL_OUTPUT_BYTES + 1)

            stdout_str = stdout_bytes.decode("utf-8", errors="replace")
            stderr_str = stderr_bytes.decode("utf-8", errors="replace")

            return RetrievalRunnerResult(
                returncode=proc.returncode,
                stdout=stdout_str,
                stderr=stderr_str,
            )
    except subprocess.TimeoutExpired:
        raise TimeoutError("Subprocess timed out during execution.") from None
    except TimeoutError:
        raise
    except Exception:  # noqa: BLE001 - subprocess boundary
        raise RuntimeProbeError("Subprocess execution failed.") from None


def _normalize_returncode(rc: Any) -> int:
    if isinstance(rc, bool) or not isinstance(rc, int):
        raise RuntimeProbeError("Runner returned an unexpected result type.") from None
    return int(rc)


def _normalize_stream_output(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, (bytes, bytearray)):
        return bytes(val).decode("utf-8", errors="replace")
    return str(val)


def _invoke_runner(
    runner: BasicMemoryRunner,
    argv: tuple[str, ...],
    env: Mapping[str, str],
    timeout: float,
    *,
    operation: str = "search",
) -> _CommandResult:
    """Safely invoke runner callable with timeout and normalize output without leaking secrets."""
    op_label = "read-note" if operation == "read-note" else "search"
    safe_env = MappingProxyType(dict(env))
    try:
        raw_res = runner(argv, safe_env, timeout)
    except (subprocess.TimeoutExpired, TimeoutError):
        raise RuntimeProbeError(f"Basic Memory {op_label} timed out.") from None
    except Exception:  # noqa: BLE001 - custom runners are untrusted extension boundary
        raise RuntimeProbeError(f"Basic Memory {op_label} execution failed.") from None

    try:
        if isinstance(raw_res, tuple):
            if len(raw_res) != 3:
                raise RuntimeProbeError("Runner returned an unexpected result type.") from None
            rc = _normalize_returncode(raw_res[0])
            out = _normalize_stream_output(raw_res[1])
            err = _normalize_stream_output(raw_res[2])
            return _CommandResult(returncode=rc, stdout=out, stderr=err)

        if isinstance(raw_res, RetrievalRunnerResult):
            rc = _normalize_returncode(raw_res.returncode)
            out = _normalize_stream_output(raw_res.stdout)
            err = _normalize_stream_output(raw_res.stderr)
            return _CommandResult(returncode=rc, stdout=out, stderr=err)

        if isinstance(raw_res, BasicMemoryRunnerResult):
            rc = _normalize_returncode(raw_res.returncode)
            out = _normalize_stream_output(raw_res.stdout)
            err = _normalize_stream_output(raw_res.stderr)
            return _CommandResult(returncode=rc, stdout=out, stderr=err)

        if hasattr(raw_res, "returncode"):
            rc = _normalize_returncode(raw_res.returncode)
            out = _normalize_stream_output(getattr(raw_res, "stdout", None))
            err = _normalize_stream_output(getattr(raw_res, "stderr", None))
            return _CommandResult(returncode=rc, stdout=out, stderr=err)
    except RuntimeProbeError:
        raise
    except Exception:  # noqa: BLE001 - normalize boundary
        raise RuntimeProbeError("Runner returned an unexpected result type.") from None

    raise RuntimeProbeError("Runner returned an unexpected result type.") from None


# ---------------------------------------------------------------------------
# Pure Planner & Plan Validation
# ---------------------------------------------------------------------------


def plan_context_retrieval(
    cfg: PersonalTidewayConfig,
    request: ContextRetrievalRequest,
    registry: ProjectRegistry | None = None,
) -> ContextRetrievalPlan:
    """Pure planner constructing an immutable ContextRetrievalPlan.

    Performs zero filesystem mutation and creates exact argv tuples for Basic Memory 0.23.2 CLI.
    """
    if cfg.home.is_symlink():
        raise BoundaryError("Workspace root is a symlink, which is not permitted.")

    layout = get_basic_memory_layout(cfg)
    reg = registry if registry is not None else load_registry(cfg.projects_yaml)
    project_record = resolve_registered_project(request.project, reg)

    project_name = project_record.memory.project_name
    exec_str = str(layout.primary_executable)

    current_state_argv: tuple[str, ...] | None = None
    if request.include_current_state:
        current_state_argv = (
            exec_str,
            "tool",
            "search-notes",
            "--permalink",
            CURRENT_STATE_PERMALINK_PATTERN,
            "--page-size",
            "1",
            "--json",
            "--project",
            project_name,
            "--local",
        )

    query_argv: tuple[str, ...] | None = None
    clean_query = request.query.strip() if request.query is not None else ""
    if clean_query:
        query_argv = (
            exec_str,
            "tool",
            "search-notes",
            clean_query,
            "--page-size",
            str(request.max_items),
            "--json",
            "--project",
            project_name,
            "--local",
        )

    search_argvs_list: list[tuple[str, ...]] = []
    if current_state_argv is not None:
        search_argvs_list.append(current_state_argv)
    if query_argv is not None:
        search_argvs_list.append(query_argv)

    search_argvs = tuple(search_argvs_list)
    is_noop = len(search_argvs) == 0

    env_overrides = compute_canonical_basic_memory_env_overrides(layout)

    return ContextRetrievalPlan(
        request=request,
        layout=layout,
        executable=layout.primary_executable,
        project_record=project_record,
        current_state_argv=current_state_argv,
        query_argv=query_argv,
        search_argvs=search_argvs,
        env_overrides=env_overrides,
        timeout=request.timeout,
        is_noop=is_noop,
    )


def validate_context_retrieval_plan(
    plan: ContextRetrievalPlan,
    cfg: PersonalTidewayConfig | None = None,
) -> None:
    """Validate that plan is canonical, consistent, and safe to execute.

    Raises fixed secret-safe ValidationError or BoundaryError with suppressed causes.
    """
    if not isinstance(plan, ContextRetrievalPlan):
        raise ValidationError("Invalid context retrieval plan type.") from None

    if isinstance(plan.timeout, bool) or not isinstance(plan.timeout, (int, float)):
        raise ValidationError("Plan timeout must be a positive finite number.") from None
    if math.isnan(plan.timeout) or math.isinf(plan.timeout) or plan.timeout <= 0:
        raise ValidationError("Plan timeout must be a positive finite number.") from None

    if not isinstance(plan.layout, BasicMemoryLayout):
        raise ValidationError("Plan layout is invalid.") from None

    if not isinstance(plan.executable, Path):
        raise ValidationError("Plan executable is invalid.") from None

    if plan.executable != plan.layout.primary_executable:
        raise ValidationError("Plan executable does not match canonical primary executable.") from None

    if cfg is not None:
        try:
            canonical_layout = get_basic_memory_layout(cfg)
        except BoundaryError:
            raise
        except Exception:  # noqa: BLE001 - layout boundary
            raise ValidationError("Failed to resolve canonical workspace layout.") from None

        if plan.layout != canonical_layout:
            raise ValidationError("Plan layout does not match canonical workspace layout.") from None

        if plan.executable != canonical_layout.primary_executable:
            raise ValidationError("Plan executable does not match canonical workspace layout.") from None

    canonical_env = compute_canonical_basic_memory_env_overrides(plan.layout)
    if dict(plan.env_overrides) != canonical_env:
        raise ValidationError("Plan env_overrides do not match canonical environment overrides.") from None

    if not isinstance(plan.project_record, ProjectRecord):
        raise ValidationError("Plan project record is invalid.") from None

    try:
        plan.project_record.validate()
    except Exception:  # noqa: BLE001 - record validation
        raise ValidationError("Plan project record failed validation.") from None

    project_name = plan.project_record.memory.project_name
    exec_str = str(plan.executable)

    expected_argvs: list[tuple[str, ...]] = []
    if plan.current_state_argv is not None:
        expected_cs = (
            exec_str,
            "tool",
            "search-notes",
            "--permalink",
            CURRENT_STATE_PERMALINK_PATTERN,
            "--page-size",
            "1",
            "--json",
            "--project",
            project_name,
            "--local",
        )
        if plan.current_state_argv != expected_cs:
            raise ValidationError("Plan current_state_argv does not match expected explicit command.") from None
        expected_argvs.append(expected_cs)

    if plan.query_argv is not None:
        clean_query = plan.request.query.strip() if plan.request.query is not None else ""
        expected_query = (
            exec_str,
            "tool",
            "search-notes",
            clean_query,
            "--page-size",
            str(plan.request.max_items),
            "--json",
            "--project",
            project_name,
            "--local",
        )
        if plan.query_argv != expected_query:
            raise ValidationError("Plan query_argv does not match expected explicit command.") from None
        expected_argvs.append(expected_query)

    if plan.search_argvs != tuple(expected_argvs):
        raise ValidationError("Plan search_argvs do not match expected command sequence.") from None

    if plan.is_noop and len(plan.search_argvs) > 0:
        raise ValidationError("No-op plan must not have search commands.") from None

    if not plan.is_noop and len(plan.search_argvs) == 0:
        raise ValidationError("Plan with no search commands must be marked as no-op.") from None


# ---------------------------------------------------------------------------
# Plan Execution & Orchestration
# ---------------------------------------------------------------------------


def execute_context_retrieval_plan(
    plan: ContextRetrievalPlan,
    *,
    dry_run: bool = False,
    runner: BasicMemoryRunner | None = None,
    cfg: PersonalTidewayConfig | None = None,
) -> ContextRetrievalResult:
    """Execute a previously built ContextRetrievalPlan.

    Fails closed before invoking runner:
    - Requires PersonalTidewayConfig for non-noop non-dry-run execution.
    - Validates plan invariants, canonical layout match, and exact command forms.
    - If dry_run: returns preview without runner calls or disk mutations.
    - If plan is a no-op: returns empty bundle, calls runner zero times.
    - Validates that executable is an installed regular executable inside the PTW Basic Memory service.
    - Builds isolated child environment via allowlist and plan overrides.
    - Runs search commands sequentially with plan.timeout.
    - Halts on first failure without calling subsequent commands.
    - Enforces returncode 0 and validates bounded JSON schema.
    - Translates errors to stable PTW exceptions with suppressed cause chaining.
    """
    if not plan.is_noop and not dry_run and cfg is None:
        raise ValidationError("PersonalTidewayConfig is required for non-dry-run execution.") from None

    validate_context_retrieval_plan(plan, cfg=cfg)

    empty_bundle = ContextBundle(
        project_id=plan.project_record.id,
        project_name=plan.project_record.memory.project_name,
        items=(),
        total_items=0,
        total_chars=0,
        truncated=False,
        has_current_state=False,
    )

    if dry_run:
        return ContextRetrievalResult(
            bundle=empty_bundle,
            dry_run=True,
            preview=plan.preview(),
        )

    if plan.is_noop:
        return ContextRetrievalResult(
            bundle=empty_bundle,
            dry_run=False,
            preview=None,
        )

    assert cfg is not None
    # Re-validate isolated executable immediately prior to execution
    try:
        is_valid = validate_isolated_executable(
            plan.executable,
            service_root=plan.layout.service_root,
            home_boundary=cfg.home,
        )
    except (BoundaryError, RuntimeProbeError):
        raise
    except Exception:  # noqa: BLE001 - probe boundary
        raise RuntimeProbeError("Basic Memory executable missing or invalid.") from None

    if not is_valid:
        raise RuntimeProbeError("Basic Memory executable missing or invalid.") from None

    subprocess_env = build_subprocess_env(plan.env_overrides)
    active_runner = runner if runner is not None else retrieval_subprocess_runner

    current_state_item: ContextItem | None = None
    if plan.current_state_argv is not None:
        cmd_res = _invoke_runner(
            active_runner,
            plan.current_state_argv,
            subprocess_env,
            plan.timeout,
            operation="search",
        )
        if cmd_res.returncode != 0:
            raise RuntimeProbeError("Basic Memory search failed with non-zero exit code.") from None

        cs_items = parse_search_notes_response(
            cmd_res.stdout,
            project_id=plan.project_record.id,
            project_name=plan.project_record.memory.project_name,
            is_current_state=True,
        )
        if cs_items:
            # Look for exact permalink suffix "current-state" (case-insensitive)
            chosen = None
            for it in cs_items:
                if is_current_state_permalink(it.permalink):
                    chosen = it
                    break
            if chosen is not None:
                current_state_item = ContextItem(
                    project_id=chosen.project_id,
                    project_name=chosen.project_name,
                    title=chosen.title,
                    permalink=chosen.permalink,
                    content=chosen.content,
                    is_current_state=True,
                    note_type=chosen.note_type,
                    status=chosen.status,
                    updated_at=chosen.updated_at,
                )

    query_items: list[ContextItem] = []
    if plan.query_argv is not None:
        cmd_res = _invoke_runner(
            active_runner,
            plan.query_argv,
            subprocess_env,
            plan.timeout,
            operation="search",
        )
        if cmd_res.returncode != 0:
            raise RuntimeProbeError("Basic Memory search failed with non-zero exit code.") from None

        query_items = parse_search_notes_response(
            cmd_res.stdout,
            project_id=plan.project_record.id,
            project_name=plan.project_record.memory.project_name,
            is_current_state=False,
        )

    # Deterministic assembly: current-state first, deduplicated by permalink case-insensitively
    candidate_items: list[ContextItem] = []
    seen_permalinks: set[str] = set()

    if current_state_item is not None:
        candidate_items.append(current_state_item)
        seen_permalinks.add(current_state_item.permalink.strip().casefold())

    for it in query_items:
        key = it.permalink.strip().casefold()
        if key in seen_permalinks:
            continue
        seen_permalinks.add(key)
        candidate_items.append(it)

    bundle = build_bounded_context_bundle(
        project_id=plan.project_record.id,
        project_name=plan.project_record.memory.project_name,
        candidate_items=candidate_items,
        max_items=plan.request.max_items,
        max_chars=plan.request.max_chars,
    )

    return ContextRetrievalResult(
        bundle=bundle,
        dry_run=False,
        preview=None,
    )


def retrieve_context(
    cfg: PersonalTidewayConfig,
    request: ContextRetrievalRequest,
    *,
    dry_run: bool = False,
    runner: BasicMemoryRunner | None = None,
    registry: ProjectRegistry | None = None,
) -> ContextRetrievalResult:
    """Plan and execute read-only context retrieval in one step."""
    plan = plan_context_retrieval(cfg, request, registry=registry)
    return execute_context_retrieval_plan(plan, dry_run=dry_run, runner=runner, cfg=cfg)


def read_project_note(
    cfg: PersonalTidewayConfig,
    project: str | ProjectRecord,
    identifier: str,
    *,
    timeout: float = DEFAULT_RETRIEVAL_TIMEOUT,
    runner: BasicMemoryRunner | None = None,
    registry: ProjectRegistry | None = None,
) -> ContextItem:
    """Read an individual project note by identifier using basic-memory tool read-note."""
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValidationError("Note identifier cannot be empty.")

    clean_id = identifier.strip()
    if clean_id.startswith("-"):
        raise ValidationError("Note identifier cannot begin with a dash.")

    if clean_id.startswith(("/", "\\")) or Path(clean_id).is_absolute():
        raise ValidationError("Note identifier cannot be an absolute path.")

    norm_parts = clean_id.replace("\\", "/").split("/")
    if any(p == ".." for p in norm_parts):
        raise ValidationError("Note identifier cannot contain '..' path components.")

    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or math.isnan(timeout)
        or math.isinf(timeout)
        or timeout <= 0
    ):
        raise ValidationError("timeout must be a positive finite number.")

    layout = get_basic_memory_layout(cfg)
    reg = registry if registry is not None else load_registry(cfg.projects_yaml)
    project_record = resolve_registered_project(project, reg)

    try:
        is_valid = validate_isolated_executable(
            layout.primary_executable,
            service_root=layout.service_root,
            home_boundary=cfg.home,
        )
    except (BoundaryError, RuntimeProbeError):
        raise
    except Exception:  # noqa: BLE001 - probe boundary
        raise RuntimeProbeError("Basic Memory executable missing or invalid.") from None

    if not is_valid:
        raise RuntimeProbeError("Basic Memory executable missing or invalid.") from None

    argv = build_read_note_argv(
        layout.primary_executable,
        project_record.memory.project_name,
        clean_id,
    )
    env_overrides = compute_canonical_basic_memory_env_overrides(layout)
    subprocess_env = build_subprocess_env(env_overrides)
    active_runner = runner if runner is not None else retrieval_subprocess_runner

    cmd_res = _invoke_runner(
        active_runner,
        argv,
        subprocess_env,
        timeout,
        operation="read-note",
    )
    if cmd_res.returncode != 0:
        raise RuntimeProbeError("Basic Memory read-note failed with non-zero exit code.") from None

    return parse_read_note_response(
        cmd_res.stdout,
        project_id=project_record.id,
        project_name=project_record.memory.project_name,
    )


__all__ = [
    "CURRENT_STATE_PERMALINK",
    "CURRENT_STATE_PERMALINK_PATTERN",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MAX_ITEMS",
    "DEFAULT_RETRIEVAL_TIMEOUT",
    "HARD_MAX_CHARS",
    "HARD_MAX_ITEMS",
    "MAX_RETRIEVAL_OUTPUT_BYTES",
    "TRUNCATION_MARKER",
    "VALID_STATUSES",
    "ContextBundle",
    "ContextItem",
    "ContextRetrievalPlan",
    "ContextRetrievalPlanPreview",
    "ContextRetrievalRequest",
    "ContextRetrievalResult",
    "RetrievalRunnerResult",
    "build_bounded_context_bundle",
    "build_current_state_search_argv",
    "build_read_note_argv",
    "build_search_notes_argv",
    "execute_context_retrieval_plan",
    "is_current_state_permalink",
    "parse_read_note_response",
    "parse_search_notes_response",
    "plan_context_retrieval",
    "read_project_note",
    "resolve_registered_project",
    "retrieval_subprocess_runner",
    "retrieve_context",
    "validate_context_retrieval_plan",
]
