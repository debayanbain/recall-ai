# ── Trust policy: only EC2 may use this role ───────────────
data "aws_iam_policy_document" "ec2_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

# ── The role (IMPORTED — you made it by hand) ──────────────
resource "aws_iam_role" "node" {
  name               = "${var.project}-node-role"
  description        = var.role_description
  assume_role_policy = data.aws_iam_policy_document.ec2_trust.json
}

# ── SSM permission (IMPORTED — you attached it by hand) ────
# This is what makes Session Manager work. No SSH needed.
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# ── Instance profile (IMPORTED — the console made it silently) ─
resource "aws_iam_instance_profile" "node" {
  name = aws_iam_role.node.name
  role = aws_iam_role.node.name
}

# ── ECR pull permission (NEW) ──────────────────────────────
# Lets the node download your Docker images. Read only: the node
# can never push or delete images.
resource "aws_iam_role_policy_attachment" "ecr_read" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

# ── S3 permission for the uploads bucket ONLY (NEW) ────────
# Least privilege: this bucket, these actions, nothing else.
# The node can NOT touch the Terraform state bucket.
data "aws_iam_policy_document" "uploads" {
  # Bucket-level actions (the resource is the bucket itself)
  statement {
    sid    = "ListUploadsBucket"
    effect = "Allow"
    actions = [
      "s3:ListBucket",
      "s3:ListBucketVersions",
    ]
    resources = [data.aws_ssm_parameter.uploads_bucket_arn.value]
  }

  # Object-level actions (the resource is "every object in the bucket")
  statement {
    sid    = "ReadWriteUploadObjects"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion", # your app deletes every version on "delete forever"
    ]
    resources = ["${data.aws_ssm_parameter.uploads_bucket_arn.value}/*"]
  }
}

resource "aws_iam_role_policy" "uploads" {
  name   = "${var.project}-uploads-rw"
  role   = aws_iam_role.node.id
  policy = data.aws_iam_policy_document.uploads.json
}