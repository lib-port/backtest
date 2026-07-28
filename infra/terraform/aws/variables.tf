variable "aws_region" {
  type        = string
  description = "AWS region for the complete deployment."
  default     = "ap-southeast-2"
}

variable "project_name" {
  type        = string
  description = "Lowercase name used to prefix resources."
  default     = "quant-backtester"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,30}$", var.project_name))
    error_message = "project_name must be a lowercase DNS-compatible name."
  }
}

variable "environment" {
  type        = string
  description = "Deployment environment."
  default     = "production"

  validation {
    condition     = contains(["development", "staging", "production"], var.environment)
    error_message = "environment must be development, staging, or production."
  }
}

variable "vpc_cidr" {
  type        = string
  description = "CIDR for the application VPC."
  default     = "10.20.0.0/16"
}

variable "container_image" {
  type        = string
  description = "Immutable ECR/OCI image reference including @sha256 digest."

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.container_image))
    error_message = "container_image must be pinned by sha256 digest."
  }
}

variable "certificate_arn" {
  type        = string
  description = "ACM certificate ARN for the HTTPS listener."
}

variable "api_hostname" {
  type        = string
  description = "DNS hostname covered by certificate_arn, for example backtests.example.com."

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9.-]+[A-Za-z0-9]$", var.api_hostname))
    error_message = "api_hostname must be a valid DNS hostname."
  }
}

variable "route53_zone_id" {
  type        = string
  description = "Optional Route 53 hosted zone ID in which to create the API alias."
  default     = null
  nullable    = true
}

variable "auth_mode" {
  type        = string
  description = "Control-plane authentication mode."
  default     = "oidc"

  validation {
    condition     = contains(["api-key", "oidc"], var.auth_mode)
    error_message = "Production auth_mode must be api-key or oidc."
  }
}

variable "api_keys_secret_arn" {
  type        = string
  description = "Optional Secrets Manager ARN whose secret value is QBT_API_KEYS_JSON."
  default     = null
  nullable    = true
}

variable "oidc_issuer" {
  type        = string
  description = "OIDC token issuer when auth_mode is oidc."
  default     = ""
}

variable "oidc_audience" {
  type        = string
  description = "OIDC audience when auth_mode is oidc."
  default     = ""
}

variable "oidc_jwks_url" {
  type        = string
  description = "OIDC JWKS endpoint when auth_mode is oidc."
  default     = ""
}

variable "db_name" {
  type    = string
  default = "quantdb"
}

variable "db_username" {
  type    = string
  default = "quant"
}

variable "application_db_secret_arn" {
  type        = string
  description = "Secrets Manager ARN containing JSON username/password for the non-owner runtime role."

  validation {
    condition     = can(regex("^arn:[^:]+:secretsmanager:", var.application_db_secret_arn))
    error_message = "application_db_secret_arn must be a Secrets Manager ARN."
  }
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.small"
}

variable "db_allocated_storage" {
  type    = number
  default = 50
}

variable "db_max_allocated_storage" {
  type    = number
  default = 500
}

variable "db_multi_az" {
  type        = bool
  description = "Enable a synchronous standby. Keep true in production."
  default     = true
}

variable "dataset_retention_days" {
  type        = number
  description = "Default S3 Object Lock governance retention for dataset versions."
  default     = 30

  validation {
    condition     = var.dataset_retention_days >= 1
    error_message = "dataset_retention_days must be at least one day."
  }
}

variable "bootstrap_mode" {
  type        = bool
  description = "Keep API and worker services at zero while the first database migration task runs."
  default     = false
}

variable "api_desired_count" {
  type    = number
  default = 2
}

variable "worker_desired_count" {
  type    = number
  default = 2
}

variable "worker_max_count" {
  type    = number
  default = 20
}

variable "waf_requests_per_five_minutes" {
  type        = number
  description = "Per-IP API request limit enforced by AWS WAF."
  default     = 2000

  validation {
    condition     = var.waf_requests_per_five_minutes >= 100
    error_message = "waf_requests_per_five_minutes must be at least 100."
  }
}

variable "api_cpu" {
  type    = number
  default = 512
}

variable "api_memory" {
  type    = number
  default = 1024
}

variable "worker_cpu" {
  type    = number
  default = 2048
}

variable "worker_memory" {
  type    = number
  default = 4096
}

variable "log_retention_days" {
  type    = number
  default = 90
}

variable "alarm_sns_topic_arn" {
  type        = string
  description = "Optional SNS topic for operational alarms."
  default     = null
  nullable    = true
}

variable "otlp_endpoint" {
  type        = string
  description = "Optional HTTPS OTLP endpoint for application traces."
  default     = ""
}

variable "tags" {
  type        = map(string)
  description = "Additional tags applied to resources."
  default     = {}
}
