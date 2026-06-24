"""Load + validate a SOW template spec from ``sow/templates/<id>.yaml``.

Cached per template name (templates are static at runtime, like the agent prompts). On top
of Pydantic schema validation, this enforces the cross-file invariant that every
``source: estimate`` / ``hybrid`` section names a ``renderer`` that actually exists in
``sow.mapper.RENDERERS`` — so a typo in the YAML fails at load, not at request time.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import yaml

import config  # imported as a module so importlib.reload(config) in tests stays transparent

from .mapper import RENDERERS
from .models import SowTemplate

TEMPLATES_DIR = Path(__file__).parent / "templates"

DEFAULT_TEMPLATE = "default_sow"


class SowTemplateError(ValueError):
    """Raised when a template file is missing or fails schema/renderer validation."""


def _validate_renderers(template: SowTemplate) -> None:
    for section in template.sections:
        if section.source in ("estimate", "hybrid"):
            if not section.renderer:
                # hybrid may carry only static bullets, but an estimate section must render.
                if section.source == "estimate":
                    raise SowTemplateError(
                        f"section '{section.id}' is source=estimate but has no renderer"
                    )
                continue
            if section.renderer not in RENDERERS:
                raise SowTemplateError(
                    f"section '{section.id}' references unknown renderer "
                    f"'{section.renderer}' (known: {sorted(RENDERERS)})"
                )


@cache
def _parse_template(name: str) -> SowTemplate:
    """Read + validate ``templates/<name>.yaml`` into a ``SowTemplate`` (cached by name).

    Pure: depends only on the YAML file (static at runtime, like the agent prompts), so it is
    safe to cache for the process lifetime. The runtime-settings override is applied separately
    in ``load_sow_template`` — never frozen into this cache."""
    path = TEMPLATES_DIR / f"{name}.yaml"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SowTemplateError(f"SOW template '{name}' not found at {path}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise SowTemplateError(f"SOW template '{name}' is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise SowTemplateError(f"SOW template '{name}' must be a YAML mapping")
    try:
        template = SowTemplate.model_validate(data)
    except Exception as exc:  # pydantic ValidationError → friendly wrapper
        raise SowTemplateError(f"SOW template '{name}' failed validation: {exc}") from exc
    _validate_renderers(template)
    return template


def load_sow_template(name: str = DEFAULT_TEMPLATE) -> SowTemplate:
    """Load the validated template and apply the ``SOW_COMPANY_NAME`` deploy-time override.

    Dependency-injection point: ``SOW_COMPANY_NAME`` (env / settings) overrides the template's
    company so a deployment sets its brand without committing it to the repo. Empty → keep the
    template's ``branding.company``.

    The override is resolved **live** on every call (via the ``config`` module attribute, so an
    ``importlib.reload(config)`` is transparent) and applied to a **copy** — never mutated onto
    the shared cached object nor baked into the parse cache — so changing the setting at runtime
    is always reflected and concurrent callers can't see each other's override."""
    template = _parse_template(name)
    company = config.get_settings().sow_company_name.strip()
    if company and template.branding.company != company:
        template = template.model_copy(deep=True)
        template.branding.company = company
    return template


def clear_template_cache() -> None:
    """Drop the parsed-template cache (the only process-lifetime state here).

    Useful in tests after editing a YAML on disk or to force a clean re-parse. The
    ``SOW_COMPANY_NAME`` override is resolved live per call, so it is never cached and does not
    need clearing — refresh ``config.get_settings`` for that."""
    _parse_template.cache_clear()
