# Quant Backtester

[![CI](https://github.com/lib-port/backtest/actions/workflows/ci.yml/badge.svg)](https://github.com/lib-port/backtest/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](https://github.com/libport/backtest/blob/main/pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://github.com/libport/backtest/blob/main/LICENSE)

A deterministic, event-driven research platform with a local CLI and a production control
plane. It supports reproducible backtests, parameter sweeps, walk-forward evaluation,
durable multi-tenant jobs, immutable dataset registration, and detailed PostgreSQL or SQLite
persistence.

This project is for research and simulation. It does not provide brokerage connectivity, live
order routing, or investment advice.

## What it includes

- Event-driven backtest loop over CSV market data
- Multi-asset moving-average crossover strategy
- Validated UTC-normalized input with bounded-memory streaming and duplicate policy
- Simulated execution with market, limit, stop, and stop-limit orders; latency, queueing,
  participation limits, partial fills, price protection, and reduce-only liquidation
- Fixed-precision, replay-safe portfolio accounting with a balanced execution journal
- Position, gross/net-exposure, leverage, order-notional, daily-loss, stop-loss, drawdown, and
  short-sale risk controls
- Explicit end-of-run order and position policies, including persisted forced liquidation
- Reproducible result hashes and versioned manifests containing raw/normalized data, warm-up,
  strategy source, RNG stream, source revision, image, config, and engine provenance
- SQLAlchemy persistence with Alembic migrations for runs, orders, fills, equity, jobs,
  datasets, an artifact outbox, and append-only audit events
- FastAPI control plane, peppered API-key or strict OIDC authentication, forced PostgreSQL RLS,
  fenced leased workers, and optional SQS/S3 integration
- Structured logs, Prometheus metrics, OpenTelemetry traces, benchmark/export utilities, and CI
- Hardened Docker Compose development stack and a production-oriented Terraform AWS foundation

## Repository layout

- `src/quant_backtester/`: core package
- `src/quant_backtester/service/`: API, authentication, job queue, dataset registry, and worker
- `alembic/`: reversible schema migrations and the current database contract
- `tests/`: unit, regression, property, service lifecycle, and smoke coverage
- `scripts/benchmark_backtest.py`: synthetic performance check
- `scripts/export_runs.py`: exports persisted runs to CSV
- `infra/docker/`: local API, worker, migration, and PostgreSQL stack
- `infra/terraform/aws/`: AWS VPC, ALB/WAF, ECS, RDS, S3, SQS, KMS, IAM, and monitoring
- `requirements/`: pinned production image and build dependency sets
- `data/sample_prices.csv`: sample input data
- `docs/architecture.md`: trust boundaries, lifecycle, and reproducibility contract
- `docs/runbooks/operations.md`: deployment, incident, rollback, and recovery procedures

## Requirements

- Python 3.11 or newer; CI exercises Python 3.11 and 3.13
- Docker with Compose v2 for the local service stack
- Terraform 1.10 or newer and AWS credentials only for the AWS deployment

The CLI needs only the base package. PostgreSQL, the API/worker, and development tooling are
provided by the `postgres`, `service`, and `dev` extras respectively.

## Quickstart

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev,postgres,service]"
python -m pytest
quant-backtester run --csv data/sample_prices.csv --symbols AAPL,MSFT
quant-backtester sweep --csv data/sample_prices.csv --symbols AAPL,MSFT \
  --short-grid 3,5 --long-grid 10,15
quant-backtester walk-forward --csv data/sample_prices.csv --symbols AAPL,MSFT \
  --short-grid 3,5 --long-grid 10,15 \
  --train-days 40 --test-days 20 --step-days 20 --warmup-days 15
python scripts/benchmark_backtest.py --ticks 5000 --repeats 2
```

The default database is `sqlite:///runs/runs.db`. Output artifacts are written under `runs/`.
The walk-forward values above are deliberately sized for the bundled 120-day sample.

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
- `--rng-seed`: override the deterministic execution seed
- `--source-timezone` and `--duplicate-policy`: control input normalization
- `--pending-order-policy` and `--position-end-policy`: select explicit end-of-data behavior

```bash
quant-backtester --help
quant-backtester run --help
```

Example CLI config, sized to work with the bundled sample:

```json
{
  "symbols": ["AAPL", "MSFT"],
  "csv_path": "data/sample_prices.csv",
  "run_name": "cfg-run",
  "short_grid": [3, 5],
  "long_grid": [10, 15],
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
    "max_drawdown_pct": 0.2,
    "max_gross_exposure": 150000,
    "max_net_exposure": 100000,
    "max_order_notional": 25000,
    "max_daily_loss_pct": 0.03,
    "allow_short": false
  },
  "data": {
    "source_timezone": "UTC",
    "duplicate_policy": "error",
    "chunksize": 50000,
    "require_sorted": true,
    "max_events": 2000000,
    "strict_schema": true,
    "require_all_symbols": true
  },
  "metrics": {
    "annual_risk_free_rate": 0.0,
    "periods_per_year": 365.25
  },
  "end_of_run": {
    "pending_order_policy": "cancel",
    "position_policy": "mark_to_market"
  },
  "walk_forward": {
    "train_days": 40,
    "test_days": 20,
    "step_days": 20,
    "purge_days": 0,
    "embargo_days": 0,
    "warmup_days": 15
  }
}
```

Example usage with a config file:

```bash
quant-backtester run --config config.json --dry-run
quant-backtester run --config config.json
quant-backtester sweep --config config.json --export-csv runs/sweep_results.csv
quant-backtester walk-forward --config config.json \
  --export-csv runs/walk_forward_results.csv
```

Command-line values take precedence over config-file values. The service API accepts the same
backtest fields under a top-level `config` object, but not CLI-only sweep or walk-forward fields.

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

Strict schema validation is enabled by default. It rejects extra columns, malformed optional
values, inconsistent quoting, non-finite or non-positive prices, invalid bid/mid/ask ordering,
negative volume, unauthorized symbols, duplicates under the default policy, and unsorted input.
Naive timestamps are interpreted in `data.source_timezone` and normalized to UTC. See
`data/sample_prices.csv` for the expected shape.

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
- End-of-data handling is explicit. Pending orders default to cancellation (or can be retained);
  positions can be marked to market, forcibly liquidated through persisted fills, or required to
  be flat. No implicit synthetic fill is hidden from the order/fill ledger.

## API and worker

Install the `service` extra to expose these entry points:

- `quant-backtester-api`: FastAPI control plane
- `quant-backtester-worker`: leased, retryable job runner
- `quant-backtester-db-bootstrap`: creates or rotates a purpose-specific API, worker, or reporting
  PostgreSQL role

The API surface and minimum roles are:

| Method | Path | Minimum role | Purpose |
| --- | --- | --- | --- |
| `GET` | `/health/live`, `/health/ready` | Public | Process and database readiness |
| `GET` | `/metrics` | Observer | Prometheus metrics |
| `POST` | `/v1/datasets` | Researcher | Register an immutable dataset version |
| `GET` | `/v1/datasets`, `/v1/datasets/{dataset_id}` | Observer | List or inspect datasets |
| `POST` | `/v1/jobs` | Researcher | Submit an idempotent backtest job |
| `GET` | `/v1/jobs`, `/v1/jobs/{job_id}` | Observer | List or inspect jobs |
| `POST` | `/v1/jobs/{job_id}/cancel` | Researcher | Request cancellation |
| `GET` | `/v1/runs`, `/v1/runs/{run_id}` | Observer | List or inspect persisted runs |

All `/v1` endpoints and `/metrics` require an `Authorization: Bearer <token>` header. Researchers
inherit observer access, and admins can perform every operation. OpenAPI docs at `/docs` and the
small browser console at `/` are available only in development and test; they are disabled in
staging and production.

Every job submission requires an `Idempotency-Key`. PostgreSQL is the durable job authority;
SQS is an optional at-least-once wake-up channel. Workers use database-clock renewable leases,
monotonic fencing tokens, independent heartbeats, deterministic bounded retries, cancellation and
shutdown checks, and the job ID as the run ID so crash recovery remains idempotent. Results and
their exact artifact bytes commit through a transactional outbox; publication failure moves a job
to `WAITING_ARTIFACT` for reconciliation instead of recomputation.

Authentication modes are `api-key`, `oidc`, and `disabled`. Disabled authentication is rejected
in staging and production. Production API keys require a separately stored random pepper of at
least 32 characters and include key IDs and optional expiry. Store only SHA-256 digests of
`pepper + token`:

```bash
python - <<'PY'
import getpass
import hashlib

pepper = getpass.getpass("Pepper: ")
token = getpass.getpass("Token: ")
print(hashlib.sha256(f"{pepper}{token}".encode()).hexdigest())
PY
```

Staging and production also require registered datasets. Dataset S3 URIs must stay within the
configured bucket/prefix and include an immutable `versionId`; workers verify the registered
SHA-256 digest before executing.

## Persistence

SQLite works out of the box. PostgreSQL support is available through the optional extra:

```bash
python -m pip install -e ".[dev,postgres]"
python -m alembic upgrade head
```

Production uses distinct RDS owner, API, and worker credentials. Run
`quant-backtester-db-bootstrap --role api` and `--role worker` as the owner after every schema
change. Run it with `--role reporting` to create the optional read-only reporting role. Runtime
startup fails closed when the database revision does not match the application.

To export persisted runs:

```bash
DATABASE_URL=sqlite:///runs/runs.db python scripts/export_runs.py --tenant-id default
```

## Docker Compose

The Compose stack runs a one-shot migration and role bootstrap followed by the API and worker
against PostgreSQL. The services use separate non-owner credentials. Replace every placeholder
in `.env` before starting it.

```bash
cp .env.example .env
docker compose --env-file .env -f infra/docker/docker-compose.yml up --build --detach
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
- separate API/worker database credentials and execution roles, with the owner credential
  restricted to the migration task
- versioned, KMS-encrypted, Object-Locked artifact and dataset storage
- encrypted SQS/DLQ job wake-ups, immutable ECR, least-privilege IAM, and KMS
- autoscaling, CloudWatch logs, alarms, dashboard, and optional OTLP export

The image reference must be pinned by digest and paired with its full Git source revision. An ACM
certificate and separately managed API, worker, and authentication secrets are required. See
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

GitHub Actions runs Python 3.11/3.13 quality checks, a PostgreSQL migration and runtime-role/RLS
integration test, packaging, dependency auditing, Terraform and Compose validation, a hardened
container smoke test, Trivy scanning, and CycloneDX SBOM generation.

See [SECURITY.md](SECURITY.md), the
[architecture](docs/architecture.md), and the [operations runbook](docs/runbooks/operations.md)
before changing security, persistence, or deployment boundaries.

## Current scope and limitations

- The bundled market-data adapter reads CSV; production workers can download registered,
  versioned CSV datasets from S3.
- The bundled strategy is moving-average crossover. The engine interfaces permit additional
  strategies, but none are shipped here.
- Corporate actions, dividends, point-in-time constituent membership, and survivorship-bias
  correction are not applied automatically; datasets must encode the intended research policy.
- Parameter sweeps and walk-forward evaluation execute within one process rather than a
  distributed research scheduler.
- Execution is a configurable simulator, not an exchange, broker, or guarantee of achievable
  fills.

## License

Licensed under the [MIT License](LICENSE).
