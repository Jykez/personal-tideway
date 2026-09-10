"""External object proposal, explicit confirmation, and registry persistence for Personal Tideway v2.

This module implements Work Package 8:
- Proposing external subjects (infrastructure, devices, cloud services without local paths).
- Strict validation and collision avoidance with zero filesystem or memory mutations during proposal.
- Explicit confirmation requirement (confirmed=True boolean) before persistence.
- Transactional persistence to canonical projects.yaml without creating filesystem objects.
- Idempotent confirmation and clear conflict detection.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
import uuid

from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import PROJECT_KIND_EXTERNAL
from personal_tideway.core.project_resolver import (
    ProjectResolutionResult,
    ProposedBindingChanges,
    derive_safe_slug,
    is_token_available,
)
from personal_tideway.core.registry import (
    ProjectRecord,
    ProjectRegistry,
    load_registry,
    now_rfc3339,
    save_registry,
    validate_rfc3339_utc,
    validate_token_format,
    validate_uuid4,
)
from personal_tideway.exceptions import ValidationError


@dataclass
class ExternalRegistrationResult(ProjectResolutionResult):
    """Structured result of external-object proposal and confirmation."""

    created: bool = False
    action: str = ""

    def __init__(
        self,
        status: str,
        project_id: str | None = None,
        project: ProjectRecord | None = None,
        record: ProjectRecord | None = None,
        proposed_record: ProjectRecord | None = None,
        proposal: ProjectRecord | None = None,
        evidence: list[str] | None = None,
        created: bool = False,
        requires_confirmation: bool = False,
        action: str = "",
        candidate_metadata: dict[str, Any] | None = None,
        ambiguous_project_ids: list[str] | None = None,
        bindings_changed: bool = False,
        proposed_additions: ProposedBindingChanges | None = None,
        proposed_removals: ProposedBindingChanges | None = None,
    ) -> None:
        eff_project = record if record is not None else project
        eff_proposal = proposal if proposal is not None else proposed_record
        super().__init__(
            status=status,
            project_id=project_id,
            project=eff_project,
            evidence=evidence or ["external"],
            requires_confirmation=requires_confirmation,
            candidate_metadata=candidate_metadata,
            ambiguous_project_ids=ambiguous_project_ids or [],
            proposed_record=eff_proposal,
            bindings_changed=bindings_changed,
            proposed_additions=proposed_additions or ProposedBindingChanges(),
            proposed_removals=proposed_removals or ProposedBindingChanges(),
        )
        self.created = created
        self.action = action

    @property
    def record(self) -> ProjectRecord | None:
        return self.project

    @record.setter
    def record(self, val: ProjectRecord | None) -> None:
        self.project = val

    @property
    def proposal(self) -> ProjectRecord | None:
        return self.proposed_record

    @proposal.setter
    def proposal(self, val: ProjectRecord | None) -> None:
        self.proposed_record = val

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data["created"] = self.created
        if self.action:
            data["action"] = self.action
        if self.record is not None:
            data["record"] = self.record.to_dict()
        if self.proposal is not None:
            data["proposal"] = self.proposal.to_dict()
            data["proposed_record"] = self.proposal.to_dict()
        return data


def propose_external_project(
    display_name: str,
    *,
    slug: str | None = None,
    aliases: list[str] | None = None,
    registry: ProjectRegistry | None = None,
    cfg: PersonalTidewayConfig | None = None,
    id_factory: Callable[[], str] | None = None,
    clock: Callable[[], str] | None = None,
) -> ExternalRegistrationResult:
    """Propose an external infrastructure subject candidate without persisting anything.

    External projects represent infrastructure/subjects without a local source directory
    (e.g., OpenWrt router, Proxmox hypervisor, networked printer).
    Returns a typed candidate with requires_confirmation=True, created=False.
    Zero filesystem or in-memory registry modifications.
    """
    if display_name is None or not isinstance(display_name, str) or not display_name.strip():
        raise ValidationError("Field 'display_name' must be a non-empty string.")

    eff_display_name = display_name.strip()

    active_cfg = cfg or PersonalTidewayConfig.resolve()
    active_registry = registry if registry is not None else load_registry(active_cfg.projects_yaml)

    # Validate or generate ID
    new_id = (id_factory or (lambda: str(uuid.uuid4())))()
    new_id = validate_uuid4(new_id, "project_id")

    # Validate or derive slug
    if slug is not None:
        clean_slug = validate_token_format(slug, "slug")
        if not is_token_available(clean_slug, active_registry):
            raise ValidationError(f"Project slug '{clean_slug}' is already in use in registry.")
        final_slug = clean_slug
    else:
        base_slug = derive_safe_slug(eff_display_name)
        if is_token_available(base_slug, active_registry):
            final_slug = base_slug
        else:
            short_suffix = new_id[:8]
            final_slug = f"{base_slug}-{short_suffix}"
            if not is_token_available(final_slug, active_registry):
                final_slug = f"{base_slug}-{new_id.replace('-', '')[:12]}"
            if not is_token_available(final_slug, active_registry):
                final_slug = f"{base_slug}-{new_id.replace('-', '')}"
            if not is_token_available(final_slug, active_registry):
                raise ValidationError("Unable to derive a unique project slug from generated project ID.")

    # Validate aliases
    clean_aliases: list[str] = []
    if aliases is not None:
        if not isinstance(aliases, list):
            raise ValidationError("Aliases must be a list of strings.")
        seen_aliases: set[str] = set()
        for a in aliases:
            clean_a = validate_token_format(a, "alias")
            lower_a = clean_a.lower()
            if lower_a in seen_aliases:
                raise ValidationError(f"Duplicate alias '{clean_a}' in aliases list.")
            seen_aliases.add(lower_a)
            if not is_token_available(clean_a, active_registry):
                raise ValidationError(f"Alias '{clean_a}' is already in use in registry.")
            if lower_a == final_slug.lower() or lower_a == new_id.lower():
                raise ValidationError(f"Alias '{clean_a}' cannot be identical to project slug or ID.")
            clean_aliases.append(clean_a)

    # Timestamp
    c_func = clock or now_rfc3339
    ts = c_func()
    validate_rfc3339_utc(ts, "created_at")

    # Construct ProjectRecord
    record = ProjectRecord.create(
        project_id=new_id,
        slug=final_slug,
        display_name=eff_display_name,
        kind=PROJECT_KIND_EXTERNAL,
        aliases=clean_aliases,
        paths=[],
        git_common_dirs=[],
        git_remotes=[],
        created_at=ts,
        updated_at=ts,
    )

    return ExternalRegistrationResult(
        status="candidate",
        project_id=record.id,
        proposed_record=record,
        evidence=["external"],
        created=False,
        requires_confirmation=True,
        action="propose_external",
        candidate_metadata={
            "proposed_id": record.id,
            "proposed_slug": record.slug,
            "proposed_display_name": record.display_name,
            "kind": PROJECT_KIND_EXTERNAL,
        },
    )


def confirm_external_project(
    proposal: ProjectRecord | ExternalRegistrationResult | dict[str, Any],
    *,
    confirmed: bool = False,
    cfg: PersonalTidewayConfig | None = None,
    registry: ProjectRegistry | None = None,
    dry_run: bool = False,
) -> ExternalRegistrationResult:
    """Explicitly confirm and persist a previously proposed external project candidate.

    Enforces policies:
    - confirmed must be True as a real boolean. False or omitted never persists.
    - Proposal must be a valid external record with empty bindings and kind=external.
    - Reject malformed, tampered, non-external, or bound proposals.
    - Idempotent: confirming an already-persisted identical proposal returns that record.
    - Conflicting ID, slug, or alias fails clearly with ValidationError; never silently binds.
    - Transactional: writes only canonical cfg.projects_yaml; failed save leaves caller registry untouched.
    - No filesystem objects are created for the external subject.
    """
    if type(confirmed) is not bool or confirmed is not True:
        raise ValidationError(
            "Explicit confirmation (confirmed=True) is required to persist an external project."
        )

    # Extract and validate ProjectRecord from proposal envelope or canonical record
    if isinstance(proposal, ExternalRegistrationResult):
        if proposal.status != "candidate":
            raise ValidationError("Invalid proposal status. Expected 'candidate'.")
        if proposal.action != "propose_external":
            raise ValidationError("Invalid proposal action. Expected 'propose_external'.")
        if proposal.created is not False:
            raise ValidationError("Proposal envelope must have created=False.")
        if proposal.requires_confirmation is not True:
            raise ValidationError("Proposal envelope must have requires_confirmation=True.")
        if proposal.project is not None:
            raise ValidationError("Proposal candidate envelope must not contain an active project record.")
        if proposal.proposed_record is None:
            raise ValidationError("Proposal candidate envelope missing proposed record.")

        rec = proposal.proposed_record
        if proposal.project_id != rec.id:
            raise ValidationError("Proposal envelope project_id does not match record ID.")
        meta = proposal.candidate_metadata
        if not isinstance(meta, dict):
            raise ValidationError("Proposal candidate envelope missing candidate_metadata.")
        if meta.get("proposed_id") != rec.id:
            raise ValidationError("Proposal candidate_metadata 'proposed_id' does not match record ID.")
        if meta.get("proposed_slug") != rec.slug:
            raise ValidationError("Proposal candidate_metadata 'proposed_slug' does not match record slug.")
        if meta.get("proposed_display_name") != rec.display_name:
            raise ValidationError(
                "Proposal candidate_metadata 'proposed_display_name' does not match record display_name."
            )
        if meta.get("kind") != rec.kind:
            raise ValidationError("Proposal candidate_metadata 'kind' does not match record kind.")
    elif isinstance(proposal, ProjectRecord):
        rec = proposal
    elif isinstance(proposal, dict):
        if "candidate_metadata" in proposal or "status" in proposal:
            if proposal.get("status") != "candidate":
                raise ValidationError("Invalid proposal status. Expected 'candidate'.")
            if proposal.get("action") != "propose_external":
                raise ValidationError("Invalid proposal action. Expected 'propose_external'.")
            if proposal.get("created") is not False:
                raise ValidationError("Serialized proposal envelope must have created=False.")
            if proposal.get("requires_confirmation") is not True:
                raise ValidationError("Serialized proposal envelope must have requires_confirmation=True.")
            if proposal.get("record") is not None:
                raise ValidationError("Serialized proposal envelope must not contain an active record.")

            raw_prop = proposal.get("proposal")
            raw_proposed_record = proposal.get("proposed_record")
            if raw_prop is None and raw_proposed_record is None:
                raise ValidationError("Serialized proposal envelope missing proposed record dict.")

            rec1 = ProjectRecord.from_dict(raw_prop) if raw_prop is not None else None
            rec2 = ProjectRecord.from_dict(raw_proposed_record) if raw_proposed_record is not None else None

            if rec1 is not None and rec2 is not None:
                if rec1.to_dict() != rec2.to_dict():
                    raise ValidationError(
                        "Proposal and proposed_record in serialized envelope must be identical."
                    )
                rec = rec1
            elif rec1 is not None:
                rec = rec1
            else:
                assert rec2 is not None
                rec = rec2

            if proposal.get("project_id") != rec.id:
                raise ValidationError("Serialized proposal envelope project_id does not match record ID.")
            meta = proposal.get("candidate_metadata")
            if not isinstance(meta, dict):
                raise ValidationError("Serialized proposal envelope missing candidate_metadata.")
            if meta.get("proposed_id") != rec.id:
                raise ValidationError("Serialized proposal candidate_metadata 'proposed_id' does not match record ID.")
            if meta.get("proposed_slug") != rec.slug:
                raise ValidationError("Serialized proposal candidate_metadata 'proposed_slug' does not match record slug.")
            if meta.get("proposed_display_name") != rec.display_name:
                raise ValidationError(
                    "Serialized proposal candidate_metadata 'proposed_display_name' does not match record display_name."
                )
            if meta.get("kind") != rec.kind:
                raise ValidationError("Serialized proposal candidate_metadata 'kind' does not match record kind.")
        else:
            rec = ProjectRecord.from_dict(proposal)
    else:
        raise ValidationError(
            f"Proposal must be an ExternalRegistrationResult, ProjectRecord, or dict, got {type(proposal).__name__}."
        )


    if rec is None:
        raise ValidationError("Proposal does not contain a valid project record.")

    # Validate integrity
    rec.validate()

    # Kind must be external
    if rec.kind != PROJECT_KIND_EXTERNAL:
        raise ValidationError(
            f"Invalid project kind '{rec.kind}' for external project confirmation. Expected '{PROJECT_KIND_EXTERNAL}'."
        )

    # Bindings must be empty
    if rec.bindings.paths or rec.bindings.git_common_dirs or rec.bindings.git_remotes:
        raise ValidationError(
            "External project must have empty bindings (no paths, git_common_dirs, or git_remotes)."
        )

    active_cfg = cfg or PersonalTidewayConfig.resolve()
    active_registry = registry if registry is not None else load_registry(active_cfg.projects_yaml)

    # Check for existing project with same ID, slug, or alias
    existing_by_id: ProjectRecord | None = None
    existing_by_slug: ProjectRecord | None = None

    for p in active_registry.projects:
        if p.id.lower() == rec.id.lower():
            existing_by_id = p
        if p.slug.lower() == rec.slug.lower():
            existing_by_slug = p

        # Check cross-token collisions with other projects
        if p.id.lower() != rec.id.lower():
            if rec.slug.lower() == p.id.lower() or any(pa.lower() == rec.slug.lower() for pa in p.aliases):
                raise ValidationError(
                    f"Registry collision: slug '{rec.slug}' conflicts with existing project '{p.slug}' (ID: {p.id})."
                )
            if rec.id.lower() == p.slug.lower() or any(pa.lower() == rec.id.lower() for pa in p.aliases):
                raise ValidationError(
                    f"Registry collision: ID '{rec.id}' conflicts with existing project '{p.slug}'."
                )
            for a in rec.aliases:
                if p.id.lower() == a.lower() or p.slug.lower() == a.lower() or any(pa.lower() == a.lower() for pa in p.aliases):
                    raise ValidationError(
                        f"Registry collision: alias '{a}' conflicts with existing project '{p.slug}' (ID: {p.id})."
                    )

    # Idempotence or ID collision: exact canonical ProjectRecord equality
    if existing_by_id is not None:
        if existing_by_id.to_dict() == rec.to_dict():
            return ExternalRegistrationResult(
                status="resolved",
                project_id=existing_by_id.id,
                record=existing_by_id,
                evidence=["external"],
                created=False,
                requires_confirmation=False,
                action="noop_exact",
            )
        raise ValidationError(
            f"Project with ID '{rec.id}' already exists in registry with different attributes."
        )

    # Slug collision check with a different project ID
    if existing_by_slug is not None:
        raise ValidationError(
            f"Registry collision: slug '{rec.slug}' is already used by existing project '{existing_by_slug.slug}' (ID: {existing_by_slug.id})."
        )

    # Dry-run handling
    if dry_run:
        return ExternalRegistrationResult(
            status="candidate",
            project_id=rec.id,
            proposed_record=rec,
            evidence=["external"],
            created=False,
            requires_confirmation=True,
            action="confirm_external",
            candidate_metadata={
                "proposed_id": rec.id,
                "proposed_slug": rec.slug,
                "proposed_display_name": rec.display_name,
                "kind": PROJECT_KIND_EXTERNAL,
                "dry_run": True,
            },
        )

    # Transactional persistence: validate copy first, save, then update caller registry
    persisted_registry = ProjectRegistry(
        version=active_registry.version,
        projects=[*active_registry.projects, rec],
    )
    persisted_registry.validate()
    save_registry(persisted_registry, active_cfg)
    if registry is not None:
        active_registry.add_project(rec)

    return ExternalRegistrationResult(
        status="resolved",
        project_id=rec.id,
        record=rec,
        evidence=["external"],
        created=True,
        requires_confirmation=False,
        action="confirm_external",
    )


class ExternalRegistrationService:
    """Service for external project proposals, explicit confirmations, and persistence."""

    def __init__(
        self,
        cfg: PersonalTidewayConfig | None = None,
        registry: ProjectRegistry | None = None,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.cfg = cfg
        self.registry = registry
        self.id_factory = id_factory
        self.clock = clock

    def propose(
        self,
        display_name: str,
        *,
        slug: str | None = None,
        aliases: list[str] | None = None,
    ) -> ExternalRegistrationResult:
        return propose_external_project(
            display_name,
            slug=slug,
            aliases=aliases,
            registry=self.registry,
            cfg=self.cfg,
            id_factory=self.id_factory,
            clock=self.clock,
        )

    def confirm(
        self,
        proposal: ProjectRecord | ExternalRegistrationResult | dict[str, Any],
        *,
        confirmed: bool = False,
        dry_run: bool = False,
    ) -> ExternalRegistrationResult:
        return confirm_external_project(
            proposal,
            confirmed=confirmed,
            cfg=self.cfg,
            registry=self.registry,
            dry_run=dry_run,
        )
