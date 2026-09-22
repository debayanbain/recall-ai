variable "region" {
  type    = string
  default = "ap-south-1"
}

variable "project" {
  type    = string
  default = "recallai"
}

variable "ecr_repos" {
  description = "One ECR repo per Docker image"
  type        = list(string)
  default     = ["recallai-api", "recallai-web"]
}

variable "ecr_keep_images" {
  description = "How many images to keep in each repo"
  type        = number
  default     = 10
}