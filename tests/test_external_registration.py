"""Comprehensive tests for external-object proposal, explicit confirmation, and registry persistence.

Tests Personal Tideway v2 Work Package 8:
- Proposal without writes or in-memory changes
- Confirmed exact persistence and empty bindings
- Strict boolean confirmed flag requirement
- Exact proposal idempotence without duplicate creation
- Conflict detection on ID, slug, and alias
- Rejection of malformed, tampered, non-external, or bound proposals
- Custom Unicode display name with safe derived slug and collision resolution
- Invalid name and metadata handling
- Dry persistence and failure rollback leaving caller registry unchanged
- Source/external path untouched (no local directory created or required)
- Resolution by slug, alias, and ID after creation
"""

from pathlib import Path
import uuid
import pytest

from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import (
    DEFAULT_MEMORY_BACKEND,
    PROJECT_KIND_DIRECTORY,
    PROJECT_KIND_EXTERNAL,
    PROJECT_KIND_GIT,
)
from personal_tideway.core import (
    ExternalRegistrationResult,
    ExternalRegistrationService,
    confirm_external_project,
    propose_external_project,
    resolve_project,
)
from personal_tideway.core.registry import (
    ProjectBindings,
    ProjectMemory,
    ProjectRecord,
    ProjectRegistry,
    load_registry,
)
from personal_tideway.exceptions import ConfigError, ValidationError


def test_proposal_no_writes_and_no_in_memory_changes(tmp_path: Path):
    """Proposal creates a candidate record, requires confirmation, and mutates nothing."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)
    registry = ProjectRegistry.empty()

    res = propose_external_project(
        "Home Proxmox Server",
        registry=registry,
        cfg=cfg,
    )

    # Validate returned candidate
    assert isinstance(res, ExternalRegistrationResult)
    assert res.status == "candidate"
    assert res.created is False
    assert res.requires_confirmation is True
    assert res.action == "propose_external"
    assert res.evidence == ["external"]
    assert res.record is None
    assert res.proposal is not None
    assert res.project_id == res.proposal.id

    # Validate candidate record details
    cand = res.proposal
    assert cand.kind == PROJECT_KIND_EXTERNAL
    assert cand.slug == "home-proxmox-server"
    assert cand.display_name == "Home Proxmox Server"
    assert cand.aliases == []
    assert cand.bindings.paths == []
    assert cand.bindings.git_common_dirs == []
    assert cand.bindings.git_remotes == []
    assert cand.memory.backend == DEFAULT_MEMORY_BACKEND
    assert cand.memory.project_name == f"ptw-home-proxmox-server-{cand.id[:8]}"
    assert cand.memory.path == f"projects/{cand.id}/memory"
    assert cand.created_at == cand.updated_at
    assert cand.created_at.endswith("Z")

    # Verify zero in-memory registry changes
    assert len(registry.projects) == 0

    # Verify zero disk writes
    assert not cfg.projects_yaml.exists()

    # Verify safe serialization
    as_dict = res.to_dict()
    assert as_dict["status"] == "candidate"
    assert as_dict["created"] is False
    assert as_dict["requires_confirmation"] is True
    assert as_dict["action"] == "propose_external"
    assert as_dict["candidate_metadata"]["kind"] == PROJECT_KIND_EXTERNAL
    assert as_dict["proposal"]["id"] == cand.id


def test_confirmed_exact_persistence_and_empty_bindings(tmp_path: Path):
    """Confirmed creation persists exactly the proposed record with empty bindings."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)
    registry = ProjectRegistry.empty()

    fixed_id = "11111111-2222-4333-8444-555555555555"
    fixed_ts = "2026-09-10T12:00:00Z"

    prop = propose_external_project(
        "Network Printer",
        slug="office-printer",
        aliases=["printer-3d", "hp-laser"],
        registry=registry,
        cfg=cfg,
        id_factory=lambda: fixed_id,
        clock=lambda: fixed_ts,
    )

    confirm_res = confirm_external_project(
        prop,
        confirmed=True,
        registry=registry,
        cfg=cfg,
    )

    assert confirm_res.status == "resolved"
    assert confirm_res.created is True
    assert confirm_res.requires_confirmation is False
    assert confirm_res.action == "confirm_external"
    assert confirm_res.project_id == fixed_id

    rec = confirm_res.record
    assert rec is not None
    assert rec.id == fixed_id
    assert rec.slug == "office-printer"
    assert rec.display_name == "Network Printer"
    assert rec.kind == PROJECT_KIND_EXTERNAL
    assert rec.aliases == ["printer-3d", "hp-laser"]
    assert rec.bindings.paths == []
    assert rec.bindings.git_common_dirs == []
    assert rec.bindings.git_remotes == []
    assert rec.memory.backend == DEFAULT_MEMORY_BACKEND
    assert rec.memory.project_name == f"ptw-office-printer-{fixed_id[:8]}"
    assert rec.memory.path == f"projects/{fixed_id}/memory"
    assert rec.created_at == fixed_ts
    assert rec.updated_at == fixed_ts

    # In-memory registry updated
    assert len(registry.projects) == 1
    assert registry.projects[0].id == fixed_id

    # Canonical file persisted
    assert cfg.projects_yaml.is_file()
    saved_reg = load_registry(cfg.projects_yaml)
    assert len(saved_reg) == 1
    saved_p = saved_reg.projects[0]
    assert saved_p.id == fixed_id
    assert saved_p.slug == "office-printer"
    assert saved_p.kind == PROJECT_KIND_EXTERNAL
    assert saved_p.bindings.paths == []
    assert saved_p.bindings.git_common_dirs == []
    assert saved_p.bindings.git_remotes == []


def test_confirmed_flag_strict(tmp_path: Path):
    """confirmed=True as a real bool is strictly required; false or non-bool never persists."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)
    registry = ProjectRegistry.empty()

    prop = propose_external_project(
        "OpenWrt Router",
        registry=registry,
        cfg=cfg,
    )

    # Omitted confirmed argument
    with pytest.raises(ValidationError, match="confirmed=True"):
        confirm_external_project(prop, registry=registry, cfg=cfg)

    # Explicit False
    with pytest.raises(ValidationError, match="confirmed=True"):
        confirm_external_project(prop, confirmed=False, registry=registry, cfg=cfg)

    # Truthy int (1 is not a bool)
    with pytest.raises(ValidationError, match="confirmed=True"):
        confirm_external_project(prop, confirmed=1, registry=registry, cfg=cfg)

    # Truthy string
    with pytest.raises(ValidationError, match="confirmed=True"):
        confirm_external_project(prop, confirmed="true", registry=registry, cfg=cfg)

    # None
    with pytest.raises(ValidationError, match="confirmed=True"):
        confirm_external_project(prop, confirmed=None, registry=registry, cfg=cfg)

    # Verify no file written and registry empty
    assert not cfg.projects_yaml.exists()
    assert len(registry.projects) == 0


def test_exact_proposal_idempotence(tmp_path: Path):
    """Confirming an already-persisted proposal returns that record without duplicate.

    Exact canonical equality is required: modified created_at, updated_at, or alias casing
    with the same ID must be rejected, not treated as noop.
    """
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)
    registry = ProjectRegistry.empty()

    prop = propose_external_project(
        "Smart Gateway",
        slug="smart-gw",
        registry=registry,
        cfg=cfg,
    )

    # First confirmation
    res1 = confirm_external_project(prop, confirmed=True, registry=registry, cfg=cfg)
    assert res1.created is True
    assert res1.status == "resolved"
    assert res1.action == "confirm_external"
    assert len(registry.projects) == 1

    # Second confirmation of the exact same proposal
    res2 = confirm_external_project(prop, confirmed=True, registry=registry, cfg=cfg)
    assert res2.created is False
    assert res2.status == "resolved"
    assert res2.action == "noop_exact"
    assert res2.record.id == res1.record.id
    assert len(registry.projects) == 1

    # Verify on disk
    saved_reg = load_registry(cfg.projects_yaml)
    assert len(saved_reg) == 1

    # Changed created_at with same ID is rejected, not treated as noop
    tampered_created = ProjectRecord.from_dict({**res1.record.to_dict(), "created_at": "2026-09-10T15:00:00Z"})
    with pytest.raises(ValidationError, match="already exists in registry with different attributes"):
        confirm_external_project(tampered_created, confirmed=True, registry=registry, cfg=cfg)

    # Changed updated_at with same ID is rejected, not treated as noop
    tampered_updated = ProjectRecord.from_dict({**res1.record.to_dict(), "updated_at": "2026-09-10T15:00:00Z"})
    with pytest.raises(ValidationError, match="already exists in registry with different attributes"):
        confirm_external_project(tampered_updated, confirmed=True, registry=registry, cfg=cfg)

    # Changed alias casing with same ID is rejected, not treated as noop
    prop_with_alias = propose_external_project("Device With Alias", slug="dev-alias", aliases=["dev-a"], registry=registry, cfg=cfg)
    res_alias = confirm_external_project(prop_with_alias, confirmed=True, registry=registry, cfg=cfg)
    tampered_alias = ProjectRecord.from_dict({**res_alias.record.to_dict(), "aliases": ["DEV-A"]})
    with pytest.raises(ValidationError, match="already exists in registry with different attributes"):
        confirm_external_project(tampered_alias, confirmed=True, registry=registry, cfg=cfg)


def test_slug_id_alias_conflict(tmp_path: Path):
    """Conflicting ID, slug, or alias must fail clearly; never silently bind to another project."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)
    registry = ProjectRegistry.empty()

    # Pre-register existing project
    prop_a = propose_external_project(
        "Primary Router",
        slug="router-core",
        aliases=["gateway-main"],
        registry=registry,
        cfg=cfg,
    )
    res_a = confirm_external_project(prop_a, confirmed=True, registry=registry, cfg=cfg)
    assert res_a.created is True

    # 1. Conflict on slug during propose
    with pytest.raises(ValidationError, match="slug 'router-core' is already in use"):
        propose_external_project(
            "Secondary Router",
            slug="router-core",
            registry=registry,
            cfg=cfg,
        )

    # 2. Conflict on alias during propose (verifies production capitalization "Alias")
    with pytest.raises(ValidationError, match="Alias 'gateway-main' is already in use"):
        propose_external_project(
            "Secondary Router",
            slug="router-backup",
            aliases=["gateway-main"],
            registry=registry,
            cfg=cfg,
        )

    # 3. Conflict on alias colliding with existing slug
    with pytest.raises(ValidationError, match="Alias 'router-core' is already in use"):
        propose_external_project(
            "Secondary Router",
            slug="router-backup",
            aliases=["router-core"],
            registry=registry,
            cfg=cfg,
        )


    # 4. Conflict on ID with altered attributes during confirmation
    tampered_same_id = ProjectRecord.create(
        project_id=res_a.record.id,
        slug="different-slug",
        display_name="Altered Name",
        kind=PROJECT_KIND_EXTERNAL,
        paths=[],
        git_common_dirs=[],
        git_remotes=[],
    )
    with pytest.raises(ValidationError, match="already exists in registry with different attributes"):
        confirm_external_project(
            tampered_same_id,
            confirmed=True,
            registry=registry,
            cfg=cfg,
        )

    # Registry still contains only the single uncorrupted project
    assert len(registry.projects) == 1
    assert registry.projects[0].slug == "router-core"


def test_malformed_tampered_non_external_bound_proposal_rejection(tmp_path: Path):
    """Reject malformed, tampered, non-external, and bound proposals."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)
    registry = ProjectRegistry.empty()

    # Rejection of kind="git"
    git_rec = ProjectRecord.create(
        project_id=str(uuid.uuid4()),
        slug="git-subject",
        display_name="Git Subject",
        kind=PROJECT_KIND_GIT,
        paths=["/var/repo"],
    )
    with pytest.raises(ValidationError, match="Invalid project kind 'git' for external project"):
        confirm_external_project(git_rec, confirmed=True, registry=registry, cfg=cfg)

    # Rejection of kind="directory"
    dir_rec = ProjectRecord.create(
        project_id=str(uuid.uuid4()),
        slug="dir-subject",
        display_name="Dir Subject",
        kind=PROJECT_KIND_DIRECTORY,
        paths=["/var/dir"],
    )
    with pytest.raises(ValidationError, match="Invalid project kind 'directory' for external project"):
        confirm_external_project(dir_rec, confirmed=True, registry=registry, cfg=cfg)

    # Rejection of external project with path bindings
    pid = str(uuid.uuid4())
    bound_path_rec = ProjectRecord(
        id=pid,
        slug="bound-ext",
        display_name="Bound Ext",
        kind=PROJECT_KIND_EXTERNAL,
        aliases=[],
        bindings=ProjectBindings(paths=["/var/opt/data"]),
        memory=ProjectMemory(
            backend=DEFAULT_MEMORY_BACKEND,
            project_name=f"ptw-bound-ext-{pid[:8]}",
            path=f"projects/{pid}/memory",
        ),
    )
    with pytest.raises(ValidationError, match="must not have path bindings|empty bindings"):
        confirm_external_project(bound_path_rec, confirmed=True, registry=registry, cfg=cfg)

    # Rejection of external project with git bindings
    pid2 = str(uuid.uuid4())
    bound_git_rec = ProjectRecord(
        id=pid2,
        slug="bound-git-ext",
        display_name="Bound Git Ext",
        kind=PROJECT_KIND_EXTERNAL,
        aliases=[],
        bindings=ProjectBindings(git_remotes=["github.com/foo/bar"]),
        memory=ProjectMemory(
            backend=DEFAULT_MEMORY_BACKEND,
            project_name=f"ptw-bound-git-ext-{pid2[:8]}",
            path=f"projects/{pid2}/memory",
        ),
    )
    with pytest.raises(ValidationError, match="must not have git bindings|empty bindings"):
        confirm_external_project(bound_git_rec, confirmed=True, registry=registry, cfg=cfg)

    # Rejection of malformed dictionary proposal
    with pytest.raises(ValidationError):
        confirm_external_project(
            {"id": "not-a-uuid", "slug": "bad", "kind": "external"},
            confirmed=True,
            registry=registry,
            cfg=cfg,
        )

    # Rejection of completely invalid proposal type
    with pytest.raises(ValidationError, match="Proposal must be"):
        confirm_external_project(
            12345,
            confirmed=True,
            registry=registry,
            cfg=cfg,
        )


def test_envelope_validation_and_tampering_rejection(tmp_path: Path):
    """Validate envelope shape and reject any tampered fields in candidate envelope."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)
    registry = ProjectRegistry.empty()

    prop = propose_external_project("Switch", slug="sw-1", registry=registry, cfg=cfg)

    # 1. Tampered status
    t_status = ExternalRegistrationResult(
        status="resolved",
        project_id=prop.project_id,
        proposed_record=prop.proposal,
        action="propose_external",
        requires_confirmation=True,
        candidate_metadata=dict(prop.candidate_metadata or {}),
    )
    with pytest.raises(ValidationError, match="Invalid proposal status"):
        confirm_external_project(t_status, confirmed=True, registry=registry, cfg=cfg)

    # 2. Tampered action
    t_action = ExternalRegistrationResult(
        status="candidate",
        project_id=prop.project_id,
        proposed_record=prop.proposal,
        action="other_action",
        requires_confirmation=True,
        candidate_metadata=dict(prop.candidate_metadata or {}),
    )
    with pytest.raises(ValidationError, match="Invalid proposal action"):
        confirm_external_project(t_action, confirmed=True, registry=registry, cfg=cfg)

    # 3. Tampered created=True
    t_created = ExternalRegistrationResult(
        status="candidate",
        project_id=prop.project_id,
        proposed_record=prop.proposal,
        created=True,
        action="propose_external",
        requires_confirmation=True,
        candidate_metadata=dict(prop.candidate_metadata or {}),
    )
    with pytest.raises(ValidationError, match="must have created=False"):
        confirm_external_project(t_created, confirmed=True, registry=registry, cfg=cfg)

    # 4. Tampered requires_confirmation=False
    t_req = ExternalRegistrationResult(
        status="candidate",
        project_id=prop.project_id,
        proposed_record=prop.proposal,
        action="propose_external",
        requires_confirmation=False,
        candidate_metadata=dict(prop.candidate_metadata or {}),
    )
    with pytest.raises(ValidationError, match="must have requires_confirmation=True"):
        confirm_external_project(t_req, confirmed=True, registry=registry, cfg=cfg)

    # 5. Envelope contains active project record
    t_active = ExternalRegistrationResult(
        status="candidate",
        project_id=prop.project_id,
        record=prop.proposal,
        proposed_record=prop.proposal,
        action="propose_external",
        requires_confirmation=True,
        candidate_metadata=dict(prop.candidate_metadata or {}),
    )
    with pytest.raises(ValidationError, match="must not contain an active project record"):
        confirm_external_project(t_active, confirmed=True, registry=registry, cfg=cfg)

    # 6. Mismatched project_id in envelope
    t_mismatch_id = ExternalRegistrationResult(
        status="candidate",
        project_id=str(uuid.uuid4()),
        proposed_record=prop.proposal,
        action="propose_external",
        requires_confirmation=True,
        candidate_metadata=dict(prop.candidate_metadata or {}),
    )
    with pytest.raises(ValidationError, match="does not match record ID"):
        confirm_external_project(t_mismatch_id, confirmed=True, registry=registry, cfg=cfg)

    # 7. Tampered candidate_metadata slug
    meta_tampered = dict(prop.candidate_metadata or {})
    meta_tampered["proposed_slug"] = "tampered-slug"
    t_meta = ExternalRegistrationResult(
        status="candidate",
        project_id=prop.project_id,
        proposed_record=prop.proposal,
        action="propose_external",
        requires_confirmation=True,
        candidate_metadata=meta_tampered,
    )
    with pytest.raises(ValidationError, match="does not match record slug"):
        confirm_external_project(t_meta, confirmed=True, registry=registry, cfg=cfg)

    # 8. Serialized envelope dict tampering
    serialized = prop.to_dict()
    serialized["candidate_metadata"]["proposed_id"] = str(uuid.uuid4())
    with pytest.raises(ValidationError, match="does not match record ID"):
        confirm_external_project(serialized, confirmed=True, registry=registry, cfg=cfg)


def test_serialized_envelope_mismatch_and_single_record_acceptance(tmp_path: Path):
    """If both proposal and proposed_record are in serialized envelope, they must be identical.

    If only one is present, it is accepted cleanly.
    """
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)
    registry = ProjectRegistry.empty()

    prop = propose_external_project("Printer", slug="office-printer", registry=registry, cfg=cfg)
    envelope = prop.to_dict()

    # Exact regression: modifying only proposal["display_name"] while leaving proposed_record
    tampered = dict(envelope)
    tampered["proposal"] = dict(envelope["proposal"])
    tampered["proposal"]["display_name"] = "Tampered Display Name"
    with pytest.raises(
        ValidationError,
        match="Proposal and proposed_record in serialized envelope must be identical",
    ):
        confirm_external_project(tampered, confirmed=True, registry=registry, cfg=cfg)

    # Acceptance when only "proposal" is present
    only_proposal = dict(envelope)
    del only_proposal["proposed_record"]
    res_only_prop = confirm_external_project(only_proposal, confirmed=True, registry=registry, cfg=cfg)
    assert res_only_prop.created is True
    assert res_only_prop.record.slug == "office-printer"

    # Acceptance when only "proposed_record" is present (for a fresh project)
    prop2 = propose_external_project("Switch", slug="office-switch", registry=registry, cfg=cfg)
    envelope2 = prop2.to_dict()
    only_proposed_rec = dict(envelope2)
    del only_proposed_rec["proposal"]
    res_only_rec = confirm_external_project(only_proposed_rec, confirmed=True, registry=registry, cfg=cfg)
    assert res_only_rec.created is True
    assert res_only_rec.record.slug == "office-switch"


def test_error_messages_do_not_echo_untrusted_envelope_secrets(tmp_path: Path):
    """Untrusted envelope values (status, action, project_id) must not appear in error messages."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)
    registry = ProjectRegistry.empty()

    prop = propose_external_project("Gateway", slug="gw-1", registry=registry, cfg=cfg)
    secret = "SUPER_SECRET_TOKEN_XYZ_987654321"

    # 1. Secret in invalid status
    t_status = prop.to_dict()
    t_status["status"] = secret
    with pytest.raises(ValidationError) as exc_status:
        confirm_external_project(t_status, confirmed=True, registry=registry, cfg=cfg)
    assert secret not in str(exc_status.value)
    assert str(exc_status.value) == "Invalid proposal status. Expected 'candidate'."

    # 2. Secret in invalid action
    t_action = prop.to_dict()
    t_action["action"] = secret
    with pytest.raises(ValidationError) as exc_action:
        confirm_external_project(t_action, confirmed=True, registry=registry, cfg=cfg)
    assert secret not in str(exc_action.value)
    assert str(exc_action.value) == "Invalid proposal action. Expected 'propose_external'."

    # 3. Secret in mismatched project_id
    t_pid = prop.to_dict()
    t_pid["project_id"] = secret
    with pytest.raises(ValidationError) as exc_pid:
        confirm_external_project(t_pid, confirmed=True, registry=registry, cfg=cfg)
    assert secret not in str(exc_pid.value)
    assert str(exc_pid.value) == "Serialized proposal envelope project_id does not match record ID."


def test_custom_unicode_display_name_with_safe_derived_slug(tmp_path: Path):
    """Unicode display names produce safe slugs, and collisions are resolved cleanly."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)
    registry = ProjectRegistry.empty()

    # Unicode with mixed ASCII and emoji
    res1 = propose_external_project(
        "Домашний Proxmox VE 🚀",
        registry=registry,
        cfg=cfg,
    )
    assert res1.proposal.slug == "proxmox-ve"
    assert res1.proposal.display_name == "Домашний Proxmox VE 🚀"

    confirm1 = confirm_external_project(res1, confirmed=True, registry=registry, cfg=cfg)
    assert confirm1.created is True

    # Same display name resolves collision via stable ID suffix
    res2 = propose_external_project(
        "Домашний Proxmox VE 🚀",
        registry=registry,
        cfg=cfg,
    )
    expected_collision_slug = f"proxmox-ve-{res2.proposal.id[:8]}"
    assert res2.proposal.slug == expected_collision_slug

    confirm2 = confirm_external_project(res2, confirmed=True, registry=registry, cfg=cfg)
    assert confirm2.created is True
    assert len(registry.projects) == 2

    # Pure non-ASCII Unicode fallback
    res3 = propose_external_project(
        "Умный Дом",
        registry=registry,
        cfg=cfg,
    )
    assert res3.proposal.slug == "project"
    confirm3 = confirm_external_project(res3, confirmed=True, registry=registry, cfg=cfg)
    assert confirm3.created is True
    assert len(registry.projects) == 3


def test_invalid_name_and_metadata(tmp_path: Path):
    """Empty display name and invalid slugs/aliases are rejected."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)
    registry = ProjectRegistry.empty()

    # Empty or whitespace-only display name
    with pytest.raises(ValidationError, match="display_name"):
        propose_external_project("", registry=registry, cfg=cfg)
    with pytest.raises(ValidationError, match="display_name"):
        propose_external_project("   ", registry=registry, cfg=cfg)
    with pytest.raises(ValidationError, match="display_name"):
        propose_external_project(None, registry=registry, cfg=cfg)

    # Malformed explicit slug
    with pytest.raises(ValidationError, match="invalid"):
        propose_external_project("Device", slug="bad slug with spaces", registry=registry, cfg=cfg)
    with pytest.raises(ValidationError, match="invalid"):
        propose_external_project("Device", slug="device/path", registry=registry, cfg=cfg)

    # Malformed aliases
    with pytest.raises(ValidationError, match="Aliases must be a list"):
        propose_external_project("Device", aliases="not-a-list", registry=registry, cfg=cfg)
    with pytest.raises(ValidationError, match="Duplicate alias"):
        propose_external_project("Device", aliases=["dup", "dup"], registry=registry, cfg=cfg)
    with pytest.raises(ValidationError, match="cannot be identical to project slug"):
        propose_external_project("Device", slug="device", aliases=["device"], registry=registry, cfg=cfg)


def test_dry_persistence_rollback(tmp_path: Path):
    """dry_run=True mutates nothing, and failed save leaves caller registry unchanged."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)
    registry = ProjectRegistry.empty()

    prop = propose_external_project(
        "Dry Device",
        registry=registry,
        cfg=cfg,
    )

    # Dry run confirmation
    dry_res = confirm_external_project(
        prop,
        confirmed=True,
        registry=registry,
        cfg=cfg,
        dry_run=True,
    )
    assert dry_res.status == "candidate"
    assert dry_res.created is False
    assert dry_res.requires_confirmation is True
    assert not cfg.projects_yaml.exists()
    assert len(registry.projects) == 0

    # Failed save simulation (directory blocking projects.yaml file write)
    cfg.projects_yaml.mkdir(parents=True)
    with pytest.raises(ConfigError):
        confirm_external_project(
            prop,
            confirmed=True,
            registry=registry,
            cfg=cfg,
        )

    # Caller registry remains pristine
    assert len(registry.projects) == 0


def test_source_external_path_untouched(tmp_path: Path):
    """No filesystem objects or source directories are created for external subjects."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)
    registry = ProjectRegistry.empty()

    prop = propose_external_project(
        "Network Switch",
        registry=registry,
        cfg=cfg,
    )
    confirm_external_project(prop, confirmed=True, registry=registry, cfg=cfg)

    # Outside ptw_home, nothing was created
    assert set(tmp_path.iterdir()) == {ptw_home}

    # Inside ptw_home, only canonical projects_yaml exists
    assert cfg.projects_yaml.is_file()
    assert not (ptw_home / "projects").exists()


def test_resolution_by_slug_and_alias_after_creation(tmp_path: Path):
    """External projects can be resolved explicitly by ID, slug, and aliases."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)
    registry = ProjectRegistry.empty()

    prop = propose_external_project(
        "OpenWrt Router",
        slug="openwrt-main",
        aliases=["main-router", "core-gw"],
        registry=registry,
        cfg=cfg,
    )
    confirm_res = confirm_external_project(prop, confirmed=True, registry=registry, cfg=cfg)
    project_id = confirm_res.project_id

    # Resolve by slug
    res_slug = resolve_project(registry, explicit_project="openwrt-main")
    assert res_slug.status == "resolved"
    assert res_slug.project_id == project_id
    assert res_slug.project.kind == PROJECT_KIND_EXTERNAL
    assert res_slug.evidence == ["explicit"]

    # Resolve by alias
    res_alias1 = resolve_project(registry, explicit_project="main-router")
    assert res_alias1.status == "resolved"
    assert res_alias1.project_id == project_id

    res_alias2 = resolve_project(registry, explicit_project="core-gw")
    assert res_alias2.status == "resolved"
    assert res_alias2.project_id == project_id

    # Resolve by ID
    res_id = resolve_project(registry, explicit_project=project_id)
    assert res_id.status == "resolved"
    assert res_id.project_id == project_id


def test_external_registration_service(tmp_path: Path):
    """ExternalRegistrationService coordinates proposal and confirmation smoothly."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)
    registry = ProjectRegistry.empty()

    service = ExternalRegistrationService(cfg=cfg, registry=registry)
    candidate = service.propose("Managed Switch", slug="switch-managed", aliases=["sw-1"])
    assert candidate.status == "candidate"
    assert candidate.created is False

    result = service.confirm(candidate, confirmed=True)
    assert result.status == "resolved"
    assert result.created is True
    assert result.record.slug == "switch-managed"
    assert len(registry.projects) == 1
