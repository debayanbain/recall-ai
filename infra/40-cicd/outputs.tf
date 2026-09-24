output "deploy_role_arn" {
  description = "Set this as the repository variable AWS_DEPLOY_ROLE_ARN in GitHub"
  value       = aws_iam_role.deploy.arn
}

output "oidc_provider_arn" {
  value = local.oidc_provider_arn
}

output "github_setup" {
  description = "Copy-paste to finish the GitHub side"
  value       = "gh variable set AWS_DEPLOY_ROLE_ARN --body ${aws_iam_role.deploy.arn} --repo ${var.github_owner}/${var.github_repo}"
}
