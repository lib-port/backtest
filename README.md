# Event-Driven Backtesting Platform Prototype

A production-grade, event-driven backtesting system designed to be **quant-dev interview-ready**:

- ✅ Slippage model (spread + impact) in execution
- ✅ Multi-asset support (positions keyed by symbol)
- ✅ Risk limits (max position, stop-loss, max drawdown halt)
- ✅ Persistence to SQLite (default) / PostgreSQL (optional)
- ✅ Parameter sweep runner (grid over short/long windows per symbol)
- ✅ Reproducibility controls (config file + RNG seed)
- ✅ Structured logging support (JSON or plain)
- ✅ Docker Compose secure pipeline + Terraform AWS template for Postgres (RDS + Secrets Manager)

## Quickstart (local SQLite)

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -U pip
pip install -e ".[dev]"
pytest
python -m quant_backtester.cli run --csv data/sample_prices.csv --symbols AAPL,MSFT
python -m quant_backtester.cli sweep --csv data/sample_prices.csv --symbols AAPL,MSFT
python scripts/benchmark_backtest.py --ticks 5000 --repeats 2
```

Backtest results are stored in `./runs/runs.db` by default.

## Quickstart (Docker Compose + Postgres)

```bash
cp .env.example .env
docker compose -f infra/docker/docker-compose.yml up --build -d postgres
# then, in another terminal:
docker compose -f infra/docker/docker-compose.yml run --rm backtester run --csv /app/data/sample_prices.csv --symbols AAPL,MSFT
```

## Data format

CSV columns:
- `date` (ISO date or timestamp)
- `symbol` (e.g., AAPL)
- `mid` (mid price)
- Optional: `bid`, `ask`
- Optional: `spread_bps` (used if bid/ask absent)
- Optional: `volume` (per-tick available volume for execution simulation)

See `data/sample_prices.csv`.

## Secure pipeline highlights

Docker Compose hardening:
- non-root user
- read-only root filesystem (tmpfs for /tmp)
- minimal capabilities (no-new-privileges)
- separate Postgres network
- credentials via `.env` (for demo) and ready for Docker secrets pattern

Terraform AWS template:
- RDS Postgres in private subnets
- Security group restricted by `allowed_cidr`
- Master password stored in AWS Secrets Manager
- IAM policy separation (you still need to wire your runtime to fetch the secret)

> Note: the Terraform is a template; it is intentionally conservative and will require you to set variables.

## CLI

- `run`: single backtest
- `sweep`: parameter grid search; writes results to DB and exports a CSV summary
- `--config`: load config from `.json`/`.yml`/`.yaml`
- `--dry-run`: validate effective config and exit
- `--no-persist`: run backtest without writing rows to DB
- `--rng-seed`: reproducible execution randomness
- `--json-logs`: emit structured logs

```bash
python -m quant_backtester.cli --help
```

Example config file (`config.json`):

```json
{
  "symbols": ["AAPL", "MSFT"],
  "csv_path": "data/sample_prices.csv",
  "run_name": "cfg-run",
  "short_window": 5,
  "long_window": 10,
  "execution": {
    "rng_seed": 123
  }
}
```

Use it:

```bash
python -m quant_backtester.cli run --config config.json --dry-run
python -m quant_backtester.cli run --config config.json
```

## Next upgrades

- corporate actions + survivorship bias handling
- FIX / market data replay adapters
- better execution models (queue position, latency, partial fills)
- distributed sweeps (Ray / Dask)

## Alembic migrations

```bash
pip install -e ".[dev]"
alembic upgrade head
```

## CI pipeline

GitHub Actions workflows are intentionally not used in this repository.

Run the same checks locally or in your preferred CI system:

```bash
pip install -e ".[dev,postgres]"
ruff format --check .
ruff check .
mypy
pytest
python -m quant_backtester.cli run --csv data/sample_prices.csv --symbols AAPL,MSFT --db sqlite:///runs/ci_runs.db --run-name ci
python scripts/benchmark_backtest.py --ticks 5000 --repeats 2 --max-seconds 20
```

## Developer workflow

Pre-commit hooks are configured in `.pre-commit-config.yaml` for formatting, linting, type checks, and tests.
