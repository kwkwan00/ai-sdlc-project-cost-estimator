"""Pure-ASGI HTTP request logging middleware.

Logs one line per HTTP request (method, path, status, latency) through the
central logging config. Implemented as raw ASGI middleware rather than
Starlette's ``BaseHTTPMiddleware`` on purpose: BaseHTTPMiddleware buffers the
response body and breaks streaming / SSE responses, which would break the SSE
event stream (`/estimates/{id}/stream`) and the AG-UI roster endpoint
(`/estimates/draft/roster/agui`). This middleware only wraps ``send`` to read the
response status off the ``http.response.start`` message — it never touches the
body, so streaming is unaffected.
"""

from __future__ import annotations

import logging
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)


class RequestLoggingMiddleware:
    """Log every HTTP request: ``http <METHOD> <path> → <status> (<ms>)``.

    Fires after the response completes (for streaming responses, that's when the
    stream closes — so the latency reflects the full request duration).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status = 0

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path = scope.get("path", "?")
            # `/health` liveness polling (→ 2xx/3xx) is pure noise at INFO — log it at DEBUG. A
            # FAILING health check (≥ 400) still surfaces at INFO so real problems aren't hidden.
            level = (
                logging.DEBUG
                if path == "/health" and 200 <= (status or 0) < 400
                else logging.INFO
            )
            logger.log(
                level,
                "http %s %s → %s (%dms)",
                scope.get("method", "?"),
                path,
                status or "?",
                int((time.perf_counter() - started) * 1000),
            )
