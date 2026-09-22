# Published values. The compute layer and GitHub Actions read these.
resource "aws_ssm_parameter" "uploads_bucket" {
  name  = "/${var.project}/data/uploads_bucket"
  type  = "String"
  value = aws_s3_bucket.uploads.id
}

resource "aws_ssm_parameter" "uploads_bucket_arn" {
  name  = "/${var.project}/data/uploads_bucket_arn"
  type  = "String"
  value = aws_s3_bucket.uploads.arn
}

resource "aws_ssm_parameter" "ecr_url" {
  for_each = aws_ecr_repository.repo

  name  = "/${var.project}/data/ecr/${each.key}"
  type  = "String"
  value = each.value.repository_url
}