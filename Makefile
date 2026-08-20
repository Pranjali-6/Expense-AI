.DEFAULT_GOAL := help
SHELL := /bin/bash
COMPOSE := docker compose

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- lifecycle --

.PHONY: init
init: ## Create .env from the template and generate real secrets
	@# One shell for the whole recipe, via line continuations. Make runs each
	@# recipe *line* in its own shell, so the earlier form —
	@#   @if [ -f .env ]; then echo "not overwriting"; exit 0; fi
	@#   @cp .env.example .env
	@# — exited only that first shell and then cheerfully copied over the file
	@# anyway. It printed "not overwriting" while overwriting, which rotated
	@# POSTGRES_PASSWORD (breaking auth against an already-initialised
	@# database) and STORAGE_MASTER_KEK (changing every account fingerprint and
	@# making stored PDFs undecryptable). A guard that prints the right thing
	@# and does the wrong thing is worse than no guard.
	@if [ -f .env ]; then \
		echo ".env already exists — not overwriting."; \
		echo "To start over: rm .env && make init && make reset && make bootstrap"; \
	else \
		cp .env.example .env && \
		python3 -c "import re,secrets,pathlib; \
p=pathlib.Path('.env'); t=p.read_text(); \
t=re.sub(r'^SECRET_KEY=.*$$','SECRET_KEY='+secrets.token_hex(32),t,flags=re.M); \
t=re.sub(r'^STORAGE_MASTER_KEK=.*$$','STORAGE_MASTER_KEK='+secrets.token_hex(32),t,flags=re.M); \
t=re.sub(r'^POSTGRES_PASSWORD=.*$$','POSTGRES_PASSWORD='+secrets.token_urlsafe(24),t,flags=re.M); \
t=re.sub(r'^APP_DB_PASSWORD=.*$$','APP_DB_PASSWORD='+secrets.token_urlsafe(24),t,flags=re.M); \
t=re.sub(r'^MINIO_ROOT_PASSWORD=.*$$','MINIO_ROOT_PASSWORD='+secrets.token_urlsafe(24),t,flags=re.M); \
t=re.sub(r'^GRAFANA_ADMIN_PASSWORD=.*$$','GRAFANA_ADMIN_PASSWORD='+secrets.token_urlsafe(16),t,flags=re.M); \
p.write_text(t)" && \
		echo ".env created with generated secrets."; \
	fi

.PHONY: up
up: ## Build and start the stack
	$(COMPOSE) up -d --build

.PHONY: bootstrap
bootstrap: ## Full setup from scratch: start, migrate, seed with demo data
	$(MAKE) up
	@echo "waiting for the API to become healthy..."
	@until curl -fsS http://localhost/api/v1/health/live >/dev/null 2>&1; do sleep 3; done
	$(MAKE) migrate
	$(MAKE) seed-demo
	$(MAKE) demo-data
	@echo ""
	@echo "Ready. http://localhost  ·  demo@expense-ai.dev / DemoPassword123!"

.PHONY: down
down: ## Stop the stack (volumes preserved)
	$(COMPOSE) down

.PHONY: reset
reset: ## Stop the stack and DESTROY all data volumes
	$(COMPOSE) down -v

.PHONY: restart
restart: ## Restart application services
	$(COMPOSE) restart api worker-default worker-extract scheduler frontend

.PHONY: ps
ps: ## Show service status
	$(COMPOSE) ps

.PHONY: logs
logs: ## Tail all logs
	$(COMPOSE) logs -f --tail=100

.PHONY: logs-api
logs-api: ## Tail API logs
	$(COMPOSE) logs -f --tail=100 api

.PHONY: logs-worker
logs-worker: ## Tail worker logs
	$(COMPOSE) logs -f --tail=100 worker-extract worker-default

# ------------------------------------------------------------------- health --

.PHONY: health
health: ## Full dependency health report
	@curl -fsS http://localhost/api/v1/health | python3 -m json.tool || \
		echo "API is not reachable through nginx yet."

# ---------------------------------------------------------------- profiles --

.PHONY: up-observability
up-observability: ## Start with Prometheus, Grafana and Loki
	$(COMPOSE) --profile observability up -d

.PHONY: up-flower
up-flower: ## Start with the Flower queue dashboard
	$(COMPOSE) --profile debug up -d

# -------------------------------------------------------------------- shell --

.PHONY: shell-api
shell-api: ## Shell into the API container
	$(COMPOSE) exec api bash

.PHONY: shell-db
shell-db: ## psql into PostgreSQL as the owner role
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-expense_owner} -d $${POSTGRES_DB:-expense_ai}

# --------------------------------------------------- data / fixtures / tests --
# Targets below are implemented in the phase noted; they are declared now so
# the workflow is discoverable from `make help` from day one.

.PHONY: migrate
migrate: ## Apply database migrations
	$(COMPOSE) exec -w /app/backend api alembic upgrade head

.PHONY: migrate-check
migrate-check: ## Verify migrations match the models (no pending drift)
	$(COMPOSE) exec -w /app/backend api alembic check

.PHONY: migrate-history
migrate-history: ## Show the migration history
	$(COMPOSE) exec -w /app/backend api alembic history --verbose

.PHONY: seed
seed: ## Seed the category tree and merchant dictionary
	$(COMPOSE) exec api python -m app.db.seed

.PHONY: seed-demo
seed-demo: ## Seed reference data plus a fictional demo tenant
	$(COMPOSE) exec api python -m app.db.seed --demo

.PHONY: gen-fixtures
gen-fixtures: ## Generate synthetic statement PDFs + expected.json pairs
	$(COMPOSE) exec worker-extract python -m tools.statement_generator

.PHONY: demo-data
demo-data: ## Generate six months of demo statements and import them as the demo user
	$(COMPOSE) exec worker-extract python -m tools.statement_generator \
		--demo --output /app/tests/fixtures/demo
	$(COMPOSE) exec worker-extract python -m tools.demo_seed \
		--base-url http://nginx/api/v1 --directory /app/tests/fixtures/demo

.PHONY: accuracy
accuracy: ## Score extraction against the synthetic golden fixtures (phase gate)
	$(COMPOSE) exec worker-extract python -m tools.accuracy_harness --corpus synthetic

.PHONY: validate-real
validate-real: ## [P4.5] Score extraction against your own redacted statements
	$(COMPOSE) exec worker-extract python -m tools.accuracy_harness --corpus real

# Published on the host's loopback only. The review page embeds rendered images
# of a real bank statement, so it must not be reachable from the network.
GROUNDTRUTH_PORT ?= 8901

.PHONY: groundtruth
groundtruth: ## [P4.5] Review one real statement and write its expected.json (PDF=path)
	@test -n "$(PDF)" || { \
		echo "usage: make groundtruth PDF=tests/fixtures/real/axis-2025.pdf"; exit 2; }
	$(COMPOSE) run --rm -p 127.0.0.1:$(GROUNDTRUTH_PORT):$(GROUNDTRUTH_PORT) \
		-w /app worker-extract \
		python -m tools.corpus.groundtruth review "$(PDF)" --port $(GROUNDTRUTH_PORT)

# Run in worker-extract, not api: it is the only image carrying both the web
# stack (from base.txt) and the PDF/OCR stack, so it is the one place the whole
# suite can run. The api image deliberately has no PyMuPDF — an API container
# that could rasterise PDFs would be carrying an attack surface it never uses.
.PHONY: test
test: ## Run the backend test suite
	$(COMPOSE) exec -w /app worker-extract pytest -c /app/backend/pytest.ini tests/

.PHONY: test-security
test-security: ## Tenant isolation, RLS and schema invariants only
	$(COMPOSE) exec -w /app worker-extract pytest -c /app/backend/pytest.ini tests/security/ -v

.PHONY: test-parsers
test-parsers: ## Parser, extraction and accuracy-harness tests only
	$(COMPOSE) exec -w /app worker-extract pytest -c /app/backend/pytest.ini \
		tests/parsers/ tests/pipeline/ tests/accuracy/ -v

.PHONY: test-privacy
test-privacy: ## Privacy perimeter, assistant traceability and log leakage
	$(COMPOSE) exec -w /app worker-extract pytest -c /app/backend/pytest.ini \
		tests/privacy/ tests/assistant/ tests/security/test_log_leakage.py -v

.PHONY: typecheck
typecheck: ## Type-check the frontend
	$(COMPOSE) exec frontend npm run typecheck

.PHONY: lint
lint: ## Lint the Python source
	$(COMPOSE) exec -w /app worker-extract ruff check backend workers parsers tools tests

# --------------------------------------------------------------- operations --

.PHONY: retention
retention: ## Apply retention windows now, instead of waiting for 04:30
	$(COMPOSE) exec worker-default python -c \
		"from workers.tasks.maintenance import retention_sweep; print(retention_sweep.apply().get())"

.PHONY: reconcile-objects
reconcile-objects: ## Report stored objects with no statement row (never deletes)
	$(COMPOSE) exec worker-default python -c \
		"from workers.tasks.maintenance import reconcile_objects; print(reconcile_objects.apply().get())"

.PHONY: metrics
metrics: ## Show the platform gauges the way Prometheus scrapes them
	@curl -fsS http://localhost/api/v1/health/metrics | grep -E "^expense_(ledger|review|untrusted|celery)" || \
		echo "API is not reachable through nginx yet."
