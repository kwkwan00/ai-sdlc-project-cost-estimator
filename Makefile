.PHONY: help up down logs ps be fe smoke evals clean install-be install-fe

help:
	@echo "Targets:"
	@echo "  make up         - start Neo4j + Qdrant containers"
	@echo "  make down       - stop containers"
	@echo "  make logs       - tail container logs"
	@echo "  make ps         - show container status"
	@echo "  make install-be - install backend python deps via uv"
	@echo "  make install-fe - install frontend node deps"
	@echo "  make be         - run FastAPI backend on :8000"
	@echo "  make fe         - run Next.js frontend on :3000"
	@echo "  make smoke      - run backend smoke test (one Pass-1 cycle)"
	@echo "  make evals      - run the LLM-as-judge evals harness"
	@echo "  make clean      - remove containers + volumes"

up:
	docker compose up -d
	@echo "Waiting for services to be healthy..."
	@docker compose ps

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

ps:
	docker compose ps

install-be:
	cd backend && uv sync

install-fe:
	cd frontend && npm install

be:
	cd backend && uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000

fe:
	cd frontend && npm run dev

smoke:
	cd backend && uv run python -m orchestrator.smoke

evals:
	cd backend && uv run python -m evals.run

clean:
	docker compose down -v
