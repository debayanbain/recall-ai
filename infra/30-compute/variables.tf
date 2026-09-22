variable "region" {
  type    = string
  default = "ap-south-1"
}

variable "project" {
  type    = string
  default = "recallai"
}

variable "instance_type" {
  description = "Free Plan allows only free-tier-eligible types. t4g.small = 2 vCPU, 2 GB, ARM"
  type        = string
  default     = "t4g.small"
}

variable "root_volume_gb" {
  type    = number
  default = 30
}

variable "role_description" {
  description = "Must match the text the console wrote, or Terraform will change it"
  type        = string
  default     = "Allows EC2 instances to call AWS services on your behalf."
}