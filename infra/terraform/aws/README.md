# Terraform AWS deployment

This configuration deploys the Quant Backtester control plane and workers on AWS. It is a
production-oriented foundation, not an account bootstrap: remote state, DNS delegation, the ACM
certificate, application secrets, image publication, and deployment orchestration remain owned
by the operator.

## Resources

- Two-AZ VPC with public ALB subnets, private ECS/RDS subnets, and one NAT gateway per AZ
- TLS-only Application Load Balancer, AWS WAF managed rules, and per-IP rate limiting
- ECS/Fargate API and worker services with private networking and queue-based worker autoscaling
- One-shot ECS task definition for Alembic migrations and runtime-role bootstrap
- Encrypted Multi-AZ RDS PostgreSQL with managed master credentials, backups, deletion
  protection, enhanced monitoring, and Performance Insights
- KMS-encrypted, versioned S3 artifact storage
- KMS-encrypted, versioned S3 dataset storage with Object Lock governance retention
- Encrypted SQS job queue and dead-letter queue
- Immutable ECR repository, least-privilege task/execution roles, CloudWatch logs, alarms,
  and dashboard

## Prerequisites

- Terraform 1.10 or newer and AWS provider credentials
- An immutable container reference ending in `@sha256:<digest>`
- An ACM certificate in the deployment region for `api_hostname`
- A Secrets Manager secret containing `{"username":"...","password":"..."}` for the
  non-owner application database role; the password must contain at least 24 characters
- Either:
  - `auth_mode = "api-key"` and a secret whose entire value is `QBT_API_KEYS_JSON`, or
  - `auth_mode = "oidc"` with HTTPS issuer/JWKS URLs and an audience
- An S3 backend bucket (and optional state KMS key) created outside this stack

The application database secret must be distinct from the RDS-managed master secret. Terraform
grants the ECS execution role read access to both, while API and worker task roles never receive
the master credential.

## Initialize and review

Supply backend settings explicitly:

```bash
cd infra/terraform/aws
terraform init \
  -backend-config="bucket=REPLACE_STATE_BUCKET" \
  -backend-config="key=quant-backtester/production.tfstate" \
  -backend-config="region=ap-southeast-2"
terraform fmt -check
terraform validate
terraform plan -out=production.tfplan \
  -var="container_image=ACCOUNT.dkr.ecr.ap-southeast-2.amazonaws.com/quant-backtester@sha256:REPLACE" \
  -var="certificate_arn=arn:aws:acm:ap-southeast-2:ACCOUNT:certificate/REPLACE" \
  -var="api_hostname=backtests.example.com" \
  -var="application_db_secret_arn=arn:aws:secretsmanager:ap-southeast-2:ACCOUNT:secret:REPLACE"
```

Provide authentication variables in a `.tfvars` file or through `TF_VAR_...` environment
variables. Do not put secret values in Terraform variables; this stack accepts only secret ARNs.

## Database migration sequence

The ECS migration task definition runs both `alembic upgrade head` with the RDS owner credential
and `quant-backtester-db-bootstrap` to create or rotate the application role. Terraform defines
the task but intentionally does not execute data-plane migrations.

For a new environment, use a staged rollout:

1. Apply the infrastructure with `bootstrap_mode = true`.
2. Run the emitted `migration_task_definition_arn` once in a private subnet using
   `application_security_group_id`.
3. Confirm that the task exits successfully.
4. Apply again with `bootstrap_mode = false`; production then requires two or more API and
   worker tasks.

On later releases, run the new migration task successfully before updating ECS services to the
new task definitions.

The task needs network access to RDS and Secrets Manager through the configured private-subnet
NAT routes. The API and worker refuse to start against a PostgreSQL schema whose Alembic revision
does not match the application.

## Dataset publication

Production jobs accept only registered datasets under `s3://<dataset-bucket>/datasets/`. Register
the exact object version URI, including `?versionId=...`, and its lowercase SHA-256 digest. The
worker downloads that version, enforces the size limit, and verifies the digest before execution.

## Destruction and recovery

RDS and the artifact, dataset, and access-log buckets use deletion protection or
`prevent_destroy`. Removing the stack therefore requires an explicit, reviewed retention or
data-migration decision. RDS also creates a uniquely named final snapshot. Dataset Object Lock
retention can prevent object deletion even after Terraform resources are removed.

Review costs before applying: the default production topology includes two NAT gateways,
Multi-AZ RDS, ALB/WAF, multiple Fargate tasks, KMS keys, and retained logs/backups.
