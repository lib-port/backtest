# Contributing

## Local setup

Use Python 3.11 or newer:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev,postgres,service]"
make check
```

Install the pre-commit hooks with `.venv/bin/pre-commit install`. Keep generated output,
credentials, local databases, Hypothesis state, and benchmark artifacts out of commits.

## Change requirements

- Add regression tests for changed behaviour and property tests for accounting or execution
  invariants.
- Preserve deterministic results: do not depend on process hash order, wall-clock time, global
  random state, or input row order unless that ordering is part of the validated contract.
- Treat API models as untrusted input and keep resource bounds explicit.
- Preserve tenant filters and PostgreSQL tenant context on every API data path.
- Use fenced database leases for worker state changes and the artifact outbox for publication.
- Store monetary and price values at fixed precision at persistence boundaries.
- Never expose internal exception text through the public job API.

Schema changes require a reversible Alembic migration, an update to
`EXPECTED_SCHEMA_REVISION`, a fresh-upgrade test, and a downgrade/upgrade round trip. Runtime
privilege changes must also update `quant-backtester-db-bootstrap` and the PostgreSQL integration
test.

## Before opening a pull request

```bash
make check
make test-cov
make smoke
make package
docker compose --env-file .env -f infra/docker/docker-compose.yml config --quiet
```

Also run `terraform fmt -check` and `terraform validate` in `infra/terraform/aws` when
infrastructure changes. Pull requests should describe operational impact, migration order,
rollback constraints, and any change to the reproducibility contract.
