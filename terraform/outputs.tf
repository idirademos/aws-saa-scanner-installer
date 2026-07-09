output "secret_name" {
  description = "Name of the Secrets Manager secret containing CyberArk credentials"
  value       = aws_secretsmanager_secret.idira_credentials.name
}

output "secret_arn" {
  description = "ARN of the Secrets Manager secret"
  value       = aws_secretsmanager_secret.idira_credentials.arn
}

output "glue_job_name" {
  description = "Name of the Glue job"
  value       = aws_glue_job.discovery.name
}

output "glue_job_arn" {
  description = "ARN of the Glue job"
  value       = aws_glue_job.discovery.arn
}

output "s3_bucket_name" {
  description = "Name of the S3 bucket containing scripts"
  value       = aws_s3_bucket.scripts.id
}

output "s3_bucket_arn" {
  description = "ARN of the S3 bucket"
  value       = aws_s3_bucket.scripts.arn
}

output "iam_role_name" {
  description = "Name of the IAM role used by the Glue job"
  value       = aws_iam_role.glue_job_role.name
}

output "iam_role_arn" {
  description = "ARN of the IAM role"
  value       = aws_iam_role.glue_job_role.arn
}

output "trigger_name" {
  description = "Name of the Glue trigger"
  value       = aws_glue_trigger.schedule.name
}

output "aws_region" {
  description = "AWS region where resources are deployed"
  value       = data.aws_region.current.name
}

output "console_urls" {
  description = "AWS Console URLs for managing the deployment"
  value = {
    secrets_manager = "https://console.aws.amazon.com/secretsmanager/secret?name=${aws_secretsmanager_secret.idira_credentials.name}&region=${data.aws_region.current.name}"
    glue_job        = "https://${data.aws_region.current.name}.console.aws.amazon.com/gluestudio/home?region=${data.aws_region.current.name}#/editor/job/${aws_glue_job.discovery.name}/details"
    glue_runs       = "https://${data.aws_region.current.name}.console.aws.amazon.com/gluestudio/home?region=${data.aws_region.current.name}#/editor/job/${aws_glue_job.discovery.name}/runs"
    s3_bucket       = "https://s3.console.aws.amazon.com/s3/buckets/${aws_s3_bucket.scripts.id}?region=${data.aws_region.current.name}"
  }
}

output "sns_topic_arn" {
  description = "ARN of the SNS topic for alerts (if enabled)"
  value       = var.enable_sns_notifications ? aws_sns_topic.glue_alerts[0].arn : null
}

output "next_steps" {
  description = "Next steps after deployment"
  value       = <<-EOT

    ✅ Deployment completed successfully!

    Next Steps:
    1. ${var.create_idira_service_user ? "✅ Credentials configured" : "🔐 If necessary update CyberArk credentials at: ${aws_secretsmanager_secret.idira_credentials.name}"}
    2. 🚀 Start a test run: aws glue start-job-run --job-name ${aws_glue_job.discovery.name} --region ${data.aws_region.current.name}
    3. 📊 View job runs: ${format("https://%s.console.aws.amazon.com/gluestudio/home?region=%s#/editor/job/%s/runs", data.aws_region.current.name, data.aws_region.current.name, aws_glue_job.discovery.name)}
    4. 📅 Schedule: ${var.schedule_expression} (${var.enable_trigger ? "enabled" : "disabled"})
    ${var.enable_sns_notifications ? "5. 📧 SNS notifications: ${length(var.sns_email_endpoints)} email(s) subscribed (confirm subscription emails)" : ""}

  EOT
}
