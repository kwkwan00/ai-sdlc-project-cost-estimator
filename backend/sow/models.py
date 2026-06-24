"""Pydantic schemas for the SOW feature.

Two groups:

* **Template schema** (``SowTemplate`` and friends) — the shape of
  ``sow/templates/*.yaml``. ``extra="forbid"`` so a typo in the config fails loudly at
  load time rather than silently dropping a field.
* **Runtime schema** (``SowDocument`` and friends) — the resolved document that crosses the
  wire: produced by ``POST /estimates/{id}/sow`` and posted back (after the user's edits) to
  ``POST /estimates/{id}/sow/docx``. Lenient on extra keys so a frontend round-trip can't
  break on an added field.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from models.twin_outputs import LlmUsage

# Section classification axes (see the package docstring).
SectionSource = Literal["boilerplate", "llm", "estimate", "hybrid"]
SectionStyle = Literal["paragraph", "bullets", "table", "signature_block", "cover"]

# Both cost scenarios the estimate carries; the fee table / resource summary follow this.
Scenario = Literal["ai_assisted", "manual_only"]


# --------------------------------------------------------------------------------------
# Template schema (sow/templates/*.yaml)
# --------------------------------------------------------------------------------------
class SowSignatory(BaseModel):
    """One party's signature column in a ``signature_block`` section."""

    model_config = ConfigDict(extra="forbid")
    party: str = Field(min_length=1, max_length=120)
    fields: list[str] = Field(default_factory=list)  # e.g. ["Signature", "Name", "Title", "Date"]


class SowTableSpec(BaseModel):
    """A static (boilerplate) table — e.g. the Project Representatives block."""

    model_config = ConfigDict(extra="forbid")
    columns: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)


class SowPlaceholderSpec(BaseModel):
    """A bracketed token the document leaves for the user (or the agent) to fill.

    ``fact_key`` says where a value comes from when one is available:
    a ``SowClientFacts`` field name (agent-extracted) or an ``estimate.*`` key
    (deterministically derived from the envelope — total investment, duration, year).
    Empty ``fact_key`` ⇒ always a manual placeholder.
    """

    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=64)  # "[CLIENT NAME]"
    label: str = Field(default="", max_length=120)
    fact_key: str = Field(default="", max_length=64)


class SowSectionSpec(BaseModel):
    """One ordered section of the template.

    Which fields matter depends on ``source``:
    * ``boilerplate`` → ``text`` / ``bullets`` / ``table`` / ``signatories`` (static).
    * ``llm`` → ``guidance`` + ``max_chars`` (prose the agent writes).
    * ``estimate`` → ``renderer`` (a key in ``sow.mapper.RENDERERS``).
    * ``hybrid`` → static ``bullets`` PLUS the ``renderer``'s estimate-derived items.
    """

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=64)
    heading: str = Field(default="", max_length=200)
    source: SectionSource
    style: SectionStyle = "paragraph"

    # boilerplate
    text: str = Field(default="", max_length=8000)
    bullets: list[str] = Field(default_factory=list)
    table: SowTableSpec | None = None
    signatories: list[SowSignatory] = Field(default_factory=list)

    # llm
    guidance: str = Field(default="", max_length=2000)
    max_chars: int = Field(default=4000, ge=1, le=20000)
    # Deterministic fallback prose used when the LLM is unavailable (no API key / error).
    # `fallback` is a template with `{name}` / `{phase_list}` substitution tokens; for a
    # `style: bullets` section, `fallback_item` is a per-phase template (`{label}`) joined into
    # the bullet list, and `fallback` is the default when the estimate has no phases.
    fallback: str = Field(default="", max_length=4000)
    fallback_item: str = Field(default="", max_length=500)

    # estimate / hybrid
    renderer: str = Field(default="", max_length=64)


class SowBranding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # The delivering firm's name — the SINGLE source, injected from the template YAML. It is
    # required (no code default) so the company is never hardcoded in code; every other
    # reference uses the [COMPANY] token, resolved to this at build time.
    company: str = Field(min_length=1, max_length=120)
    tagline: str = ""
    commitment: str = ""  # e.g. "At [COMPANY], Your Success is our success. ..."
    confidential_footer: str = ""


class SowStyle(BaseModel):
    """Document-level look, consumed by the python-docx renderer."""

    model_config = ConfigDict(extra="forbid")
    font: str = "Calibri"
    font_size_pt: float = Field(default=10.0, gt=0)
    heading_color: str = "1F3864"  # hex RGB, navy
    margin_inch: float = Field(default=1.0, gt=0)


class SowTemplate(BaseModel):
    """The full template spec loaded from one ``sow/templates/<id>.yaml`` file."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    title: str = Field(default="STATEMENT OF WORK", max_length=200)
    # Required: a template must declare its branding (company name is sourced from here).
    branding: SowBranding
    style: SowStyle = Field(default_factory=SowStyle)
    placeholders: list[SowPlaceholderSpec] = Field(default_factory=list)
    sections: list[SowSectionSpec] = Field(min_length=1)


# --------------------------------------------------------------------------------------
# Runtime schema (the resolved document on the wire)
# --------------------------------------------------------------------------------------
class SowClientFacts(BaseModel):
    """Client-specific facts the agent extracts from the brief/estimate — or null.

    Every field is optional: the agent populates one ONLY when the value is explicitly
    present in the provided context, otherwise it stays ``None`` and the matching token is
    left as a literal placeholder. The agent never invents these. ``extra="forbid"`` so a
    stray field triggers ``call_structured``'s one-shot corrective retry (twin convention).
    """

    model_config = ConfigDict(extra="forbid")
    client_name: str | None = Field(default=None, max_length=200)
    sow_number: str | None = Field(default=None, max_length=64)
    msa_date: str | None = Field(default=None, max_length=64)
    effective_date: str | None = Field(default=None, max_length=64)
    client_representative: str | None = Field(default=None, max_length=200)
    provider_representative: str | None = Field(default=None, max_length=200)


class SowTable(BaseModel):
    columns: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)


class SowSectionContent(BaseModel):
    """A fully-resolved section: concrete content the preview renders and the docx emits.

    ``kind`` mirrors the template ``style``; the renderer/preview dispatch on it. ``editable``
    is a UI hint — prose/bullets are editable, tables/signature/cover are read-only.
    """

    id: str
    heading: str = ""
    kind: SectionStyle
    text: str = ""
    bullets: list[str] = Field(default_factory=list)
    table: SowTable | None = None
    signatories: list[SowSignatory] = Field(default_factory=list)
    editable: bool = True


class SowDocument(BaseModel):
    """The resolved SOW. Returned by generate, posted back (edited) to render the docx."""

    estimate_id: str
    template_id: str
    title: str
    project_name: str = ""
    scenario: Scenario = "ai_assisted"
    sections: list[SowSectionContent] = Field(default_factory=list)
    # Tokens still unresolved after the client-fact + estimate substitution pass — the UI
    # flags these as "fill in Word".
    placeholders: list[str] = Field(default_factory=list)


class SowGenerateResponse(BaseModel):
    """Response of ``POST /estimates/{id}/sow`` — the document + its generation meta-cost."""

    document: SowDocument
    llm_usage: LlmUsage = Field(default_factory=LlmUsage)


class SowDocxRequest(BaseModel):
    """Body of ``POST /estimates/{id}/sow/docx`` — the (possibly edited) document to render."""

    document: SowDocument
