# Developer and lab commands. Use `make help` to list them.
# On Windows without make, run the underlying commands shown in the README.

PY      ?= python
COMPOSE  = docker compose -f lab/docker-compose.yml
LAB_PW  ?= lab-only-password
# The lab password is a throwaway; real deployments pass PGSENTRY_DB_PASSWORD(_FILE) themselves.
LAB_ENV  = PGSENTRY_DB_PASSWORD=$(LAB_PW)

.DEFAULT_GOAL := help

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-22s %s\n", $$1, $$2}'

install: ## Install in editable mode with dev tools
	$(PY) -m pip install -e ".[dev]"

lint: ## ruff lint + format check
	ruff check .
	ruff format --check .

typecheck: ## mypy
	mypy

test: ## pytest
	pytest

check: lint typecheck test ## Everything CI runs for Python

lab-up: ## Start primary, replica, Prometheus, Grafana (waits until healthy)
	$(COMPOSE) up -d --build --wait
	@echo "Grafana http://127.0.0.1:3000 (admin / $(LAB_PW))   Prometheus http://127.0.0.1:9090"

lab-down: ## Stop the lab and delete its data
	$(COMPOSE) down -v

lab-check: ## Run pgsentry against the lab primary and the lab replica
	-$(LAB_ENV) pgsentry -c lab/pgsentry-primary.yaml db check
	-$(LAB_ENV) pgsentry -c lab/pgsentry-replica.yaml db check

lab-load: ## Write 200k rows on the primary (generates WAL)
	$(COMPOSE) exec primary psql -U postgres -c "INSERT INTO lab_events (payload) SELECT repeat('x', 200) FROM generate_series(1, 200000)"

lab-backup: ## Record a successful backup (touches lab/state/last_backup)
	date -u +%Y-%m-%dT%H:%M:%SZ > lab/state/last_backup

lab-pause-replay: ## Failure drill: pause WAL replay on the replica
	$(COMPOSE) exec replica psql -U postgres -c "SELECT pg_wal_replay_pause()"

lab-resume-replay: ## Undo lab-pause-replay
	$(COMPOSE) exec replica psql -U postgres -c "SELECT pg_wal_replay_resume()"

lab-stop-replica: ## Failure drill: stop the replica container
	$(COMPOSE) stop replica

lab-start-replica: ## Undo lab-stop-replica
	$(COMPOSE) start replica

.PHONY: help install lint typecheck test check lab-up lab-down lab-check lab-load lab-backup \
        lab-pause-replay lab-resume-replay lab-stop-replica lab-start-replica
