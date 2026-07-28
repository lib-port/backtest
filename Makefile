.PHONY: install install-service format lint type test test-cov check package run sweep smoke migrate migration-check

PYTHON ?= .venv/bin/python

install:
	$(PYTHON) -m pip install -U pip
	$(PYTHON) -m pip install -e ".[dev,postgres,service]"

install-service: install

format:
	$(PYTHON) -m ruff format .
	$(PYTHON) -m ruff check --fix .

lint:
	$(PYTHON) -m ruff format --check .
	$(PYTHON) -m ruff check .

type:
	$(PYTHON) -m mypy

test:
	HYPOTHESIS_STORAGE_DIRECTORY=/tmp/quant-backtester-hypothesis $(PYTHON) -m pytest

test-cov:
	HYPOTHESIS_STORAGE_DIRECTORY=/tmp/quant-backtester-hypothesis $(PYTHON) -m pytest --cov=quant_backtester --cov-branch --cov-report=term-missing --cov-report=xml

check: lint type test

package:
	$(PYTHON) -m build

migrate:
	$(PYTHON) -m alembic upgrade head

migration-check:
	$(PYTHON) -m alembic check

run:
	$(PYTHON) -m quant_backtester.cli run --csv data/sample_prices.csv --symbols AAPL,MSFT

sweep:
	$(PYTHON) -m quant_backtester.cli sweep --csv data/sample_prices.csv --symbols AAPL,MSFT

smoke:
	mkdir -p runs/smoke
	LOG_ARGS=""; if [ -n "$$CI" ]; then LOG_ARGS="--json-logs"; fi; \
	$(PYTHON) -m quant_backtester.cli run $$LOG_ARGS --csv data/sample_prices.csv --symbols AAPL,MSFT --run-name smoke-run --db sqlite:///runs/smoke/smoke.db --out runs/smoke; \
	$(PYTHON) -m quant_backtester.cli sweep $$LOG_ARGS --csv data/sample_prices.csv --symbols AAPL,MSFT --run-name smoke-sweep --db sqlite:///runs/smoke/smoke.db --out runs/smoke --short-grid 5,10 --long-grid 15,20 --export-csv runs/smoke/sweep_smoke.csv; \
	DATABASE_URL=sqlite:///runs/smoke/smoke.db $(PYTHON) scripts/export_runs.py --tenant-id default; \
	$(PYTHON) scripts/benchmark_backtest.py --ticks 1000 --symbols AAPL,MSFT --repeats 1 --max-seconds 5 --csv runs/smoke/benchmark_input.csv
