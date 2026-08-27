# gpodsync — development and operation commands.
#
# `make` on its own lists what is available, so this file is also the project's
# index of what can be done to it.

.DEFAULT_GOAL := help
.PHONY: help setup lock venv lint lint-fix format typecheck audit-self-test audit-history ci-local ci-lint image image-audit image-run secret-scan check-deploy test-acceptance-prebuilt \
        remote-status remote-backup deploy rollback remote-logs remote-trace-on remote-trace-off \
        test test-unit test-component test-acceptance audit check clean

PYTHON  ?= python3
VENV    ?= .venv
BIN     := $(VENV)/bin
UV      ?= uv

help:  ## Show this help
	@echo "gpodsync"
	@echo ""
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- Environment -------------------------------------------------------------

setup: venv  ## Install dev dependencies and activate the git hooks
	@git config core.hooksPath .githooks
	@chmod +x .githooks/* scripts/*.sh
	@echo "Hooks active. Deployment details go in .env.deploy (untracked)."

venv:  ## Create the virtualenv and install pinned dev dependencies
	@test -d $(VENV) || $(UV) venv $(VENV) --python 3.14
	@$(UV) pip install --python $(BIN)/python -r requirements-dev.txt

lock:  ## Recompile requirements*.txt from requirements*.in, with hashes
	$(UV) pip compile requirements.in --generate-hashes --python-version 3.14 -o requirements.txt
	$(UV) pip compile requirements-dev.in --generate-hashes --python-version 3.14 -o requirements-dev.txt

# --- Quality -----------------------------------------------------------------

lint:  ## Check formatting and lint rules
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

lint-fix:  ## Apply the fixable lint rules
	$(BIN)/ruff check --fix .

format:  ## Format the code
	$(BIN)/ruff format .

# The fake AntennaPod is included deliberately. It is not scaffolding — it is the
# thing that decides whether a green acceptance suite means anything, so it is
# held to the same standard as the server.
# Django's own opinion about the settings, with the three deliberate deviations
# silenced in settings.py alongside the reason for each. The key is generated
# rather than fixed because the check measures its length.
check-deploy:  ## Run Django's deployment checks
	@GPODSYNC_ALLOWED_HOSTS=gpodder.example.com \
	 GPODSYNC_DATA_DIR=/tmp/gpodsync-check \
	 GPODSYNC_SECRET_KEY="$$($(BIN)/python -c 'import secrets; print(secrets.token_urlsafe(64))')" \
	 $(BIN)/python manage.py check --deploy --fail-level WARNING

typecheck:  ## Check type annotations
	$(BIN)/mypy gpodsync tests/fake_antennapod

# --- Tests -------------------------------------------------------------------

# The pure core carries a hard 100% gate on lines and branches. It is reachable
# because the layer holds no Django and no I/O; the day it stops being reachable
# is the day logic has leaked out of the domain and into a view, which is the
# thing this gate is really guarding.
test-unit:  ## Run unit tests (pure logic, no database) with the 100% gate
	$(BIN)/pytest tests/unit -m unit \
		--cov=gpodsync.domain --cov-report=term-missing --cov-fail-under=100

test-component:  ## Run component tests (Django in-process)
	$(BIN)/pytest tests/component -m component \
		--cov=gpodsync.api --cov=gpodsync.models --cov-report=term-missing --cov-fail-under=90

# Measured by the scenario matrix, not by line coverage: gating these on a
# percentage rewards tests that touch code over tests that prove behaviour.
test-acceptance:  ## Run acceptance tests (fake AntennaPod vs. the container)
	$(BIN)/pytest tests/acceptance -m acceptance

# Same suite, against an image that is already present and must not be rebuilt —
# the release pipeline audits an image and then runs this against those bits.
test-acceptance-prebuilt:
	GPODSYNC_ACCEPTANCE_PREBUILT=1 $(BIN)/pytest tests/acceptance -m acceptance

test: test-unit test-component test-acceptance  ## Run every test layer

# --- Image -------------------------------------------------------------------

IMAGE ?= gpodsync:dev

image:  ## Build the container image
	docker build -t $(IMAGE) .

image-audit: image  ## Check the built image carries nothing private
	@scripts/audit.sh --tree --image $(IMAGE)

image-run: image  ## Run the image locally the way it is meant to be run
	docker compose up --build

# --- Privacy -----------------------------------------------------------------

# The self-test runs first, every time. This gate is the one thing protecting a
# public repository from a private string, and its first version reported matches
# and still exited 0 — a broken gate is indistinguishable from a passing one
# unless something deliberately breaks it.
audit: audit-self-test  ## Check that nothing private would reach a public artefact
	@scripts/audit.sh --tree

audit-self-test:  ## Prove the audit actually fails when it finds something
	@scripts/audit.sh --self-test

audit-history: audit-self-test  ## Same, over every commit on every ref. Before going public.
	@scripts/audit.sh --history

secret-scan:  ## Scan the history for credentials of any shape, not just ours
	@scripts/secret-scan.sh

# --- CI ---------------------------------------------------------------------

# Runs the real workflow file locally, so a pipeline is never debugged by pushing
# commits to watch it fail.
#
# The secret is exported and named, never passed as `--secret NAME=value`. An
# argument is visible in /proc/<pid>/cmdline to every user on the machine, so
# spelling it out on the command line would publish the private-string list to
# `ps` — the exact data the rest of this project exists to keep out of sight. It
# also mangled the value: the list is multi-line, and act's dotenv-style
# --secret-file is no better, so the environment is the only route that keeps the
# entries separate.
ci-local:  ## Run the CI workflow locally with act, before pushing
	@command -v act >/dev/null 2>&1 || { echo "act is not installed"; exit 1; }
	@set -a; if [ -f .env.deploy ]; then . ./.env.deploy; fi; set +a; \
	act push --workflows .github/workflows/ci.yml --secret AUDIT_FORBIDDEN_STRINGS

ci-lint:  ## Check the workflow files parse and their jobs resolve
	@command -v act >/dev/null 2>&1 || { echo "act is not installed"; exit 1; }
	act push --workflows .github/workflows --list

# --- Deployment --------------------------------------------------------------
#
# Every target here reads the host from .env.deploy, which is untracked. Nothing
# in this file names a server.
#
# The rule for all of them: back up before changing anything, and verify with a
# real request afterwards. A container that started is not the same as a service
# that answers.

REMOTE = set -a; . ./.env.deploy; set +a;

remote-status:  ## What is running on the server right now
	@$(REMOTE) \
	 container=$$(ssh -o BatchMode=yes "$$DEPLOY_HOST" 'docker ps -a --filter name=gpodsync --format "{{.Status}} ({{.Image}})"' 2>/dev/null); \
	 printf 'container: %s\n' "$${container:-not deployed}"; \
	 code=$$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$$PRODUCTION_URL/healthz/" 2>/dev/null); \
	 case "$$code" in 200) note="answering";; 000|"") note="no route yet";; *) note="unexpected";; esac; \
	 printf 'https:     %s (%s)\n' "$${code:-000}" "$$note"

remote-backup:  ## Copy the database out, safely, and keep it here too
	@$(REMOTE) scripts/deploy.sh backup

deploy:  ## Back up, pull, restart and verify. Usage: make deploy TAG=0.1.0
	@$(REMOTE) GPODSYNC_TAG="$(TAG)" scripts/deploy.sh deploy

rollback:  ## Point at a different tag and restart. Usage: make rollback TAG=1.2.3
	@$(REMOTE) GPODSYNC_TAG="$(TAG)" scripts/deploy.sh rollback

remote-logs:  ## Follow the server's logs
	@$(REMOTE) ssh "$$DEPLOY_HOST" "cd $$DEPLOY_DIR && docker compose -f compose.production.yaml logs -f --tail=100"

remote-trace-on:  ## Turn request tracing on, and prove it took effect
	@$(REMOTE) scripts/deploy.sh trace on

remote-trace-off:  ## Turn it off again, and prove that took effect too
	@$(REMOTE) scripts/deploy.sh trace off

# --- Gate --------------------------------------------------------------------

# Mirrors ci.yml exactly, and grows with it as each test layer arrives.
check: audit secret-scan lint typecheck check-deploy test-unit test-component  ## Everything CI runs

clean:  ## Remove caches and build artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
