# Security policy

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Use GitHub’s private
“Report a vulnerability” flow for this repository and include:

- the affected revision and deployment mode;
- a minimal reproduction or proof of concept;
- the expected and observed security boundary;
- any known exposure of credentials, tenant data, or immutable artifacts.

Do not include real customer data or active credentials. Maintainers should acknowledge a
complete report privately, assess severity, and coordinate remediation and disclosure before
publishing details.

## Supported versions

Security fixes target the current `main` branch and the most recent release. Operators should
deploy immutable images by digest and keep PostgreSQL, the base image, and Python dependencies on
supported patch releases.

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
