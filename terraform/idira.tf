resource "random_password" "service_user_password" {
  count            = var.create_idira_service_user ? 1 : 0
  length           = 32
  special          = true
  override_special = "!~.,|#*-[]<>?"
}

resource "idsec_identity_user" "aws_disco_service_user" {
  count                 = var.create_idira_service_user ? 1 : 0
  username              = var.idira_username
  display_name          = "AWS Disco Service User"
  is_service_user       = true
  is_oauth_client       = true
  password              = random_password.service_user_password[0].result
  password_never_expire = true
  email                 = "notrequired@example.com" # Value only required to satisfy API validation (regression error?)
}

data "idsec_identity_role" "aws_disco_role" {
  role_name = "Discovery & Context AWS Communication"
}

resource "idsec_identity_role_member" "aws_disco_role_member" {
  count       = var.create_idira_service_user ? 1 : 0
  role_id     = data.idsec_identity_role.aws_disco_role.role_id
  member_name = idsec_identity_user.aws_disco_service_user[0].username
  member_type = "USER"
}
