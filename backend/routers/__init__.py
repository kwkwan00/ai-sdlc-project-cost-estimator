"""HTTP routers for the orchestrator backend.

Each module groups one concern's endpoints into an `APIRouter` that `main.py`
includes. The routers hold only the HTTP surface; run orchestration, the SSE broker,
and the in-memory registries live in `runtime.py`.
"""
