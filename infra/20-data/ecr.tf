# ── ECR repos ──────────────────────────────────────────────
# These ALREADY exist (you made them by hand). imports.tf adopts them.
# Settings here must match what you clicked: IMMUTABLE + AES256.
resource "aws_ecr_repository" "repo" {
  for_each = toset(var.ecr_repos)

  name                 = each.value
  image_tag_mutability = "IMMUTABLE"

  # Destroy fails if images are inside. Images can be rebuilt by CI.
  force_delete = false

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

# Keeps image storage cost low. NEW — Terraform creates this.
resource "aws_ecr_lifecycle_policy" "repo" {
  for_each   = aws_ecr_repository.repo
  repository = each.value.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Delete untagged images after 1 day"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep only the newest ${var.ecr_keep_images} images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = var.ecr_keep_images
        }
        action = { type = "expire" }
      }
    ]
  })
}