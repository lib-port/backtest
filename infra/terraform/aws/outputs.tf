output "db_endpoint" {
  value = aws_db_instance.db.address
}

output "db_port" {
  value = aws_db_instance.db.port
}

output "secret_arn" {
  value = aws_secretsmanager_secret.db.arn
}
