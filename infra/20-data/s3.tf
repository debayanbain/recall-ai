# ── The uploads bucket (replaces Backblaze B2) ─────────────
# This bucket does NOT exist yet. Terraform CREATES it.
resource "aws_s3_bucket" "uploads" {
  bucket = local.bucket

  # User files cannot be rebuilt. `terraform destroy` will refuse.
  lifecycle {
    prevent_destroy = true
  }
}

# ACLs disabled: only IAM policies decide access.
resource "aws_s3_bucket_ownership_controls" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Never public. The app gives files out with presigned URLs.
resource "aws_s3_bucket_public_access_block" "uploads" {
  bucket                  = aws_s3_bucket.uploads.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Keep old versions, so a bad deploy can be undone.
resource "aws_s3_bucket_versioning" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Encrypt everything at rest.
resource "aws_s3_bucket_server_side_encryption_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Cleanup rules, so storage cost does not grow forever.
resource "aws_s3_bucket_lifecycle_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id

  # This rule only makes sense after versioning is on.
  depends_on = [aws_s3_bucket_versioning.uploads]

  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 7
    }

    expiration {
      expired_object_delete_marker = true
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}