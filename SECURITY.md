# Security policy

## Security assumptions

- Internet traffic reaches only the TLS Application Load Balancer and WAF.
- API, worker, and migration tasks use distinct database and secret identities.
- PostgreSQL row-level security is a defence-in-depth boundary; every API transaction also sets
  an explicit tenant context.
- The worker is a trusted data-plane component with `BYPASSRLS`, but it receives tenant-qualified
  durable jobs and has no delete privilege.
- Dataset versions and result artifacts are immutable and hash-verified.
- The RDS owner credential is used only by the one-shot migration task.

Compromise of the AWS account, KMS administrators, the RDS owner, or the container build pipeline
is outside the application-level tenant boundary and requires the platform incident process.

## Secret handling

Never commit bearer tokens, API-key peppers, database URLs, `.env` files, Terraform state, or
cloud credentials. Rotate a secret immediately if it appears in a log or commit. Structured
application logs redact common credential forms, but callers must still avoid placing secrets in
job payloads, dataset metadata, run names, or identifiers.
