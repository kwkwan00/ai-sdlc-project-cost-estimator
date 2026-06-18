"""AG-UI streaming endpoint for the team-roster proposal agent (Option B).

Instead of chaining the roster agent into the synchronous prefill response, the
frontend kicks off this AG-UI agent run on Stage 2 — so the page renders the
prefilled fields instantly and the proposed roster streams in a beat later.

Transport is the AG-UI event protocol over SSE:
    RUN_STARTED → STATE_SNAPSHOT(roster + plan + rationale) → RUN_FINISHED
    (or RUN_ERROR on failure, after which the frontend keeps the default roster).

The agent logic itself (`run_roster_agent` + `proposal_to_roster`) is unchanged
and shared — only the transport differs from a plain JSON endpoint.
"""

from __future__ import annotations

import logging

from ag_ui.core import (
    EventType,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StateSnapshotEvent,
)
from ag_ui.encoder import EventEncoder
from fastapi import Request
from fastapi.responses import StreamingResponse

from db.repositories import get_default_rates
from models.project_schema import Stage2Context
from roster_agent import proposal_to_roster, run_roster_agent

logger = logging.getLogger(__name__)


def _extract_inputs(input_data: RunAgentInput) -> tuple[Stage2Context, str]:
    """Pull the interpreted Stage 2 context + raw description from the run input.

    The frontend passes them via AG-UI ``forwardedProps``:
        {"stage2": <Stage2Context dict>, "raw_input": "<description>"}
    Falls back to an empty Stage2Context so the agent can still run if either is
    missing or malformed.
    """
    props = input_data.forwarded_props or {}
    raw_input = ""
    stage2_data = None
    if isinstance(props, dict):
        raw_input = str(props.get("raw_input") or "")
        stage2_data = props.get("stage2")
    try:
        stage2 = Stage2Context.model_validate(stage2_data) if stage2_data else Stage2Context()
    except Exception:  # noqa: BLE001 - bad client payload shouldn't 500 the run
        stage2 = Stage2Context()
    return stage2, raw_input


async def roster_agui_endpoint(
    input_data: RunAgentInput, request: Request
) -> StreamingResponse:
    """AG-UI agent-run endpoint: streams the proposed roster as protocol events."""
    encoder = EventEncoder(accept=request.headers.get("accept") or "text/event-stream")

    async def event_generator():
        yield encoder.encode(
            RunStartedEvent(
                type=EventType.RUN_STARTED,
                thread_id=input_data.thread_id,
                run_id=input_data.run_id,
            )
        )
        logger.info(
            "roster AG-UI run started (thread=%s run=%s)",
            input_data.thread_id,
            input_data.run_id,
        )
        try:
            stage2, raw_input = _extract_inputs(input_data)
            proposal = await run_roster_agent(stage2, raw_input)
            roster = proposal_to_roster(proposal, await get_default_rates())
            # Shared-state snapshot the UI binds to. `roster` matches the Stage 2
            # form shape ({"roles": [...]}) so the frontend applies it directly.
            snapshot = {
                "roster": roster.model_dump(mode="json"),
                "project_plan": [p.model_dump(mode="json") for p in proposal.project_plan],
                "staffing_rationale": proposal.staffing_rationale,
            }
            yield encoder.encode(
                StateSnapshotEvent(type=EventType.STATE_SNAPSHOT, snapshot=snapshot)
            )
            logger.info(
                "roster AG-UI run finished (%d role(s))", len(roster.roles)
            )
            yield encoder.encode(
                RunFinishedEvent(
                    type=EventType.RUN_FINISHED,
                    thread_id=input_data.thread_id,
                    run_id=input_data.run_id,
                )
            )
        except Exception:  # noqa: BLE001
            # Log the real error server-side only; never leak internal LLM/DB/MCP
            # exception details to the client.
            logger.exception("roster AG-UI run failed; emitting RUN_ERROR")
            yield encoder.encode(
                RunErrorEvent(type=EventType.RUN_ERROR, message="roster proposal failed")
            )

    return StreamingResponse(event_generator(), media_type=encoder.get_content_type())
