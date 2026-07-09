# CyberArk Discovery Scanner - Terraform Deployment

This directory contains Terraform configuration to deploy the CyberArk Discovery Scanner infrastructure on AWS.

## Overview

The CyberArk Discovery Scanner is an AWS Glue job that automatically discovers AWS Bedrock Agents and Bedrock AgentCore resources across all AWS regions and uploads the discovery data to CyberArk's cloud infrastructure.

**What gets deployed:**
- AWS Glue Job (Python Shell 3.9) for resource discovery
- AWS Glue Trigger for scheduled execution
- IAM Role with permissions for Bedrock, Secrets Manager, S3, and CloudWatch Logs
- AWS Secrets Manager Secret for CyberArk credentials
- S3 Bucket with versioning, encryption, and public access blocks
- Optional: SNS Topic for job failure notifications

## Prerequisites

1. **Terraform** version 1.5.0 or later
   ```bash
   terraform version
   ```

2. **AWS CLI** configured with appropriate credentials
   ```bash
   aws sts get-caller-identity
   ```

3. **AWS Permissions** - Your AWS credentials need permissions to create:
   - S3 buckets and objects
   - IAM roles and policies
   - AWS Glue jobs and triggers
   - AWS Secrets Manager secrets
   - CloudWatch Events rules (if using SNS notifications)
   - SNS topics and subscriptions (if using SNS notifications)

4. **CyberArk Credentials** - Service user account with:
   - Tenant name
   - Username
   - Password

## Quick Start

### 1. Configure Variables

Copy the example tfvars file:

```bash
cd terraform/
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars` with your configuration.

### 2. Initialize Terraform

```bash
terraform init
```

This downloads the required provider plugins and prepares your working directory.

### 3. Review the Plan

```bash
terraform plan
```

Review the resources that will be created. Terraform will show you exactly what changes it will make.

### 4. Deploy the Infrastructure

```bash
terraform apply
```

Type `yes` when prompted to confirm the deployment.

### 5. Configure Idira Credentials

If you didn't provide credentials via variables or set `create_idira_service_user` to true, update the secret manually:

```bash
aws secretsmanager update-secret \
  --secret-id $(terraform output -raw secret_name) \
  --secret-string '{"tenant_name":"your-tenant","username":"your-user","password":"your-password"}' \
  --region $(terraform output -raw aws_region)
```

### 6. Test the Deployment

Start a manual Glue job run:

```bash
aws glue start-job-run \
  --job-name $(terraform output -raw glue_job_name) \
  --region $(terraform output -raw aws_region)
```

Monitor the job in the AWS Console using the URL from `terraform output console_urls`.

## Configuration Options

### Required Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `aws_region` | AWS region for deployment | `"us-east-1"` |
| `s3_bucket_name` | Globally unique S3 bucket name | `"cyberark-discovery-123456789012"` |

### Optional Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `name_prefix` | idira-scanner | Prefix for AWS resource naming |
| `create_idira_service_user` | false | Whether to create an Idira Identity service user for the scanner. Requires Idira Identity API credentials configured in the provider. If false, you can either set the credentials using the variables idira_username and idira_password or set them manually in AWS Secrets Manager after deployment. |
| `idira_tenant_name` | "" | Idira tenant name (stored in Secrets Manager). Leave empty to set manually. NOTE: If create_idira_service_user = true, this variable must be set and the specified tenant will be used for creating the service user in CyberArk Identity. |
| `idira_username` | "" | Idira username (stored in Secrets Manager). Leave empty to set manually. NOTE: If create_idira_service_user = true, this variable must be set and the specified username will be created as a service user in CyberArk Identity. |
| `idira_password` | "" | Idira password (stored in Secrets Manager). Leave empty to set manually. NOTE: If create_idira_service_user = true, a random password will be generated and the value of this variable will be ignored. |
| `glue_job_name` | `"cyberark-discovery"` | Name for the Glue job |
| `timeout` | `120` | Job timeout in minutes (1-2880) |
| `max_retries` | `0` | Maximum job retries on failure |
| `schedule_expression` | `"cron(0 10,22 * * ? *)"` | Cron expression for job schedule |
| `enable_trigger` | `true` | Enable the scheduled trigger |

### SNS Notification Variables

To receive email alerts when the Glue job fails:

```hcl
enable_sns_notifications = true
sns_email_endpoints      = ["ops@example.com", "security@example.com"]
```

**Note:** You'll need to confirm the SNS subscription via email after deployment.

### Custom Tags

```hcl
tags = {
  Application = "CyberArk-Discovery"
  Environment = "Production"
  CostCenter  = "Security"
  Owner       = "security-team@example.com"
}
```

## Outputs

After deployment, Terraform provides several useful outputs:

```bash
terraform output
```

**Available outputs:**
- `secret_name` - Name of the Secrets Manager secret
- `secret_arn` - ARN of the Secrets Manager secret
- `glue_job_name` - Name of the Glue job
- `glue_job_arn` - ARN of the Glue job
- `s3_bucket_name` - Name of the S3 bucket
- `iam_role_name` - Name of the IAM role
- `trigger_name` - Name of the Glue trigger
- `aws_region` - Deployment region
- `console_urls` - AWS Console URLs for managing resources
- `sns_topic_arn` - SNS topic ARN (if enabled)
- `next_steps` - Formatted next steps guidance

## Managing the Deployment

### View Current State

```bash
terraform show
```

### Update Configuration

1. Edit `terraform.tfvars` with your changes
2. Run `terraform plan` to preview changes
3. Run `terraform apply` to apply changes

### Destroy Resources

To remove all deployed resources:

```bash
terraform destroy
```

**Warning:** This will delete all resources including the S3 bucket and its contents, the Secrets Manager secret, and all job run history.

## Monitoring and Operations

### View Glue Job Runs

```bash
# List recent job runs
aws glue get-job-runs \
  --job-name $(terraform output -raw glue_job_name) \
  --region $(terraform output -raw aws_region)
```

### View CloudWatch Logs

```bash
# Tail logs in real-time
aws logs tail /aws-glue/python-jobs/cyberark-discovery \
  --follow \
  --region $(terraform output -raw aws_region)
```

### Start Manual Job Run

```bash
aws glue start-job-run \
  --job-name $(terraform output -raw glue_job_name) \
  --region $(terraform output -raw aws_region)
```

### Update Credentials

```bash
aws secretsmanager update-secret \
  --secret-id $(terraform output -raw secret_name) \
  --secret-string '{"tenant_name":"new-tenant","username":"new-user","password":"new-password"}' \
  --region $(terraform output -raw aws_region)
```

### Disable/Enable Scheduled Trigger

**Disable:**
```bash
aws glue update-trigger \
  --name $(terraform output -raw trigger_name) \
  --trigger-update Enabled=false \
  --region $(terraform output -raw aws_region)
```

**Enable:**
```bash
aws glue update-trigger \
  --name $(terraform output -raw trigger_name) \
  --trigger-update Enabled=true \
  --region $(terraform output -raw aws_region)
```

### S3 Bucket Security

The Terraform configuration automatically enables:
- ✅ Server-side encryption (AES256)
- ✅ Versioning (for audit trail)
- ✅ Public access blocks (prevents accidental exposure)

## Additional Resources

- [AWS Glue Documentation](https://docs.aws.amazon.com/glue/)
- [Terraform AWS Provider Documentation](https://registry.terraform.io/providers/hashicorp/aws/latest/docs)
- [AWS Bedrock Documentation](https://docs.aws.amazon.com/bedrock/)
- [Terraform Best Practices](https://www.terraform-best-practices.com/)

## Support

For issues specific to:
- **Terraform configuration**: Check this README and Terraform documentation
- **AWS Glue job execution**: Review CloudWatch Logs and AWS Glue console
- **CyberArk integration**: Contact CyberArk support with discovery data format questions
- **AWS infrastructure**: Check AWS service documentation and support
