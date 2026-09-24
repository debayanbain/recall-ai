# Same state bucket as every other layer. New key = this layer's own state file,
# so a mistake here cannot touch 30-compute's record of the server.
terraform {
  backend "s3" {
    bucket       = "recallai-tfstate-9eb36356"
    key          = "40-cicd/terraform.tfstate"
    region       = "ap-south-1"
    encrypt      = true
    use_lockfile = true
  }
}
