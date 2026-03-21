.PHONY: install lint type test run sweep smoke

PYTHON ?= .venv/bin/python

install:
	pip install -U pip
	pip install -e ".[dev]"

lint:
	ruff format --check .
	ruff check .

type:
	mypy

test:
	pytest

run:
	$(PYTHON) -m quant_backtester.cli run --csv data/sample_prices.csv --symbols AAPL,MSFT

sweep:
	$(PYTHON) -m quant_backtester.cli sweep --csv data/sample_prices.csv --symbols AAPL,MSFT

smoke:
	mkdir -p runs/smoke
	LOG_ARGS=""; if [ -n "$$CI" ]; then LOG_ARGS="--json-logs"; fi; \
	$(PYTHON) -m quant_backtester.cli run $$LOG_ARGS --csv data/sample_prices.csv --symbols AAPL,MSFT --run-name smoke-run --db sqlite:///runs/smoke/smoke.db --out runs/smoke; \
	$(PYTHON) -m quant_backtester.cli sweep $$LOG_ARGS --csv data/sample_prices.csv --symbols AAPL,MSFT --run-name smoke-sweep --db sqlite:///runs/smoke/smoke.db --out runs/smoke --short-grid 5,10 --long-grid 15,20 --export-csv runs/smoke/sweep_smoke.csv; \
	DATABASE_URL=sqlite:///runs/smoke/smoke.db $(PYTHON) scripts/export_runs.py; \
	$(PYTHON) scripts/benchmark_backtest.py --ticks 1000 --symbols AAPL,MSFT --repeats 1 --max-seconds 5 --csv runs/smoke/benchmark_input.csv
