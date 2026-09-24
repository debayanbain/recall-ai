data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

# From 30-compute. The deploy tunnels to the K3s API on THIS instance and
# nothing else -- the role's ssm:StartSession is scoped to this ARN.
data "aws_ssm_parameter" "instance_id" {
  name = "/${var.project}/compute/instance_id"
}

# From 20-data. Push is scoped to these ARNs, never to "*".
data "aws_ecr_repository" "repo" {
  for_each = toset(var.ecr_repos)
  name     = each.value
}

# SecureString parameters are encrypted with the AWS-managed SSM key. Reading one
# needs kms:Decrypt on that key as well as ssm:GetParameter.
data "aws_kms_alias" "ssm" {
  name = "alias/aws/ssm"
}

locals {
  # "owner/repo", or "owner@id/repo@id" when the account mints immutable-id subjects.
  repo_claim = (
    var.github_owner_id != "" && var.github_repo_id != ""
    ? "${var.github_owner}@${var.github_owner_id}/${var.github_repo}@${var.github_repo_id}"
    : "${var.github_owner}/${var.github_repo}"
  )

  # `build` has no environment, so its claim is the ref one; `deploy` names
  # environment: production, and GitHub then puts THAT in the sub instead of the ref.
  # Both are needed and neither is a superset of the other.
  subjects = length(var.allowed_subjects) > 0 ? var.allowed_subjects : [
    "repo:${local.repo_claim}:ref:refs/heads/${var.github_branch}",
    "repo:${local.repo_claim}:environment:production",
  ]

  instance_arn = join("", [
    "arn:${data.aws_partition.current.partition}:ec2:${var.region}:",
    "${data.aws_caller_identity.current.account_id}:instance/",
    nonsensitive(data.aws_ssm_parameter.instance_id.value),
  ])

  ssm_param_prefix = "arn:${data.aws_partition.current.partition}:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter"
}
