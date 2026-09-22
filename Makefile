# zombiescan -- developer and operator entry points.
#
# Everything runs through `uv run`, so `make sync` is the only setup step.
# Variables you are likely to want:
#
#   SUBS="--subscription <id> --subscription <id>"
#   ARGS="--min-cost 5" extra flags for scan/plan targets
#
# NOTE: `make clean` removes *local* files (caches, reports). It has nothing to
# do with `zombiescan clean`, which deletes Azure resources -- that is
# `make plan` (dry run) and, deliberately, a hand-typed command for the real
# thing.

UV ?= uv
RUN := $(UV) run

SUBS ?=
ARGS ?=

JSON ?= findings.json
SCRIPT ?= cleanup.sh
HTML ?= report.html

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@echo "zombiescan -- make targets"
	@echo
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk -F':.*?## ' '{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "Variables: SUBS='--subscription <id>' ARGS='--min-cost 5'"

# --- setup -----------------------------------------------------------------

.PHONY: sync
sync: ## Install dependencies (including dev) into .venv
	$(UV) sync

# --- running the scanner ---------------------------------------------------

.PHONY: checks
checks: ## List every zombie check the engine knows about
	$(RUN) zombiescan checks

.PHONY: providers
providers: ## List the Azure resource providers the checks read
	$(RUN) zombiescan providers

.PHONY: scan
scan: ## Scan (read-only); defaults to the subscription az is set to
	$(RUN) zombiescan scan $(SUBS) $(ARGS)

.PHONY: scan-all
scan-all: ## Scan every enabled subscription these credentials can see (read-only)
	$(RUN) zombiescan scan --all-subscriptions $(ARGS)

.PHONY: report
report: ## Scan and write findings.json, cleanup.sh and report.html
	$(RUN) zombiescan scan $(SUBS) \
		--json $(JSON) --script $(SCRIPT) --html $(HTML) $(ARGS)

.PHONY: plan
plan: ## Dry-run `zombiescan clean`: show what it would delete, change nothing
	$(RUN) zombiescan clean $(SUBS) $(ARGS)

# --- development -----------------------------------------------------------

.PHONY: test
test: ## Run the offline test suite (fixtures, no credentials)
	$(RUN) pytest

.PHONY: test-live
test-live: ## Live smoke test: one real scan, plus the api-version and --yes checks
	ZOMBIESCAN_LIVE=1 $(RUN) pytest -m live

.PHONY: fmt
fmt: ## Format and autofix with ruff
	$(RUN) ruff format .
	$(RUN) ruff check --fix .

.PHONY: lint
lint: ## Check formatting and lints without writing (what CI runs)
	$(RUN) ruff check .
	$(RUN) ruff format --check .

.PHONY: ci
ci: lint test ## Everything CI runs: lint, format check, offline tests

# --- pricing ---------------------------------------------------------------

.PHONY: pricing
pricing: ## Refresh the bundled price table from the Azure Retail Prices API
	$(RUN) python -m zombiescan.pricing.refresh

# --- housekeeping ----------------------------------------------------------

.PHONY: clean
clean: ## Remove local caches, build output and generated reports
	rm -rf dist build .pytest_cache .ruff_cache
	rm -f $(JSON) $(SCRIPT) $(HTML) remediation.sh
	find src tests -name '__pycache__' -type d -prune -exec rm -rf {} +
