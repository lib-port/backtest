resource "aws_db_subnet_group" "database" {
  name       = "${local.name}-database"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_parameter_group" "database" {
  name_prefix = "${local.name}-postgres16-"
  family      = "postgres16"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  parameter {
    name  = "log_connections"
    value = "1"
  }

  parameter {
    name  = "log_disconnections"
    value = "1"
  }

  lifecycle { create_before_destroy = true }
}

data "aws_iam_policy_document" "rds_monitoring_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["monitoring.rds.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "rds_monitoring" {
  name               = "${local.name}-rds-monitoring"
  assume_role_policy = data.aws_iam_policy_document.rds_monitoring_assume.json
}

resource "aws_iam_role_policy_attachment" "rds_monitoring" {
  role       = aws_iam_role.rds_monitoring.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

resource "random_id" "final_snapshot" {
  byte_length = 4
}

resource "aws_db_instance" "database" {
  identifier = "${local.name}-postgres"

  engine         = "postgres"
  engine_version = "16.13"
  instance_class = var.db_instance_class

  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = var.db_max_allocated_storage
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.data.arn

  db_name  = var.db_name
  username = var.db_username
  port     = 5432

  manage_master_user_password   = true
  master_user_secret_kms_key_id = aws_kms_key.data.arn

  db_subnet_group_name   = aws_db_subnet_group.database.name
  vpc_security_group_ids = [aws_security_group.database.id]
  parameter_group_name   = aws_db_parameter_group.database.name
  publicly_accessible    = false
  multi_az               = var.db_multi_az

  backup_retention_period = 30
  backup_window           = "15:00-16:00"
  maintenance_window      = "sun:16:30-sun:17:30"
  copy_tags_to_snapshot   = true

  auto_minor_version_upgrade = true
  deletion_protection        = true
  skip_final_snapshot        = false
  final_snapshot_identifier  = "${local.name}-final-${random_id.final_snapshot.hex}"

  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]
  monitoring_interval             = 60
  monitoring_role_arn             = aws_iam_role.rds_monitoring.arn

  performance_insights_enabled          = true
  performance_insights_kms_key_id       = aws_kms_key.data.arn
  performance_insights_retention_period = 7

  apply_immediately = false

  lifecycle {
    prevent_destroy = true
  }
}
