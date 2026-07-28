resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${local.name}/api"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.data.arn
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/ecs/${local.name}/worker"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.data.arn
}

resource "aws_cloudwatch_log_group" "migration" {
  name              = "/ecs/${local.name}/migration"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.data.arn
}

resource "aws_ecs_cluster" "this" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "enhanced"
  }
}

locals {
  database_environment = [
    { name = "QBT_DB_HOST", value = aws_db_instance.database.address },
    { name = "QBT_DB_PORT", value = tostring(aws_db_instance.database.port) },
    { name = "QBT_DB_NAME", value = var.db_name },
    { name = "QBT_DB_SSLMODE", value = "require" },
  ]
  database_admin_secrets = [
    {
      name      = "QBT_DB_USERNAME"
      valueFrom = "${aws_db_instance.database.master_user_secret[0].secret_arn}:username::"
    },
    {
      name      = "QBT_DB_PASSWORD"
      valueFrom = "${aws_db_instance.database.master_user_secret[0].secret_arn}:password::"
    },
  ]
  database_application_secrets = [
    {
      name      = "QBT_DB_USERNAME"
      valueFrom = "${var.application_db_secret_arn}:username::"
    },
    {
      name      = "QBT_DB_PASSWORD"
      valueFrom = "${var.application_db_secret_arn}:password::"
    },
  ]
  database_bootstrap_secrets = [
    {
      name      = "QBT_APP_DB_USERNAME"
      valueFrom = "${var.application_db_secret_arn}:username::"
    },
    {
      name      = "QBT_APP_DB_PASSWORD"
      valueFrom = "${var.application_db_secret_arn}:password::"
    },
  ]
  common_environment = concat(
    local.database_environment,
    [
      { name = "QBT_ENVIRONMENT", value = var.environment },
      { name = "QBT_LOG_LEVEL", value = "INFO" },
      { name = "QBT_QUEUE_URL", value = aws_sqs_queue.jobs.url },
      { name = "QBT_ARTIFACT_BUCKET", value = aws_s3_bucket.artifacts.id },
      { name = "QBT_ARTIFACT_PREFIX", value = "runs" },
      { name = "QBT_ARTIFACT_KMS_KEY_ID", value = aws_kms_key.data.arn },
      { name = "QBT_DATASET_BUCKET", value = aws_s3_bucket.datasets.id },
      { name = "QBT_DATASET_PREFIX", value = "datasets/" },
      { name = "QBT_ALLOW_LOCAL_DATASETS", value = "false" },
      { name = "QBT_REQUIRE_VERSIONED_DATASETS", value = "true" },
      { name = "QBT_REQUIRE_DATASET_HASH", value = "true" },
      { name = "QBT_REQUIRE_REGISTERED_DATASETS", value = "true" },
      { name = "QBT_REQUIRE_SORTED_DATA", value = "true" },
      { name = "QBT_MAX_DATASET_BYTES", value = "250000000" },
      { name = "QBT_SOURCE_REVISION", value = var.container_image },
    ],
    var.otlp_endpoint == "" ? [] : [
      { name = "OTEL_EXPORTER_OTLP_ENDPOINT", value = var.otlp_endpoint },
    ],
  )
  api_environment = concat(local.common_environment, [
    { name = "QBT_AUTH_MODE", value = var.auth_mode },
    { name = "QBT_OIDC_ISSUER", value = var.oidc_issuer },
    { name = "QBT_OIDC_AUDIENCE", value = var.oidc_audience },
    { name = "QBT_OIDC_JWKS_URL", value = var.oidc_jwks_url },
  ])
  api_secrets = concat(
    local.database_application_secrets,
    var.auth_mode == "api-key" ? [
      {
        name      = "QBT_API_KEYS_JSON"
        valueFrom = var.api_keys_secret_arn
      }
    ] : [],
  )
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.api_cpu)
  memory                   = tostring(var.api_memory)
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.api_task.arn

  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([
    {
      name                   = "api"
      image                  = var.container_image
      essential              = true
      readonlyRootFilesystem = true
      user                   = "10001:10001"
      command                = ["quant-backtester-api", "--json-logs"]
      environment            = local.api_environment
      secrets                = local.api_secrets
      portMappings = [{
        containerPort = 8000
        hostPort      = 8000
        protocol      = "tcp"
      }]
      linuxParameters = { initProcessEnabled = true }
      healthCheck = {
        command = [
          "CMD-SHELL",
          "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=2)\" || exit 1",
        ]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 30
      }
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.api.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "api"
        }
      }
    }
  ])
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.worker_cpu)
  memory                   = tostring(var.worker_memory)
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.worker_task.arn

  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }

  ephemeral_storage {
    size_in_gib = 50
  }

  volume {
    name = "scratch"
  }

  container_definitions = jsonencode([
    {
      name                   = "worker"
      image                  = var.container_image
      essential              = true
      readonlyRootFilesystem = true
      user                   = "10001:10001"
      command                = ["quant-backtester-worker", "--json-logs"]
      environment            = local.common_environment
      secrets                = local.database_application_secrets
      linuxParameters        = { initProcessEnabled = true }
      mountPoints = [{
        sourceVolume  = "scratch"
        containerPath = "/tmp"
        readOnly      = false
      }]
      ulimits = [{
        name      = "nofile"
        softLimit = 65536
        hardLimit = 65536
      }]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.worker.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "worker"
        }
      }
    }
  ])
}

resource "aws_ecs_task_definition" "migration" {
  family                   = "${local.name}-migration"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.execution.arn

  container_definitions = jsonencode([
    {
      name                   = "migration"
      image                  = var.container_image
      essential              = true
      readonlyRootFilesystem = true
      user                   = "10001:10001"
      command = [
        "sh",
        "-ceu",
        "python -m alembic upgrade head && quant-backtester-db-bootstrap",
      ]
      environment     = local.database_environment
      secrets         = concat(local.database_admin_secrets, local.database_bootstrap_secrets)
      linuxParameters = { initProcessEnabled = true }
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.migration.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "migration"
        }
      }
    }
  ])
}

resource "aws_lb" "api" {
  name                       = substr("${local.name}-api", 0, 32)
  internal                   = false
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.alb.id]
  subnets                    = aws_subnet.public[*].id
  enable_deletion_protection = true
  drop_invalid_header_fields = true

  access_logs {
    bucket  = aws_s3_bucket.access_logs.id
    prefix  = "alb"
    enabled = true
  }

  depends_on = [aws_s3_bucket_policy.access_logs]
}

resource "aws_lb_target_group" "api" {
  name        = substr("${local.name}-api", 0, 32)
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.this.id

  deregistration_delay = 30

  health_check {
    enabled             = true
    path                = "/health/ready"
    protocol            = "HTTP"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 15
    timeout             = 5
    matcher             = "200"
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.api.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_ecs_service" "api" {
  name            = "${local.name}-api"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.bootstrap_mode ? 0 : var.api_desired_count
  launch_type     = "FARGATE"

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 60
  enable_execute_command             = false

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  depends_on = [aws_lb_listener.https]

  lifecycle {
    ignore_changes = [desired_count]
  }
}

resource "aws_ecs_service" "worker" {
  name            = "${local.name}-worker"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.bootstrap_mode ? 0 : var.worker_desired_count
  launch_type     = "FARGATE"

  deployment_minimum_healthy_percent = 50
  deployment_maximum_percent         = 200
  enable_execute_command             = false

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = false
  }

  lifecycle {
    ignore_changes = [desired_count]
  }
}

resource "aws_appautoscaling_target" "api" {
  max_capacity       = 10
  min_capacity       = var.bootstrap_mode ? 0 : max(2, var.api_desired_count)
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.api.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "api_cpu" {
  name               = "${local.name}-api-cpu"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension
  service_namespace  = aws_appautoscaling_target.api.service_namespace

  target_tracking_scaling_policy_configuration {
    target_value       = 60
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}

resource "aws_appautoscaling_target" "worker" {
  max_capacity       = var.worker_max_count
  min_capacity       = var.bootstrap_mode ? 0 : max(1, var.worker_desired_count)
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.worker.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "worker_queue" {
  name               = "${local.name}-worker-queue"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.worker.resource_id
  scalable_dimension = aws_appautoscaling_target.worker.scalable_dimension
  service_namespace  = aws_appautoscaling_target.worker.service_namespace

  target_tracking_scaling_policy_configuration {
    target_value       = 1
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
    customized_metric_specification {
      metric_name = "ApproximateNumberOfMessagesVisible"
      namespace   = "AWS/SQS"
      statistic   = "Average"
      dimensions {
        name  = "QueueName"
        value = aws_sqs_queue.jobs.name
      }
    }
  }
}

resource "aws_wafv2_web_acl" "api" {
  name  = "${local.name}-api"
  scope = "REGIONAL"

  default_action {
    allow {}
  }

  rule {
    name     = "AWSManagedCommon"
    priority = 1
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-common"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "PerIpRateLimit"
    priority = 2
    action {
      block {}
    }
    statement {
      rate_based_statement {
        aggregate_key_type = "IP"
        limit              = var.waf_requests_per_five_minutes
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-rate-limit"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${local.name}-waf"
    sampled_requests_enabled   = true
  }
}

resource "aws_wafv2_web_acl_association" "api" {
  resource_arn = aws_lb.api.arn
  web_acl_arn  = aws_wafv2_web_acl.api.arn
}
