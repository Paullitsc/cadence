.PHONY: install install-pip test lint fmt run review roster-sync roster-push clean

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

review:         ## Local CV review app (pick bullets, preview the page, submit to sheet)
	uv run python -m internship_pipeline.review

# networking_targets.yaml is git-ignored (real names, public repo), so your local
# copy is the master and CI reads it from the NETWORKING_TARGETS_YAML secret.
# After naming someone on the sheet's Networking tab: roster-sync, then roster-push.
roster-sync:    ## Pull people named on the sheet back into your local roster
	uv run python -m internship_pipeline.run_daily --stage networking

roster-push:    ## Upload the local roster to the Actions secret CI reads
	@test -s networking_targets.yaml || { echo "networking_targets.yaml missing/empty"; exit 1; }
	gh secret set NETWORKING_TARGETS_YAML --repo Paullitsc/cadence < networking_targets.yaml
	@echo "NETWORKING_TARGETS_YAML updated ($$(wc -c < networking_targets.yaml) bytes)"

clean:
	rm -rf .pytest_cache .ruff_cache build dist
