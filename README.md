# Event-Driven Backtesting Platform Prototype

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](https://github.com/libport/backtest/blob/main/pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://github.com/libport/backtest/blob/main/LICENSE)
[![Lint: Ruff](https://img.shields.io/badge/lint-ruff-D7FF64?logo=ruff&logoColor=111111)](https://github.com/libport/backtest/blob/main/pyproject.toml)
[![Type checked: mypy](https://img.shields.io/badge/type%20checked-mypy-2A6DB2)](https://github.com/libport/backtest/blob/main/pyproject.toml)
[![Tests: pytest](https://img.shields.io/badge/tests-pytest-0A9EDC?logo=pytest&logoColor=white)](https://github.com/libport/backtest/tree/main/tests)
[![Last Commit](https://img.shields.io/github/last-commit/libport/backtest)](https://github.com/libport/backtest/commits/main)

An event-driven Python backtesting prototype with reproducible runs, parameter sweeps, walk-forward evaluation, execution slippage, risk controls, and optional persistence to SQLite or PostgreSQL.

## What it includes

- Event-driven backtest loop over CSV market data
- Multi-asset moving-average crossover strategy
- Simulated execution with spread, impact, latency, queueing, and partial fills
- Portfolio risk controls for max position, stop-loss, and max drawdown halt
- Persistence via SQLAlchemy with Alembic migrations
- Parameter sweeps and walk-forward optimization
- Structured logging, benchmark script, and CSV export utilities
- Local Docker Compose Postgres stack plus a Terraform AWS RDS template

## Repository layout

- `src/quant_backtester/`: core package
- `tests/`: smoke, config, execution, sweep, and walk-forward coverage
- `scripts/benchmark_backtest.py`: synthetic performance check
- `scripts/export_runs.py`: exports persisted runs to CSV
- `infra/docker/`: local Postgres container setup
- `infra/terraform/aws/`: conservative AWS RDS template
- `data/sample_prices.csv`: sample input data

## Quickstart

```bash
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e ".[dev]"
pytest
python -m quant_backtester.cli run --csv data/sample_prices.csv --symbols AAPL,MSFT
python -m quant_backtester.cli sweep --csv data/sample_prices.csv --symbols AAPL,MSFT
python -m quant_backtester.cli walk-forward --csv data/sample_prices.csv --symbols AAPL,MSFT --short-grid 5,10,20 --long-grid 30,50,100 --train-days 252 --test-days 63 --step-days 21
python scripts/benchmark_backtest.py --ticks 5000 --repeats 2
```

The default database is `sqlite:///runs/runs.db`. Output artifacts are written under `runs/`.

## CLI

Available subcommands:

- `run`: execute one backtest
- `sweep`: evaluate a grid of `short_window` / `long_window` pairs and export ranked results
- `walk-forward`: optimize on rolling train windows and evaluate out-of-sample windows

Useful flags shared across commands:

- `--config`: load `.json`, `.yml`, or `.yaml`
- `--dry-run`: validate and print the effective config
- `--db`: override the database URL
- `--out`: override the output directory
- `--no-persist`: skip database writes
- `--json-logs`: emit structured logs
- `--rng-seed`: make simulated execution deterministic

```bash
python -m quant_backtester.cli --help
```

Example config:

```json
{
  "symbols": ["AAPL", "MSFT"],
  "csv_path": "data/sample_prices.csv",
  "run_name": "cfg-run",
  "short_grid": [5, 10, 20],
  "long_grid": [30, 50, 100],
  "short_window": 5,
  "long_window": 10,
  "initial_cash": 100000,
  "trade_quantity": 100,
  "commission_per_trade": 1.0,
  "execution": {
    "rng_seed": 123,
    "default_spread_bps": 5.0,
    "impact_bps_per_unit": 2.0,
    "impact_volume": 10000.0,
    "micro": {
      "latency_events": 1,
      "default_tick_volume": 5000.0,
      "max_participation_rate": 0.2,
      "queue_ahead_fraction": 0.7,
      "base_fill_probability": 0.8
    }
  },
  "risk": {
    "max_position_per_symbol": 1000,
    "stop_loss_pct": 0.05,
    "max_drawdown_pct": 0.2
  },
  "walk_forward": {
    "train_days": 252,
    "test_days": 63,
    "step_days": 21
  }
}
```

Example usage with a config file:

```bash
python -m quant_backtester.cli run --config config.json --dry-run
python -m quant_backtester.cli run --config config.json
python -m quant_backtester.cli sweep --config config.json --export-csv runs/sweep_results.csv
python -m quant_backtester.cli walk-forward --config config.json --export-csv runs/walk_forward_results.csv
```

## Data format

Required CSV columns:

- `date`: ISO date or timestamp
- `symbol`: instrument identifier such as `AAPL`
- `mid`: mid price

Optional columns:

- `bid`
- `ask`
- `spread_bps`
- `volume`

See `data/sample_prices.csv` for the expected shape.

## Persistence

SQLite works out of the box. PostgreSQL support is available through the optional extra:

```bash
pip install -e ".[dev,postgres]"
alembic upgrade head
```

To export persisted runs:

```bash
DATABASE_URL=sqlite:///runs/runs.db python scripts/export_runs.py
```

## Docker Compose

The Docker setup runs the backtester against PostgreSQL using the settings in `.env.example`.

```bash
cp .env.example .env
docker compose -f infra/docker/docker-compose.yml up --build -d postgres
docker compose -f infra/docker/docker-compose.yml run --rm backtester run --csv /app/data/sample_prices.csv --symbols AAPL,MSFT
```

Current hardening in `infra/docker/docker-compose.yml` includes:

- non-root container user
- read-only root filesystem
- `tmpfs` for `/tmp`
- dropped Linux capabilities
- `no-new-privileges`

## Terraform AWS template

`infra/terraform/aws/` provisions a conservative PostgreSQL foundation:

- VPC with public and private subnets
- RDS PostgreSQL in private subnets
- security group restricted by `allowed_cidr`
- master password stored in AWS Secrets Manager

It is a template, not a one-command production deployment. Review variables and networking before use.

## Development

Common local commands:

```bash
make install
make lint
make type
make test
make smoke
```

The repository also includes `.pre-commit-config.yaml` hooks for Ruff, mypy, and pytest.

GitHub Actions are intentionally not configured here, so validation is expected to run locally or in your preferred CI system.

## Next upgrades

- corporate actions and survivorship-bias handling
- richer execution models and market replay adapters
- distributed parameter sweeps
- additional strategies beyond moving-average crossover
