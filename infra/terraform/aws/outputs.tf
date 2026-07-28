output "api_base_url" {
  description = "Public HTTPS base URL. If route53_zone_id is omitted, create this DNS alias externally."
  value       = "https://${var.api_hostname}"
}

output "load_balancer_dns_name" {
  value = aws_lb.api.dns_name
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "api_service_name" {
  value = aws_ecs_service.api.name
}

output "worker_service_name" {
  value = aws_ecs_service.worker.name
}

output "migration_task_definition_arn" {
  description = "Run this task in the private subnets before deploying new API and worker revisions."
  value       = aws_ecs_task_definition.migration.arn
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "api_security_group_id" {
  value = aws_security_group.api.id
}

output "worker_security_group_id" {
  description = "Use this security group when running the migration task in private subnets."
  value       = aws_security_group.worker.id
}

output "database_endpoint" {
  value = aws_db_instance.database.address
}

output "database_port" {
  value = aws_db_instance.database.port
}

output "database_master_secret_arn" {
  value     = aws_db_instance.database.master_user_secret[0].secret_arn
  sensitive = true
}

output "artifact_bucket_name" {
  value = aws_s3_bucket.artifacts.id
}

output "dataset_bucket_name" {
  value = aws_s3_bucket.datasets.id
}

output "job_queue_url" {
  value = aws_sqs_queue.jobs.url
}

output "job_dead_letter_queue_url" {
  value = aws_sqs_queue.dead_letter.url
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "kms_key_arn" {
  value = aws_kms_key.data.arn
}

output "operations_dashboard_name" {
  value = aws_cloudwatch_dashboard.operations.dashboard_name
}
