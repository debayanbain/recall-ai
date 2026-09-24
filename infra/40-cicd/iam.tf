# ── Who may assume the deploy role ─────────────────────────
# Both conditions are load-bearing and neither is optional:
#   aud  -- without it, any OIDC token GitHub ever mints for any audience works.
#   sub  -- without it, ANY repository on github.com can assume this role. That is
#           not an exaggeration: the provider trusts GitHub, not your account.
data "aws_iam_policy_document" "trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # StringEquals, not StringLike: an exact list, no pattern to get wrong. It is also
    # case-sensitive and format-sensitive -- see `local.subjects` and github_owner_id.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = local.subjects
    }
  }
}

resource "aws_iam_role" "deploy" {
  name                 = var.role_name
  description          = "GitHub Actions: build, push to ECR, roll out on K3s"
  assume_role_policy   = data.aws_iam_policy_document.trust.json
  max_session_duration = 3600

  # The variable's own validation only covers an explicit list; this covers the
  # derived one too, so no path reaches a trust policy with a wildcard subject.
  lifecycle {
    precondition {
      condition     = alltrue([for s in local.subjects : !strcontains(s, "*")])
      error_message = "A wildcard subject would let any repository assume this role."
    }
  }
}

# ── What the role may do ───────────────────────────────────
# Deliberately NOT in here: terraform state, IAM, EC2, the uploads bucket, and
# /recallai/app/* (the application's real secrets -- OpenAI keys, the database
# URL, the Telegram token). CI deploys an image; it has never needed to read
# them. Infrastructure changes stay a human running terraform locally.
data "aws_iam_policy_document" "deploy" {
  # ECR's auth endpoint is account-wide by API design; it cannot be scoped.
  # It only mints a token whose reach is the statement below.
  statement {
    sid       = "EcrAuth"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "EcrPushPull"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
      "ecr:DescribeImages",
      "ecr:DescribeRepositories",
    ]
    resources = [for r in data.aws_ecr_repository.repo : r.arn]
  }

  # Two parameters, by name. Not the path, and never with a trailing wildcard
  # that would reach /recallai/app/*.
  statement {
    sid     = "ReadDeployParameters"
    effect  = "Allow"
    actions = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = [
      "${local.ssm_param_prefix}/${var.project}/compute/instance_id",
      "${local.ssm_param_prefix}/${var.project}/cicd/kubeconfig",
      "${local.ssm_param_prefix}/${var.project}/data/ecr/*",
    ]
  }

  # Needed only to decrypt the kubeconfig SecureString above. ViaService pins it
  # to SSM, so the key cannot be used to decrypt anything else in the account.
  statement {
    sid       = "DecryptSsmSecureStrings"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = [data.aws_kms_alias.ssm.target_key_arn]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.region}.amazonaws.com"]
    }
  }

  # The tunnel to the K3s API. Scoped to ONE instance and ONE document:
  # AWS-StartPortForwardingSession forwards a port and cannot open a shell.
  # SessionDocumentAccessCheck is what makes the document half enforced --
  # without it the document ARN in `resources` is decorative.
  statement {
    sid     = "PortForwardToNode"
    effect  = "Allow"
    actions = ["ssm:StartSession"]
    resources = [
      local.instance_arn,
      "arn:${data.aws_partition.current.partition}:ssm:${var.region}::document/AWS-StartPortForwardingSession",
    ]

    condition {
      test     = "BoolIfExists"
      variable = "ssm:SessionDocumentAccessCheck"
      values   = ["true"]
    }
  }

  # Closing its own tunnel at the end of the job. A session ARN is unguessable
  # and terminating one only ends a port forward, so this is the one statement
  # here that is not narrowed further.
  statement {
    sid       = "EndOwnSession"
    effect    = "Allow"
    actions   = ["ssm:TerminateSession"]
    resources = ["arn:${data.aws_partition.current.partition}:ssm:*:*:session/*"]
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "${var.project}-github-deploy"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}
