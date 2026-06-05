"""CLI smoke harness: run a single estimation pass against a fixture project.

Usage:
    uv run python -m orchestrator.smoke
    uv run python -m orchestrator.smoke --no-llm   # skip LLM parse_input, use defaults
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from langgraph.types import Command

from models.project_schema import Stage2Context, Stage3Maturity

HEALTHCARE_FIXTURE = """
We need to build a HIPAA-compliant patient portal for a regional clinic. Patients
should be able to view their lab results, schedule appointments, message their
provider, request prescription refills, and view billing. Clinic staff need an
admin view to manage appointment availability and review messages.

Estimated 25 screens covering 4 user roles (patient, provider, billing admin, scheduler).
Integrations: Epic EHR (FHIR), Stripe billing, Twilio SMS for reminders. The clinic
already uses Okta for SSO. They want responsive web (no mobile app initially).
""".strip()


async def main(*, use_llm: bool = True) -> None:
    from orchestrator.graph import build_graph

    estimate_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": estimate_id}}

    initial_state = {
        "estimate_id": estimate_id,
        "project_name": "Patient Portal (smoke)",
        "raw_input": HEALTHCARE_FIXTURE,
        "stage2": Stage2Context(industry="healthcare", target_timeline_weeks=20),
        "stage3": Stage3Maturity(),
        "parsed_context": {} if use_llm else {"industry_hint": "healthcare", "summary": "smoke"},
    }

    graph = build_graph()

    print(">>> Pass 1 — running...")
    result = await graph.ainvoke(initial_state, config=config)

    interrupts = result.get("__interrupt__")
    if interrupts is None:
        print("!! No interrupt — graph did not pause for clarifying questions")
        print(json.dumps(result, default=str, indent=2)[:2000])
        return

    print(f">>> Pass 1 complete. {len(result.get('clarifying_questions', []))} clarifying questions.")
    for q in result.get("clarifying_questions", []):
        print(f"  Q: {q.text}  (impact {q.impact_hours}h, default '{q.suggested_default}')")

    print("\n>>> Resuming with all defaults...")
    resume_payload = {"answers": {}, "skip_remaining": True}
    final = await graph.ainvoke(Command(resume=resume_payload), config=config)

    fe = final.get("final_estimate")
    if fe is None:
        print("!! No final estimate")
        return

    print("\n=== FINAL ESTIMATE ===")
    print(f"AI-assisted hours: {fe.total_ai_assisted_hours.pert_mean:.0f} (PERT)")
    print(f"Manual-only hours: {fe.total_manual_only_hours.pert_mean:.0f} (PERT)")
    print(f"AI hours saved:    {fe.ai_hours_saved_pert:.0f}")
    print(f"AI cost saved:     ${fe.ai_cost_saved_usd:,.0f}")
    print(f"Cost (AI):         ${fe.total_cost_ai_assisted_usd:,.0f}")
    print(f"Cost (manual):     ${fe.total_cost_manual_only_usd:,.0f}")
    print(f"Confidence:        {fe.confidence:.0%}")
    print(f"Duration:          {fe.duration_weeks_low:.0f}-{fe.duration_weeks_high:.0f} weeks")
    print(f"Headcount:         {fe.headcount_by_role}")
    print("\nPer-phase (PERT mid hours):")
    for p in fe.phases:
        print(f"  {p.phase.value:18s}  AI: {p.ai_assisted_hours.pert_mean:>6.0f}h    manual: {p.manual_only_hours.pert_mean:>6.0f}h    [{p.algorithm}]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip parse_input LLM call (uses stub parsed_context).",
    )
    args = parser.parse_args()
    asyncio.run(main(use_llm=not args.no_llm))
