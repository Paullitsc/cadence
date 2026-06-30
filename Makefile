.PHONY: install install-pip test lint fmt run clean

install:        ## Create venv + install dev deps (uv, recommended)
	uv sync --extra dev

install-pip:    ## Fallback: venv + pip
	python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

test:
	uv run pytest

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

run:
	uv run python -m internship_pipeline.run_daily

clean:
	rm -rf .pytest_cache .ruff_cache build dist
