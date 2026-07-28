data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "api_execution" {
  name               = "${local.name}-api-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "api_execution" {
  role       = aws_iam_role.api_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "worker_execution" {
  name               = "${local.name}-worker-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "worker_execution" {
  role       = aws_iam_role.worker_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "migration_execution" {
  name               = "${local.name}-migration-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "migration_execution" {
  role       = aws_iam_role.migration_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "api_execution_secrets" {
  statement {
    sid     = "ReadApiSecrets"
    actions = ["secretsmanager:GetSecretValue"]
    resources = compact([
      var.api_db_secret_arn,
      var.api_keys_secret_arn,
      var.api_key_pepper_secret_arn,
    ])
  }

  statement {
    sid       = "DecryptApiSecrets"
    actions   = ["kms:Decrypt"]
    resources = distinct(concat([aws_kms_key.data.arn], var.api_secrets_kms_key_arns))
  }
}

resource "aws_iam_role_policy" "api_execution_secrets" {
  name   = "api-secrets"
  role   = aws_iam_role.api_execution.id
  policy = data.aws_iam_policy_document.api_execution_secrets.json
}

data "aws_iam_policy_document" "worker_execution_secrets" {
  statement {
    sid       = "ReadWorkerDatabaseSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.worker_db_secret_arn]
  }

  statement {
    sid       = "DecryptWorkerSecrets"
    actions   = ["kms:Decrypt"]
    resources = distinct(concat([aws_kms_key.data.arn], var.worker_secrets_kms_key_arns))
  }
}

resource "aws_iam_role_policy" "worker_execution_secrets" {
  name   = "worker-secrets"
  role   = aws_iam_role.worker_execution.id
  policy = data.aws_iam_policy_document.worker_execution_secrets.json
}

data "aws_iam_policy_document" "migration_execution_secrets" {
  statement {
    sid     = "ReadDatabaseBootstrapSecrets"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_db_instance.database.master_user_secret[0].secret_arn,
      var.api_db_secret_arn,
      var.worker_db_secret_arn,
    ]
  }

  statement {
    sid     = "DecryptDatabaseBootstrapSecrets"
    actions = ["kms:Decrypt"]
    resources = distinct(
      concat(
        [aws_kms_key.data.arn],
        var.api_secrets_kms_key_arns,
        var.worker_secrets_kms_key_arns,
      )
    )
  }
}

resource "aws_iam_role_policy" "migration_execution_secrets" {
  name   = "database-bootstrap-secrets"
  role   = aws_iam_role.migration_execution.id
  policy = data.aws_iam_policy_document.migration_execution_secrets.json
}

resource "aws_iam_role" "api_task" {
  name               = "${local.name}-api-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "api_task" {
  statement {
    sid       = "PublishJobWakeups"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.jobs.arn]
  }

  statement {
    sid       = "EncryptJobWakeups"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.data.arn]
  }
}

resource "aws_iam_role_policy" "api_task" {
  name   = "publish-job-wakeups"
  role   = aws_iam_role.api_task.id
  policy = data.aws_iam_policy_document.api_task.json
}

resource "aws_iam_role" "worker_task" {
  name               = "${local.name}-worker-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "worker_task" {
  statement {
    sid = "ReadVersionedDatasets"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
    ]
    resources = ["${aws_s3_bucket.datasets.arn}/datasets/*"]
  }

  statement {
    sid = "ReadWriteRunArtifacts"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:PutObject",
      "s3:AbortMultipartUpload",
    ]
    resources = ["${aws_s3_bucket.artifacts.arn}/runs/*"]
  }

  statement {
    sid       = "ListDatasetPrefix"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.datasets.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["datasets/*"]
    }
  }

  statement {
    sid = "ConsumeJobWakeups"
    actions = [
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:ReceiveMessage",
    ]
    resources = [aws_sqs_queue.jobs.arn]
  }

  statement {
    sid = "DataKeyOperations"
    actions = [
      "kms:Decrypt",
      "kms:Encrypt",
      "kms:GenerateDataKey",
    ]
    resources = [aws_kms_key.data.arn]
  }
}

resource "aws_iam_role_policy" "worker_task" {
  name   = "application-data"
  role   = aws_iam_role.worker_task.id
  policy = data.aws_iam_policy_document.worker_task.json
}
