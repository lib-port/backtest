variable "aws_region" {
  type    = string
  default = "ap-southeast-1"
}

variable "project_name" {
  type    = string
  default = "quant-backtester-prod"
}

variable "allowed_cidr" {
  type        = string
  description = "CIDR allowed to access Postgres (e.g., your public IP /32)"
}

variable "db_name" {
  type    = string
  default = "quantdb"
}

variable "db_username" {
  type    = string
  default = "quant"
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "db_allocated_storage" {
  type    = number
  default = 20
}
