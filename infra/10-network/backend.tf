terraform {
  backend "s3" {
    bucket       = "recallai-tfstate-9eb36356"
    key          = "10-network/terraform.tfstate"
    region       = "ap-south-1"
    encrypt      = true
    use_lockfile = true
  }
}