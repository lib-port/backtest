# Quant Backtester

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](https://github.com/libport/backtest/blob/main/pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://github.com/libport/backtest/blob/main/LICENSE)
[![Lint: Ruff](https://img.shields.io/badge/lint-ruff-D7FF64?logo=ruff&logoColor=111111)](https://github.com/libport/backtest/blob/main/pyproject.toml)
[![Type checked: mypy](https://img.shields.io/badge/type%20checked-mypy-2A6DB2)](https://github.com/libport/backtest/blob/main/pyproject.toml)
[![Tests: pytest](https://img.shields.io/badge/tests-pytest-0A9EDC?logo=pytest&logoColor=white)](https://github.com/libport/backtest/tree/main/tests)
[![Last Commit](https://img.shields.io/github/last-commit/libport/backtest)](https://github.com/libport/backtest/commits/main)

A deterministic, event-driven research platform with a local CLI and a production control
plane. It supports reproducible backtests, parameter sweeps, walk-forward evaluation,
durable multi-tenant jobs, immutable dataset registration, and detailed PostgreSQL or SQLite
persistence.

## What it includes

- Event-driven backtest loop over CSV market data
- Multi-asset moving-average crossover strategy
- Validated UTC-normalized input with bounded-memory streaming and duplicate policy
- Simulated execution with market, limit, stop, and stop-limit orders; latency, queueing,
  participation limits, partial fills, price protection, and reduce-only liquidation
- Fixed-precision, replay-safe portfolio accounting with a balanced execution journal
- Position, gross-exposure, leverage, stop-loss, and drawdown risk controls
- Reproducible result hashes and manifests containing source, data, config, and engine provenance
- SQLAlchemy persistence with Alembic migrations for runs, orders, fills, equity, jobs,
  datasets, artifacts, and audit events
- FastAPI control plane, API-key or OIDC authentication, durable leased workers, and optional
  SQS/S3 integration
- Structured logs, Prometheus metrics, OpenTelemetry traces, benchmark/export utilities, and CI
- Hardened Docker Compose development stack and a complete Terraform AWS deployment

## Repository layout

- `src/quant_backtester/`: core package
- `src/quant_backtester/service/`: API, authentication, job queue, dataset registry, and worker
- `tests/`: unit, regression, property, service lifecycle, and smoke coverage
- `scripts/benchmark_backtest.py`: synthetic performance check
- `scripts/export_runs.py`: exports persisted runs to CSV
- `infra/docker/`: local API, worker, migration, and PostgreSQL stack
- `infra/terraform/aws/`: AWS VPC, ALB/WAF, ECS, RDS, S3, SQS, KMS, IAM, and monitoring
- `requirements/`: pinned production image and build dependency sets
- `data/sample_prices.csv`: sample input data

## Quickstart

```bash
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e ".[dev,postgres,service]"
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

## Execution and valuation semantics

- Each market event updates the current mark, processes eligible fills, and then records one
  post-fill equity point. Final equity is read from the live portfolio state, so a fill on the
  final data event is included in final equity and derived metrics.
- LIMIT prices are hard execution boundaries after spread and impact: BUY fills cannot exceed
  their limit and SELL fills cannot fall below it. Fill quantity is reduced when modeled impact
  exhausts the price headroom, and the remainder stays pending.
- Stop-loss liquidations have a single active order per symbol. They remain active through
  latency, price rebounds, and partial fills; conflicting pending strategy orders are cancelled,
  and reduce-only enforcement prevents a liquidation from reversing the position.
- Orders still pending when market data ends are not synthetically filled. The result's `extra`
  payload reports `pending_order_count` and `active_stop_symbols`.

## API and worker

Install the `service` extra to expose these entry points:

- `quant-backtester-api`: FastAPI control plane and browser console
- `quant-backtester-worker`: leased, retryable job runner
- `quant-backtester-db-bootstrap`: creates or rotates the least-privilege PostgreSQL runtime role

The API provides:

- `POST /v1/datasets` and `GET /v1/datasets`: immutable dataset registration
- `POST /v1/jobs`, `GET /v1/jobs`, and `POST /v1/jobs/{id}/cancel`: idempotent job lifecycle
- `GET /v1/runs`: tenant-scoped persisted results
- `/health/live`, `/health/ready`, `/metrics`, `/docs`, and a small console at `/`

Every job submission requires an `Idempotency-Key`. PostgreSQL is the durable job authority;
SQS is an optional at-least-once wake-up channel. Workers use renewable leases, bounded retries,
cancellation checks, and the job ID as the run ID so replay after a crash remains idempotent.

Authentication modes are `api-key`, `oidc`, and `disabled`. Disabled authentication is rejected
in production. API keys are configured as SHA-256 digests, never plaintext:

```bash
python -c "import hashlib; print(hashlib.sha256(b'REPLACE_TOKEN').hexdigest())"
```

Production mode also requires registered datasets. Dataset S3 URIs must stay within the
configured bucket/prefix and include an immutable `versionId`; workers verify the registered
SHA-256 digest before executing.

## Persistence

SQLite works out of the box. PostgreSQL support is available through the optional extra:

```bash
pip install -e ".[dev,postgres]"
alembic upgrade head
```

To export persisted runs:

```bash
DATABASE_URL=sqlite:///runs/runs.db python scripts/export_runs.py --tenant-id default
```

## Docker Compose

The Compose stack runs a one-shot migration followed by the API and worker against PostgreSQL.
Replace every placeholder in `.env` before starting it.

```bash
cp .env.example .env
docker compose -f infra/docker/docker-compose.yml up --build -d
curl http://127.0.0.1:8000/health/ready
```

The API binds only to `127.0.0.1` by default. Data is mounted read-only, while PostgreSQL and
result artifacts use named volumes. Application containers run with:

- a non-root UID/GID
- a read-only root filesystem and bounded `/tmp` tmpfs
- all Linux capabilities dropped
- `no-new-privileges`, resource limits, and an internal backend network
- pinned base and PostgreSQL image digests

## Terraform AWS deployment

`infra/terraform/aws/` defines a production foundation with:

- a two-AZ VPC, public ALB, private ECS/Fargate tasks, WAF, and TLS-only ingress
- encrypted Multi-AZ RDS PostgreSQL with managed master credentials
- separate application database credentials and a least-privilege bootstrap task
- versioned, encrypted artifact storage and object-locked dataset storage
- encrypted SQS/DLQ job wake-ups, immutable ECR, least-privilege IAM, and KMS
- autoscaling, CloudWatch logs, alarms, dashboard, and optional OTLP export

The image reference must be pinned by digest. An ACM certificate and a separately managed
Secrets Manager value for the application database role are required. See
`infra/terraform/aws/README.md` for the bootstrap/migration sequence and operational cautions.

## Development

Common local commands:

```bash
make install
make check
make test-cov
make smoke
make migration-check
make package
```

The repository also includes `.pre-commit-config.yaml` hooks for Ruff, mypy, and pytest.
GitHub Actions runs Python 3.11/3.13 quality checks, a PostgreSQL migration round trip, packaging,
dependency auditing, Terraform and Compose validation, a hardened container smoke test,
Trivy scanning, and CycloneDX SBOM generation.

## Next upgrades

- corporate actions and survivorship-bias handling
- richer execution models and market replay adapters
- distributed parameter sweeps
- additional strategies beyond moving-average crossover
