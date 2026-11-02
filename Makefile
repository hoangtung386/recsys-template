.DEFAULT_GOAL := help
.PHONY: help setup lint format typecheck test test-fast bench schemas schemas-check check train serve loop-demo clean

APP ?= demo

help:  ## Show this help
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-12s %s\n", $$1, $$2}'

setup:  ## Create .venv from pyproject.toml / uv.lock (all roles + dev tools)
	uv sync --all-extras

lint:  ## PEP 8 / PEP 257 / bugbear / bandit checks
	uv run ruff check .
	uv run ruff format --check src tests benchmarks

format:  ## Auto-format and apply safe lint fixes
	uv run ruff check --fix src tests benchmarks
	uv run ruff format src tests benchmarks

typecheck:  ## mypy --strict
	uv run mypy

test:  ## Full test suite with coverage
	uv run pytest --cov

test-fast:  ## Skip the tests that train a model
	uv run pytest -m "not slow"

bench:  ## Engine latency guard (p95 must stay under 5 ms)
	uv run python benchmarks/engine_bench.py --max-p95-ms 5

schemas:  ## Regenerate the committed JSON Schemas
	uv run python -c "import json; from recsys.engine import EngineConfig; print(json.dumps(EngineConfig.json_schema(), indent=2, sort_keys=True))" > schemas/engine-config.schema.json

schemas-check: schemas  ## Fail if the committed schemas are stale
	@test -z "$$(git status --porcelain schemas 2>/dev/null)" || (echo "schemas are stale: run make schemas and commit" && exit 1)

check: lint typecheck test bench  ## Everything CI runs

train:  ## Train, gate and register a model on synthetic data
	uv run rec-train --app $(APP)

serve:  ## Serve $(APP) on :8080 (promote a model first: rec-admin promote)
	uv run rec-serve --app $(APP) --port 8080

clean:  ## Remove caches and local artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml htmlcov artifacts
