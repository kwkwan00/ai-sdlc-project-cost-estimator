# QUICKSTART

A complete, step-by-step guide to setting up and running the **AI SDLC Project Cost Estimator** from a fresh clone. Follow it top to bottom — no step is optional unless explicitly marked **(optional)**.

There are two supported ways to run the project; pick one:

- **Path A — Local development** (recommended while coding): datastores run in Docker, the backend and frontend run on your host with hot-reload.
- **Path B — Full Docker stack**: everything (datastores **and** both apps) runs in Docker.

> For architecture and feature detail, see [`README.md`](./README.md); for contributor/agent guidance, see [`CLAUDE.md`](./CLAUDE.md). This file is only about getting it running.

---

## Table of contents

- [1. Prerequisites](#1-prerequisites)
- [2. Get the code](#2-get-the-code)
- [3. Configure environment (`.env`)](#3-configure-environment-env)
- [4. Path A — Local development](#4-path-a--local-development)
- [5. Path B — Full Docker stack](#5-path-b--full-docker-stack)
- [6. Verify it works (both flows)](#6-verify-it-works-both-flows)
- [7. (Optional) Tooling research via docs-mcp-server](#7-optional-tooling-research-via-docs-mcp-server)
- [8. Database migrations](#8-database-migrations)
- [9. Running the tests](#9-running-the-tests)
- [10. Ports reference](#10-ports-reference)
- [11. Command cheatsheet](#11-command-cheatsheet)
- [12. Stopping and cleaning up](#12-stopping-and-cleaning-up)
- [13. Troubleshooting](#13-troubleshooting)

---

## 1. Prerequisites

Install these on your machine first. Versions below are the minimums tested.

| Tool | Why | Install / check |
|---|---|---|
| **git** | Clone the repo | `git --version` |
| **Docker** + **Docker Compose v2** | Runs Neo4j, Postgres, Qdrant, docs-mcp-server (and, in Path B, the apps) | Docker Desktop (macOS/Windows) or Docker Engine (Linux). Verify Compose v2: `docker compose version` (note the **space**, not `docker-compose`). |
| **uv** | Python dependency + interpreter manager for the backend. It fetches Python 3.12 automatically — you do **not** need a system Python. | `curl -LsSf https://astral.sh/uv/install.sh \| sh` then `uv --version`. Docs: https://docs.astral.sh/uv/ |
| **Node.js 20+** (with npm) | Builds/runs the Next.js frontend (the Docker image uses Node 22) | `node --version` / `npm --version`. https://nodejs.org/ |
| **An Anthropic API key** | **Required** for real estimates (the six twins + WBS planner + support agents call Claude) | Get one at https://console.anthropic.com/. Key looks like `sk-ant-...`. |
| **An OpenAI API key** **(optional)** | Only needed for (a) docs-mcp-server scraping/search of unknown AI tools and (b) the optional eval harness LLM-as-judge | https://platform.openai.com/ |

**You do not need:** a system-installed Python, PostgreSQL, or Neo4j — Docker and `uv` provide them.

> The app **degrades gracefully**: it starts and serves requests even when Neo4j, Postgres, Qdrant, or the OpenAI key are absent — you just lose that layer's features (persistence, history, tool research). The only hard requirement for *real* (non-stub) estimates is `ANTHROPIC_API_KEY`.

---

## 2. Get the code

```bash
git clone <your-repo-url> ai-sdlc-project-cost-estimator
cd ai-sdlc-project-cost-estimator
```

All commands in this guide are run **from the repository root** unless stated otherwise.

---

## 3. Configure environment (`.env`)

The whole project is configured by **one `.env` file at the repository root**. The backend reads `../.env` (when launched from `backend/`) or `.env`, and `docker-compose.yml` reads the same file.

```bash
cp .env.example .env
```

Now open `.env` and set the values:

### Must set

- **`ANTHROPIC_API_KEY`** — replace `sk-ant-...` with your real key. Without it, the twins return deterministic *stub* estimates (useful for a no-cost smoke test, but not real numbers).

### Recommended (defaults already work for local Docker)

- **`NEO4J_PASSWORD`** and **`POSTGRES_PASSWORD`** ship as `changeme-please`. These same values are used by the Neo4j and Postgres containers, so the defaults work out-of-the-box for local development. Change them to anything you like — just keep the app value and the container value identical (they come from the same `.env`). If you blank `NEO4J_PASSWORD` or `POSTGRES_PASSWORD`, that persistence layer silently disables.

### Optional

- **`OPENAI_API_KEY`** — set it to enable docs-mcp-server tool research ([§7](#7-optional-tooling-research-via-docs-mcp-server)) and the eval judge. Leave blank otherwise (unknown AI tools then map to "none").
- **Model tiers** (`ANTHROPIC_MODEL`, `ANTHROPIC_MODEL_PREFILL`, `ANTHROPIC_MODEL_ROSTER`, `ANTHROPIC_MODEL_MERGE`, `ANTHROPIC_MODEL_TOOLING`, `ANTHROPIC_MODEL_WBS`) — sensible defaults are set; override only if you want different models.
- **`WBS_EFFORT_SCALE`** — global multiplier on the WBS bottom-up realism factor (default `1.0`).

Everything else in `.env.example` has working defaults and is documented inline in that file.

> **Never commit your `.env`** — it holds live secrets. It is already gitignored.

---

## 4. Path A — Local development

Datastores in Docker; backend + frontend on your host with hot-reload. This is the fastest inner loop.

### 4.1 Start the datastores only

> ⚠️ Do **not** use `make up` for this path. `make up` runs `docker compose up -d`, which also builds and starts the **dockerized** backend/frontend (they have no profile), and those would collide with your host apps on ports `8000`/`3000`. Start only the datastores:

```bash
docker compose up -d neo4j postgres qdrant docs-mcp-server
```

This brings up:

- **Neo4j** on `:7474` (browser) / `:7687` (Bolt)
- **Postgres** on `:5432`
- **Qdrant** on `:6333` / `:6334`
- **docs-mcp-server** on `:6280`

Check they're healthy:

```bash
docker compose ps
```

Wait until Neo4j and Postgres show `healthy` (a few seconds to ~30s on first run).

### 4.2 Install dependencies (one-time)

```bash
make install-be     # cd backend && uv sync   (fetches Python 3.12 + all backend deps)
make install-fe     # cd frontend && npm install
```

### 4.3 Run the backend (shell 1)

```bash
make be             # uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

On startup the backend automatically runs Alembic migrations against Postgres (because `POSTGRES_MIGRATE_ON_START=true`). Watch for the readiness line:

```
✓ Backend ready at http://0.0.0.0:8000
```

Sanity-check it from another terminal:

```bash
curl http://localhost:8000/health        # -> {"status":"ok","service":"ai-sdlc-estimator"}
```

Interactive API docs: http://localhost:8000/docs

### 4.4 Run the frontend (shell 2)

```bash
make fe             # next dev on :3000
```

Watch for:

```
✓ Frontend ready at http://localhost:3000
```

### 4.5 Open the app

Go to **http://localhost:3000**. Continue to [§6 Verify it works](#6-verify-it-works-both-flows).

### 4.6 (Optional) One-shot smoke test — no browser

Runs a single Pass-1 → resume-with-defaults → synthesis cycle end-to-end:

```bash
make smoke                                        # uses a fixture project description
# or skip the LLM call for parse_input (works with no API key):
cd backend && uv run python -m orchestrator.smoke --no-llm
```

---

## 5. Path B — Full Docker stack

Everything runs in Docker — no host Python/Node needed beyond Docker itself.

### 5.1 Build and start

```bash
docker compose up -d --build
```

This builds and starts the default services: **neo4j, postgres, qdrant, docs-mcp-server, estimator-backend, estimator-frontend**.

First build takes a few minutes. Then confirm everything is up:

```bash
docker compose ps
docker compose logs estimator-backend  | grep "Backend ready"
docker compose logs estimator-frontend | grep "Frontend ready"
```

### 5.2 Open the app

Go to **http://localhost:3000** (frontend) — it talks to the backend at **http://localhost:8000**.

> **Important about `NEXT_PUBLIC_API_URL`:** the frontend bakes this URL in at **build time** and it is called from your **browser**, so it must be `http://localhost:8000`, **not** the internal Docker hostname `http://estimator-backend:8000`. The default in `docker-compose.yml` is already correct for desktop use. If you change it, rebuild the frontend image.

### 5.3 Rebuild after code changes

```bash
docker compose up -d --build estimator-backend estimator-frontend
```

---

## 6. Verify it works (both flows)

The app ships two estimation flows. Try each from the landing page at http://localhost:3000.

### Quick Estimate (top-down, parametric)

1. Click **Quick Estimate** / "New estimate".
2. **Stage 1** — paste a project description (or pick an example, or upload a PDF/Word/`.txt`).
3. **Stage 2** — review the project context + the proposed team roster.
4. **Stage 3** — describe your AI tooling + pick a codebase context, then submit.
5. **Stage 4** — answer the clarifying questions (or skip with defaults).
6. **Stage 5** — the review page renders the dual-scenario (AI-assisted vs manual) estimate, cost table, timeline, confidence/fan charts, and team-scaling.

### WBS Estimate (bottom-up)

1. Click **WBS Estimate** on the landing page.
2. **Describe** the project (+ codebase + freeform AI tooling).
3. **Team** — the roster is proposed and tooling is classified, then it drafts the tree.
4. **Edit** — adjust the task tree (phase, role, 3-point hours per leaf), set the **Contingency reserve %** (default 30%), click **Re-evaluate** to preview, then **Submit** to open the shared review page.

You can leave a WBS draft and **resume** it later from `/wbs`, and **Duplicate** any draft or completed WBS estimate.

Past estimates appear on the landing dashboard and can be reopened.

---

## 7. (Optional) Tooling research via docs-mcp-server

The `docs-mcp-server` container (started by default on `:6280`) lets the Stage-3 tooling classifier research AI tools it doesn't recognize. For it to **return or index anything it must have an embeddings provider**:

- Set **`OPENAI_API_KEY`** in `.env` (the docs-mcp-server uses it for embeddings).
- `DOCS_MCP_AUTO_SCRAPE=true` (default) scrapes-then-indexes an unknown tool's docs before answering; set it `false` to only search the existing index.
- Without an embeddings key, unknown tools simply fall back to Claude's own knowledge / "none" — the estimate still completes.

This is purely an enrichment step; everything works without it.

---

## 8. Database migrations

Migrations run **automatically** on backend startup when `POSTGRES_MIGRATE_ON_START=true` (the default), so for normal use you don't need to do anything.

To run them manually (from `backend/`, host shell — works against the dockerized Postgres via the DSN in `.env`):

```bash
cd backend
uv run alembic upgrade head            # apply all migrations
uv run alembic downgrade -1            # roll back one
uv run alembic revision --autogenerate -m "describe change"   # create a new migration
```

Set `POSTGRES_MIGRATE_ON_START=false` if you prefer to run migrations out-of-band (e.g. in CI/CD).

---

## 9. Running the tests

### Backend (pytest, ruff, mypy)

```bash
cd backend
uv run pytest                       # full suite (~640 tests; in-memory SQLite — no live Postgres needed)
uv run pytest tests/test_api.py -q  # a single file
uv run pytest -k discovery -q       # by keyword
uv run ruff check .                 # lint
uv run mypy .                       # type-check
```

The Postgres-layer tests use an in-memory SQLite engine, so no running database is required for CI.

### Frontend (vitest, lint, type-check, build)

```bash
cd frontend
npm test                            # vitest run (one-shot)
npm run test:watch                  # watch mode
npm run lint                        # next lint
npm run type-check                  # tsc --noEmit
npm run build                       # production build (.next/standalone for Docker)
```

### (Optional) Eval harness

Grades twin outputs with deterministic rubrics + LLM-as-judge rubrics:

```bash
make evals                          # uses ANTHROPIC_MODEL_EVAL as judge
```

---

## 10. Ports reference

| Service | Host port(s) | URL / use |
|---|---|---|
| Frontend (Next.js) | `3000` | http://localhost:3000 |
| Backend (FastAPI) | `8000` | http://localhost:8000 · `/health` · `/docs` |
| Neo4j | `7474`, `7687` | Browser http://localhost:7474 · Bolt `7687` |
| Postgres | `5432` | `postgresql://estimator:…@localhost:5432/estimator` |
| Qdrant | `6333`, `6334` | REST `6333` · gRPC `6334` (scaffolded, unused in MVP) |
| docs-mcp-server | `6280` | MCP over HTTP at `/mcp` |

---

## 11. Command cheatsheet

Driven from the root `Makefile`:

```bash
make help          # list targets
make up            # docker compose up -d  (NOTE: starts the FULL stack incl. dockerized apps)
make down          # docker compose down
make ps            # container status
make logs          # tail container logs
make install-be    # cd backend && uv sync
make install-fe    # cd frontend && npm install
make be            # run backend (host) on :8000
make fe            # run frontend (host) on :3000
make smoke         # one Pass-1 cycle, no browser
make evals         # LLM-as-judge eval harness
make clean         # docker compose down -v  (removes named volumes)
```

Raw Docker Compose equivalents:

```bash
docker compose up -d neo4j postgres qdrant docs-mcp-server   # datastores only (Path A)
docker compose up -d --build                                 # full stack (Path B)
docker compose up -d --build estimator-backend estimator-frontend   # rebuild apps only
docker compose logs -f estimator-backend                     # follow one service
docker compose down                                          # stop everything
```

---

## 12. Stopping and cleaning up

```bash
make down            # stop containers (keeps data)
# or
docker compose down
```

For local dev (Path A), also stop the `make be` / `make fe` processes (Ctrl-C in their shells).

Full reset (⚠️ destructive):

```bash
make clean           # docker compose down -v  — removes named Docker volumes (e.g. Qdrant)
```

> **Note on data:** the stateful services (Neo4j and Postgres) **bind-mount under `./data/`** rather than using named volumes. `make clean` / `down -v` does **not** wipe `./data/`. For a truly fresh database, delete the relevant directory manually, e.g. `rm -rf ./data/neo4j ./data/postgres`.

---

## 13. Troubleshooting

- **`docker compose: command not found`** — you have the old standalone `docker-compose`. Install Docker Compose v2 (bundled with recent Docker Desktop / the `docker-compose-plugin`). All commands here use `docker compose` (with a space).
- **Port already in use (`3000`/`8000`/`5432`/`7687`/`6280`)** — another process (or a stray container) holds the port. Find it (`lsof -i :8000`) and stop it, or change the host-side port mapping in `docker-compose.yml`. A common cause in Path A is having run `make up` (which also starts the dockerized apps); use the datastores-only command from [§4.1](#41-start-the-datastores-only) instead, or `docker compose stop estimator-backend estimator-frontend`.
- **Backend logs "Neo4j connect failed; persistence disabled"** — `NEO4J_PASSWORD` is unset or Neo4j isn't up yet. The backend keeps working without it. Confirm `docker compose ps` shows Neo4j `healthy`.
- **Backend logs "Postgres disabled (no POSTGRES_DSN / POSTGRES_PASSWORD)"** — expected when neither is set. History + calibration no-op; the rest works. Set `POSTGRES_PASSWORD` (or `POSTGRES_DSN`) to enable.
- **Twin returns a *stub* estimate / very low confidence** — the LLM call failed, almost always a missing/incorrect `ANTHROPIC_API_KEY`. Set it and restart `make be` (or `docker compose restart estimator-backend`).
- **Neo4j fails to start with `JettyWebServer.loadStaticContent: Path is null`** — newer 5.x community images regress on arm64; the image is pinned to `neo4j:5.20-community` for this reason. Don't bump it without testing on arm64.
- **Neo4j / Docker "no space left on device"** — the Docker VM disk is full. Stateful data is bind-mounted under `./data/` (on the host, which has space); prune build cruft with `docker image prune -af && docker builder prune -f`.
- **Frontend can't reach the backend in Docker** — `NEXT_PUBLIC_API_URL` must be `http://localhost:8000` (browser-reachable), never the internal service name. It's build-time, so rebuild the frontend image after changing it.
- **Next.js build fails on `useSearchParams()`** — wrap the page in `<Suspense>` (existing pages show the pattern).
- **Backend logs "Alembic upgrade failed"** — startup logs it but doesn't crash. Run `cd backend && uv run alembic upgrade head` manually to see the error.
- **Tooling classifier maps every tool to "none"** — docs-mcp-server is unreachable/timed out, or has no `OPENAI_API_KEY` to embed/search with. This is the safe fallback; the estimate still completes. See [§7](#7-optional-tooling-research-via-docs-mcp-server).

---

Once it's running, see [`README.md`](./README.md) for what each part does and the design rationale.
