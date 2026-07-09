# Data Sources
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# Variable validation
resource "null_resource" "validate_cybr_creds" {
  lifecycle {
    precondition {
      condition     = var.create_idira_service_user ? (var.idira_tenant_name != "" && var.idira_username != "") : true
      error_message = "If create_idira_service_user is true, idira_tenant_name and idira_username must be provided. If you want to provide your own credentials, set create_idira_service_user to false and provide idira_tenant_name, idira_username, and idira_password (or set them manually in AWS Secrets Manager after deployment)."
    }
  }
}

# Locals
locals {

  cybr_creds_string = var.create_idira_service_user ? jsonencode({
    tenant_name = var.idira_tenant_name
    username    = var.idira_username
    password    = random_password.service_user_password[0].result
    }) : jsonencode({
    tenant_name = var.idira_tenant_name
    username    = var.idira_username
    password    = var.idira_password
  })

  common_tags = merge(var.tags, {
    ManagedBy = "Terraform"
  })

  glue_job_name = var.glue_job_name != "" ? var.glue_job_name : "${var.name_prefix}-scanner"
  secret_name   = "${var.name_prefix}-idira-credentials"
}

# S3 Bucket for Scripts and Dependencies
resource "aws_s3_bucket" "scripts" {
  bucket = var.s3_bucket_name
  tags   = local.common_tags
}

resource "aws_s3_bucket_versioning" "scripts" {
  bucket = aws_s3_bucket.scripts.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "scripts" {
  bucket = aws_s3_bucket.scripts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "scripts" {
  bucket = aws_s3_bucket.scripts.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Upload Discovery Script to S3
resource "aws_s3_object" "discovery_script" {
  bucket = aws_s3_bucket.scripts.id
  key    = var.script_s3_key
  source = var.discovery_script_path
  etag   = filemd5(var.discovery_script_path)
  tags   = local.common_tags
}

# Upload Dependencies to S3
resource "aws_s3_object" "dependencies" {
  bucket = aws_s3_bucket.scripts.id
  key    = var.dependencies_s3_key
  source = var.dependencies_zip_path
  etag   = filemd5(var.dependencies_zip_path)
  tags   = local.common_tags
}

# Secrets Manager Secret
resource "aws_secretsmanager_secret" "idira_credentials" {
  name        = local.secret_name
  description = "idira service user credentials for discovery scanner"
  tags        = local.common_tags
}

resource "aws_secretsmanager_secret_version" "idira_credentials" {
  secret_id     = aws_secretsmanager_secret.idira_credentials.id
  secret_string = local.cybr_creds_string
}

# IAM Role for Glue Job
resource "aws_iam_role" "glue_job_role" {
  name = "${local.glue_job_name}-execution-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "glue.amazonaws.com"
      }
      Action = "sts:AssumeRole"
    }]
  })

  tags = local.common_tags
}

# IAM Policy for Glue Job
resource "aws_iam_role_policy" "glue_job_policy" {
  name = "${local.glue_job_name}-policy"
  role = aws_iam_role.glue_job_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Bedrock Agent permissions (all regions)
      {
        Effect = "Allow"
        Action = [
          "bedrock:ListAgents",
          "bedrock:GetAgent",
          "bedrock:ListAgentAliases",
          "bedrock:ListAgentVersions",
          "bedrock:ListAgentActionGroups",
          "bedrock:ListTagsForResource"
        ]
        Resource = "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:agent/*"
      },
      # Bedrock AgentCore permissions (all regions)
      {
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:ListAgentRuntimes",
          "bedrock-agentcore:ListAgentRuntimeVersions",
          "bedrock-agentcore:GetAgentRuntime",
          "bedrock-agentcore:ListAgentRuntimeEndpoints",
          "bedrock-agentcore:GetAgentRuntimeEndpoint",
          "bedrock-agentcore:ListTagsForResource"
        ]
        Resource = "arn:aws:bedrock-agentcore:*:${data.aws_caller_identity.current.account_id}:runtime/*"
      },
      # Secrets Manager permissions
      {
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = aws_secretsmanager_secret.idira_credentials.arn
      },
      # S3 permissions for script and dependencies
      {
        Effect = "Allow"
        Action = "s3:GetObject"
        Resource = [
          "${aws_s3_bucket.scripts.arn}/${var.script_s3_key}",
          "${aws_s3_bucket.scripts.arn}/${var.dependencies_s3_key}"
        ]
      },
      # CloudWatch Logs permissions
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws-glue/python-jobs/*"
      }
    ]
  })
}

# Glue Job
resource "aws_glue_job" "discovery" {
  name         = local.glue_job_name
  description  = "idira Discovery Scanner for AWS Bedrock resources"
  role_arn     = aws_iam_role.glue_job_role.arn
  glue_version = "4.0"
  max_capacity = 0.0625
  max_retries  = var.max_retries
  timeout      = var.timeout

  command {
    name            = "pythonshell"
    python_version  = "3.9"
    script_location = "s3://${aws_s3_bucket.scripts.id}/${var.script_s3_key}"
  }

  # Non-overridable arguments (security-critical)
  non_overridable_arguments = {
    "--secret_arn"     = aws_secretsmanager_secret.idira_credentials.arn
    "--aws_account_id" = data.aws_caller_identity.current.account_id
  }

  # Default arguments (can be overridden at runtime)
  default_arguments = {
    "--extra-py-files"                   = "s3://${aws_s3_bucket.scripts.id}/${var.dependencies_s3_key}"
    "--enable-metrics"                   = "true"
    "--enable-continuous-cloudwatch-log" = "true"
    "--enable-continuous-log-filter"     = "true"
  }

  tags = local.common_tags
}

# Glue Trigger (Scheduled)
resource "aws_glue_trigger" "schedule" {
  name        = "${local.glue_job_name}-schedule"
  description = "Scheduled trigger for idira Discovery"
  type        = "SCHEDULED"
  schedule    = var.schedule_expression
  enabled     = var.enable_trigger

  actions {
    job_name = aws_glue_job.discovery.name
  }

  tags = local.common_tags
}

# SNS Topic (Optional)
resource "aws_sns_topic" "glue_alerts" {
  count = var.enable_sns_notifications ? 1 : 0
  name  = "${local.glue_job_name}-alerts"
  tags  = local.common_tags
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.enable_sns_notifications ? length(var.sns_email_endpoints) : 0
  topic_arn = aws_sns_topic.glue_alerts[0].arn
  protocol  = "email"
  endpoint  = var.sns_email_endpoints[count.index]
}

# CloudWatch Event Rule for Job Failures
resource "aws_cloudwatch_event_rule" "glue_job_failed" {
  count       = var.enable_sns_notifications ? 1 : 0
  name        = "${local.glue_job_name}-failure-alert"
  description = "Detect Glue job failures and timeouts"

  event_pattern = jsonencode({
    source      = ["aws.glue"]
    detail-type = ["Glue Job State Change"]
    detail = {
      jobName = [local.glue_job_name]
      state   = ["FAILED", "TIMEOUT"]
    }
  })

  tags = local.common_tags
}

resource "aws_cloudwatch_event_target" "sns" {
  count     = var.enable_sns_notifications ? 1 : 0
  rule      = aws_cloudwatch_event_rule.glue_job_failed[0].name
  target_id = "SendToSNS"
  arn       = aws_sns_topic.glue_alerts[0].arn
}

# SNS Topic Policy (allow CloudWatch Events to publish)
resource "aws_sns_topic_policy" "glue_alerts" {
  count = var.enable_sns_notifications ? 1 : 0
  arn   = aws_sns_topic.glue_alerts[0].arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "events.amazonaws.com"
      }
      Action   = "SNS:Publish"
      Resource = aws_sns_topic.glue_alerts[0].arn
    }]
  })
}
