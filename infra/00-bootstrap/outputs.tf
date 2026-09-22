output "state_bucket" {
  description = "Copy this into every other layer's backend.tf"
  value       = aws_s3_bucket.tfstate.id
}

output "region" {
  value = var.region
}
