# syntax=docker/dockerfile:1.7
#
# Multi-stage build for the estimator backend.
#   builder: uv-driven dependency install into a venv at /app/.venv
#   runtime: slim Python with the venv + source only, runs as non-root `app`
#
# Build context: ./backend  (configured in docker-compose.yml)

FROM python:3.12-slim AS builder

# Grab the uv binary from its official distroless image — avoids running pip
# bootstrap and gives us a reproducible uv version.
COPY --from=ghcr.io/astral-sh/uv:0.5.13 /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install deps first (cached layer) — only re-runs when pyproject/uv.lock change.
COPY pyproject.toml ./
COPY uv.lock* ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --no-install-project

# Now copy the rest of the source. The previous layer is unchanged when only
# .py files edit, keeping rebuilds fast.
COPY . .


FROM python:3.12-slim AS runtime

# Non-root runtime user.
RUN groupadd --system app && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

# Copy the prepared app from the builder stage (includes .venv + source).
COPY --from=builder --chown=app:app /app /app

USER app

EXPOSE 8000

# Container-native healthcheck so the dependency graph in docker-compose can
# wait for the API to actually be answering.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=6 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3); sys.exit(0)" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
