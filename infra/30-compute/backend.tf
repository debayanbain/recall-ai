# Same state bucket as every layer. New key = this layer's own state file.
terraform {
  backend "s3" {
    bucket       = "recallai-tfstate-9eb36356"
    key          = "30-compute/terraform.tfstate"
    region       = "ap-south-1"
    encrypt      = true
    use_lockfile = true
  }
}