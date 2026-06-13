"""Stage 3 AI-tooling classifier.

Turns the user's freeform tooling description ("Claude Code for dev, CodeRabbit for
review, Figma AI for design") into per-phase `AiToolingLevel`s the twins consume.

Two-step, mirroring the prefill/roster draft-agent pattern:
1. One forced-tool `call_structured` classifies what the model confidently recognizes
   and lists anything it doesn't in `unknown_tools` (those phases stay NONE).
2. Only if there are unknown tools AND docs-mcp-server is configured/reachable,
   research them via that self-hosted MCP server (client-side, streamable HTTP) and
   re-classify with the notes. When `docs_mcp_auto_scrape` is on (default), a tool
   that isn't in the docs-mcp index yet is SCRAPED (its latest docs indexed) before
   the estimate continues; otherwise the step only searches the existing index. When
   research is unavailable / times out, unknown tools simply stay NONE (the
   conservative choice — unverified tooling never inflates the AI reduction).

Every failure path degrades to an all-NONE classification, so the endpoint always
returns a valid result even without an API key or network.
"""

from __future__ import annotations

import asyncio
import logging

from pydantic import BaseModel, ConfigDict, Field

from config import get_settings
from models.project_schema import PhaseToolingLevels
from orchestrator.llm import call_structured, research_with_local_mcp
from orchestrator.nodes._twin_base import load_prompt

logger = logging.getLogger(__name__)

_MAX_UNKNOWN = 10

# Per-tool classification we want back from the research digest, regardless of mode.
_RESEARCH_TASK = (
    "Then, for each tool, state in one line: what it does, which SDLC phase it serves "
    "(discovery, ux_design, development, code_review, deployment, or qa_testing), and "
    "how autonomous it is (autocomplete, chat, or agentic). If a tool genuinely cannot "
    "be found or indexed, say so."
)

# Search-only research: just query whatever is already in the docs-mcp index.
_SEARCH_ONLY_SYSTEM = (
    "You research software-development AI tools. Use the available documentation-search "
    "tools (search_docs, list_libraries) to look up any tool you don't already know. "
    + _RESEARCH_TASK
)

# Scrape-then-search: if a tool isn't in the index yet, index its latest docs FIRST,
# then search. docs-mcp-server exposes: list_libraries, search_docs, find_version,
# scrape_docs, get_job_info, list_jobs, fetch_url.
_SCRAPE_THEN_SEARCH_SYSTEM = (
    "You research software-development AI tools using a self-hosted documentation index "
    "(docs-mcp-server) exposed as tools: list_libraries, search_docs, find_version, "
    "scrape_docs, get_job_info, list_jobs, fetch_url.\n\n"
    "For EACH tool named below, make sure its latest documentation is indexed BEFORE you "
    "answer:\n"
    "1. Check whether it is already indexed with list_libraries (and/or search_docs).\n"
    "2. If it is NOT indexed, find the tool's official documentation site (use fetch_url "
    "on the vendor site if you need to locate the docs URL), then call scrape_docs with "
    "that library name and docs URL to index the latest docs.\n"
    "3. scrape_docs starts an ASYNCHRONOUS indexing job — poll get_job_info (or list_jobs) "
    "until that job has completed or clearly failed. Do not answer until indexing is "
    "done.\n"
    "4. Once indexed, search_docs the library to ground your answer.\n\n"
    + _RESEARCH_TASK
)


class ClassifyToolingRequest(BaseModel):
    """Request body for POST /estimates/draft/classify-tooling."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(
        default="",
        max_length=2000,
        description="Freeform description of the team's AI development tooling.",
    )


class ToolingClassification(BaseModel):
    """Structured result of classifying a freeform AI-tooling description.

    Doubles as the classify-tooling endpoint's response model.
    """

    model_config = ConfigDict(extra="forbid")

    ai_tooling: PhaseToolingLevels = Field(default_factory=PhaseToolingLevels)
    unknown_tools: list[str] = Field(default_factory=list, max_length=_MAX_UNKNOWN)
    notes: str = Field(default="", max_length=400)


def _empty() -> ToolingClassification:
    """All-NONE classification — the safe default when there's nothing to classify."""
    return ToolingClassification(ai_tooling=PhaseToolingLevels())


async def _run_classifier(description: str, research_notes: str = "") -> ToolingClassification:
    """One forced-tool classification call. Raises on LLM failure (caller handles it)."""
    user = f"AI tooling description:\n\n{description.strip()}"
    if research_notes:
        user += f"\n\nResearch notes (use these to classify previously-unknown tools):\n{research_notes}"
    return await call_structured(
        system=load_prompt("tooling_classifier"),
        user=user,
        response_model=ToolingClassification,
        tool_name="classify_ai_tooling",
        model=get_settings().anthropic_model_tooling,
    )


def _docs_mcp_target() -> tuple[str, dict[str, str] | None] | None:
    """(url, headers) for the docs-mcp-server, or None when research is disabled."""
    settings = get_settings()
    url = settings.docs_mcp_url.strip()
    if not url:
        return None
    headers = (
        {"Authorization": f"Bearer {settings.docs_mcp_auth_token}"}
        if settings.docs_mcp_auth_token
        else None
    )
    return url, headers


async def _research_unknown_tools(names: list[str]) -> str:
    """Identify unknown tools via docs-mcp-server, indexing missing ones first.

    With ``docs_mcp_auto_scrape`` on (default), Claude is told to SCRAPE a tool's
    latest docs into the index when it isn't there yet, wait for the indexing job,
    then search — i.e. "update the docs before estimating". With it off, this only
    searches the existing index. Returns a prose digest, or "" if research is
    disabled/unavailable/times out — in which case the unknown tools stay NONE.
    """
    target = _docs_mcp_target()
    if not target:
        return ""
    url, headers = target
    settings = get_settings()
    joined = ", ".join(names)
    if settings.docs_mcp_auto_scrape:
        system = _SCRAPE_THEN_SEARCH_SYSTEM
        timeout = settings.docs_mcp_scrape_timeout_s
        user = f"Tools to research and (if not yet indexed) index, then research: {joined}"
    else:
        system = _SEARCH_ONLY_SYSTEM
        timeout = settings.docs_mcp_research_timeout_s
        user = f"Research these development tools: {joined}"
    try:
        notes = await asyncio.wait_for(
            research_with_local_mcp(
                system=system,
                user=user,
                mcp_url=url,
                headers=headers,
                model=settings.anthropic_model_tooling,
            ),
            timeout=timeout,
        )
        return notes.strip()
    except TimeoutError:
        logger.warning(
            "docs-mcp tool research timed out after %.0fs; unknown tools stay 'none'",
            timeout,
        )
        return ""
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "docs-mcp tool research failed (%s); unknown tools stay 'none'", exc
        )
        return ""


async def classify_ai_tooling(description: str) -> ToolingClassification:
    """Top-level entry point used by the HTTP endpoint.

    Always returns a valid classification: an all-NONE result on a blank description
    or any LLM failure, so the endpoint never surfaces an API-key / network error.
    """
    if not description.strip():
        return _empty()

    try:
        result = await _run_classifier(description)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "tooling classifier failed (%s); returning all-'none'. "
            "Set ANTHROPIC_API_KEY for real classification.",
            exc,
        )
        return _empty()

    if result.unknown_tools and get_settings().anthropic_api_key:
        logger.info("researching %d unknown tool(s): %s", len(result.unknown_tools), result.unknown_tools)
        notes = await _research_unknown_tools(result.unknown_tools)
        if notes:
            try:
                result = await _run_classifier(description, research_notes=notes)
            except Exception as exc:  # noqa: BLE001
                logger.warning("re-classification after research failed (%s); keeping first pass", exc)

    logger.info(
        "tooling classified: %s | unknown=%s",
        result.ai_tooling.model_dump(),
        result.unknown_tools,
    )
    return result
