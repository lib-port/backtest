check "authentication_configuration" {
  assert {
    condition = (
      var.auth_mode == "api-key"
      ? (
        var.api_keys_secret_arn != null && var.api_keys_secret_arn != ""
        && var.api_key_pepper_secret_arn != null
        && var.api_key_pepper_secret_arn != ""
      )
      : alltrue([
        startswith(var.oidc_issuer, "https://"),
        var.oidc_audience != "",
        startswith(var.oidc_jwks_url, "https://"),
      ])
    )
    error_message = "Configure API-key and pepper secret ARNs or complete HTTPS OIDC settings."
  }
}

check "separate_database_credentials" {
  assert {
    condition = alltrue([
      var.api_db_secret_arn != aws_db_instance.database.master_user_secret[0].secret_arn,
      var.worker_db_secret_arn != aws_db_instance.database.master_user_secret[0].secret_arn,
      var.api_db_secret_arn != var.worker_db_secret_arn,
    ])
    error_message = "API, worker, and RDS owner credentials must all be separate secrets."
  }
}

check "production_high_availability" {
  assert {
    condition = var.environment != "production" || (
      var.db_multi_az
      && (
        var.bootstrap_mode
        || (var.api_desired_count >= 2 && var.worker_desired_count >= 2)
      )
    )
    error_message = "Production requires Multi-AZ RDS and at least two API and worker tasks."
  }
}

check "capacity_bounds" {
  assert {
    condition = (
      var.api_desired_count >= 0
      && var.api_desired_count <= 10
      && var.worker_desired_count >= 0
      && var.worker_max_count >= 1
      && var.worker_max_count >= var.worker_desired_count
      && var.db_max_allocated_storage >= var.db_allocated_storage
    )
    error_message = "Desired counts and maximum worker/database capacity are inconsistent."
  }
}

check "network_range" {
  assert {
    condition     = can(cidrnetmask(var.vpc_cidr))
    error_message = "vpc_cidr must be a valid IPv4 CIDR."
  }
}
