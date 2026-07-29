# Quant Backtester documentation

This directory is the engineering and operations handbook for Quant Backtester. It explains the
deployed system, the invariants that must survive changes, and the procedures used to release,
observe, recover, and secure it.

The root [README](../README.md) remains the installation and user quickstart. These documents are
for maintainers, reviewers, platform engineers, security engineers, and incident responders.

## Documentation
| Document | Read it when |
| --- | --- |
| [Architecture](architecture.md) | Changing service boundaries, engine semantics, persistence, tenancy, or infrastructure |
| [Operations runbook](runbooks/operations.md) | Deploying, monitoring, responding to an alert, rotating secrets, or recovering data |
| [ADR index](adr/README.md) | Reviewing why a durable-system decision exists or proposing a replacement |
| [ADR 0001](adr/0001-durable-fenced-jobs.md) | Changing queues, leases, retries, cancellation, or worker ownership |
| [ADR 0002](adr/0002-transactional-artifact-outbox.md) | Changing result persistence, object publication, reconciliation, or artifact immutability |

## Non-negotiable system invariants

Treat these as review gates:

1. PostgreSQL is authoritative for dataset registration, job state, leases, runs, and artifact
   publication state. SQS is never authoritative.
2. A worker may mutate a running job only while its `worker_id`, `lease_generation`, status, and
   unexpired database-clock lease still match.
3. A service job cannot become `SUCCEEDED` while its exact staged artifact remains unpublished;
   the returned descriptor must match the staged SHA-256 and byte length.
4. Tenant identity comes from a validated credential, is carried explicitly through every API
   transaction, and is enforced again by PostgreSQL FORCE RLS.
5. Runtime services never receive the RDS owner credential. API, worker, and migration tasks have
   separate database and AWS identities.
6. Production inputs are registered, versioned, size-bounded, strictly parsed, and verified by
   SHA-256 before execution.
7. A result hash excludes operational metadata and includes every behavior-affecting deterministic
   input.
8. Monetary and price values are quantized in portfolio accounting and stored as
   `NUMERIC(38,8)` at persistence boundaries.
9. Audit events are append-only. Public job errors are stable codes and messages; internal
   exception detail stays in restricted storage and logs.
10. Application startup fails when the database schema revision is not the expected Alembic head.

## Source-of-truth order

When documentation and implementation disagree, stop the rollout and reconcile them. For
investigation, use this order:

1. Alembic migrations and database constraints for schema and RLS behavior.
2. Application code and tests for runtime behavior.
3. Terraform and Docker Compose for deployed topology and configuration.
4. ADRs for the intent behind architectural constraints.
5. Narrative documentation.

Do not silently treat stale documentation as an operator override.

## Documentation change policy

Update documentation in the same pull request when a change affects any of the following:

- a public endpoint, role, request limit, job state, retry rule, or error code;
- an environment variable or its default;
- a table, migration order, RLS policy, grant, or runtime database role;
- an IAM permission, network path, health check, alarm, dashboard, backup, or retention setting;
- the reproducibility contract, engine-semantics version, result manifest, or end-of-run behavior;
- a deployment, rollback, rotation, incident, or recovery procedure.

For Mermaid diagrams:

- use stable component names that match code and Terraform;
- label trust-boundary crossings and authoritative stores;
- keep secrets and account-specific identifiers out of diagrams;
- prefer several focused diagrams over one unreadable graph;
- validate every diagram before merge.

## Terminology

| Term | Meaning |
| --- | --- |
| Control plane | FastAPI service that authenticates callers and manages datasets, jobs, and result reads |
| Worker data plane | Trusted worker process that claims durable jobs and runs simulations |
| Durable job | The `run_jobs` row in PostgreSQL |
| Wake-up | An at-least-once SQS message containing job and tenant IDs |
| Lease generation | Monotonic fencing token incremented on each successful claim |
| Artifact outbox | Exact serialized result bytes staged in `artifact_publications` with the run |
| Registered dataset | Tenant-scoped immutable dataset descriptor with URI, schema version, and SHA-256 |
| Result identity | Deterministic SHA-256 over behavior-affecting result content |
| Provenance | Source revision, image digest, platform, and other replay evidence not itself used as result identity |
