output "uploads_bucket" {
  value = aws_s3_bucket.uploads.id
}

output "ecr_urls" {
  value = { for name, repo in aws_ecr_repository.repo : name => repo.repository_url }
}