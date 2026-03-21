# Terraform AWS: Secure Postgres (RDS) Template

This is a conservative template to provision:
- VPC (2 public + 2 private subnets)
- RDS PostgreSQL in **private** subnets
- Security Group allowing inbound Postgres only from `allowed_cidr`
- Master password stored in AWS Secrets Manager
- Outputs for endpoint + secret ARN

## Usage

```bash
cd infra/terraform/aws
terraform init
terraform apply -var="aws_region=ap-southeast-1" -var="allowed_cidr=YOUR_PUBLIC_IP/32"
```

Then build your `DATABASE_URL` by fetching the secret value and using the RDS endpoint.

## Notes

- You must have AWS credentials configured (env vars or AWS profile).
- You should wire your application to fetch the secret at runtime (e.g., via IAM role + Secrets Manager API).
