"""Non-twin LLM support agents — the lightweight, pinned-model agents around the estimation
twins. Each follows the same shape: ``load_prompt`` (from ``orchestrator.prompts``) +
``call_structured`` forced-tool-use against a Pydantic response model, pinned to its own
``anthropic_model_*`` setting, with a deterministic fallback so the HTTP layer never surfaces
an LLM/network error.

Members:
* ``prefill`` — normalize raw Stage-1 text into Stage-2 fields (``prefill_stage2_from_raw``).
* ``roster_agent`` / ``roster_agui`` — propose a team roster (``run_roster_agent``) + its
  AG-UI streaming endpoint (``roster_agui_endpoint``).
* ``tooling_classifier`` — classify the freeform AI-tooling description into per-phase levels
  (``classify_ai_tooling``), with docs-MCP research behind an SSRF guard.
* ``wbs_agent`` — draft the bottom-up WBS task tree (``generate_wbs_tree``).

(The clarifying-question consolidator ``merge_pass1._consolidate_semantically`` shares the
same shape but lives under ``orchestrator/nodes`` because it runs inside the graph.)

Import the concrete entry points directly (``from agents.prefill import ...``); this package
deliberately avoids eager re-exports to keep import-time coupling low.
"""
