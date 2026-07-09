# Required Variables
variable "aws_region" {
  description = "AWS region for deployment"
  type        = string
}

variable "s3_bucket_name" {
  description = "S3 bucket name for Glue scripts and dependencies (must be globally unique)"
  type        = string
}

variable "name_prefix" {
  description = "Prefix for resource naming"
  type        = string
  default     = "idira-scanner"
}

# Optional Variables - Core Configuration
variable "glue_job_name" {
  description = "Name for the Glue job"
  type        = string
  default     = ""
}

variable "script_s3_key" {
  description = "S3 key path for the Glue job script"
  type        = string
  default     = "idira-scanner/discovery.py"
}

variable "dependencies_s3_key" {
  description = "S3 key path for the dependencies zip file"
  type        = string
  default     = "idira-scanner/dependencies.zip"
}

variable "max_retries" {
  description = "Maximum number of retries for the Glue job"
  type        = number
  default     = 0
}

variable "timeout" {
  description = "Job timeout in minutes (max 2880 = 48 hours)"
  type        = number
  default     = 120

  validation {
    condition     = var.timeout >= 1 && var.timeout <= 2880
    error_message = "Timeout must be between 1 and 2880 minutes."
  }
}

variable "schedule_expression" {
  description = "Cron expression for Glue trigger schedule (AWS cron format)"
  type        = string
  default     = "cron(0 10,22 * * ? *)"
}

variable "enable_trigger" {
  description = "Whether to enable the Glue trigger on creation"
  type        = bool
  default     = true
}

# File Paths
variable "discovery_script_path" {
  description = "Local path to discovery.py script"
  type        = string
  default     = "./discovery.py"
}

variable "dependencies_zip_path" {
  description = "Local path to dependencies.zip file"
  type        = string
  default     = "./dependencies.zip"
}

# -----------------------------------------------------------------------------
# CyberArk Secrets (optional - can be set post-deployment)
# -----------------------------------------------------------------------------

variable "create_idira_service_user" {
  description = "Whether to create an Idira Identity service user for the scanner. Requires Idira Identity API credentials configured in the provider. If false, you can either set the credentials using the variables idira_username and idira_password or set them manually in AWS Secrets Manager after deployment."
  type        = bool
  default     = false
}

variable "idira_tenant_name" {
  description = "Idira tenant name (stored in Secrets Manager). Leave empty to set manually. NOTE: If create_idira_service_user = true, this variable must be set and the specified tenant will be used for creating the service user in CyberArk Identity."
  default     = ""
  type        = string
  sensitive   = true
}

variable "idira_username" {
  description = "Idira username (stored in Secrets Manager). Leave empty to set manually. NOTE: If create_idira_service_user = true, this variable must be set and the specified username will be created as a service user in CyberArk Identity."
  type        = string
  default     = ""
  sensitive   = true
}

variable "idira_password" {
  description = "Idira password (stored in Secrets Manager). Leave empty to set manually. NOTE: If create_idira_service_user = true, a random password will be generated and the value of this variable will be ignored."
  type        = string
  default     = ""
  sensitive   = true
}

# SNS Notifications (Optional)
variable "enable_sns_notifications" {
  description = "Enable SNS notifications for Glue job failures"
  type        = bool
  default     = false
}

variable "sns_email_endpoints" {
  description = "List of email addresses to receive SNS notifications (requires enable_sns_notifications = true)"
  type        = list(string)
  default     = []
}

# Tags
variable "tags" {
  description = "Additional tags to apply to all resources"
  type        = map(string)
  default = {
    Application = "Idira-Scanner"
    ManagedBy   = "Terraform"
  }
}
