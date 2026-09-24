# ── GitHub as an identity provider ─────────────────────────
# This is what removes long-lived AWS keys from GitHub secrets. A workflow asks
# GitHub for a short-lived JWT describing itself (repo, ref, workflow) and trades
# it at STS for credentials that expire with the job. Nothing to leak, nothing to
# rotate, and a leaked repo secret is no longer an AWS credential.
resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 1 : 0

  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]

  # AWS stopped validating this for token.actions.githubusercontent.com (it now
  # trusts the host's public CA chain), but the argument is still required and a
  # wrong value is silently accepted. These are GitHub's published thumbprints.
  thumbprint_list = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
  ]
}

data "aws_iam_openid_connect_provider" "existing" {
  count = var.create_oidc_provider ? 0 : 1
  url   = "https://token.actions.githubusercontent.com"
}

locals {
  oidc_provider_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : data.aws_iam_openid_connect_provider.existing[0].arn
}
