"""Project workspace initialization and template application."""

from datetime import datetime, timezone
from pathlib import Path
import yaml

from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import SCHEMA_VERSION
from personal_tideway.exceptions import ValidationError
from personal_tideway.utils import atomic_write_text


def find_template(templates_dir: Path, template_name: str) -> Path | None:
    """Find template file by exact name or with .yaml/.yml extension."""
    candidates = [
        templates_dir / template_name,
        templates_dir / f"{template_name}.yaml",
        templates_dir / f"{template_name}.yml",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def init_project(
    cfg: PersonalTidewayConfig,
    target_path: Path | None = None,
    template_name: str | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> Path:
    """Initialize a .personal-tideway.yaml project manifest at the target directory."""
    project_dir = Path(target_path or ".").resolve()
    manifest_file = project_dir / ".personal-tideway.yaml"

    if manifest_file.exists() and not force:
        raise ValidationError(
            f"Project manifest already exists at {manifest_file}. Use --force to overwrite."
        )

    content: str
    if template_name:
        tpl_path = find_template(cfg.templates_dir, template_name)
        if not tpl_path:
            raise ValidationError(
                f"Template '{template_name}' not found in templates directory ({cfg.templates_dir})."
            )
        raw = tpl_path.read_text(encoding="utf-8")
        content = raw.replace("{{project_name}}", project_dir.name)
    else:
        # Check if default template exists in templates
        default_tpl = find_template(cfg.templates_dir, "default")
        if default_tpl:
            raw = default_tpl.read_text(encoding="utf-8")
            content = raw.replace("{{project_name}}", project_dir.name)
        else:
            default_data = {
                "version": SCHEMA_VERSION,
                "project": {
                    "name": project_dir.name,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            }
            content = yaml.safe_dump(default_data, sort_keys=False)

    if not dry_run:
        project_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(manifest_file, content)

    return manifest_file
