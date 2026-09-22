variable "region" {
  type    = string
  default = "ap-south-1"
}

variable "project" {
  type    = string
  default = "recallai"
}

variable "vpc_cidr" {
  description = "Private address range for the whole VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "public_subnet_cidr" {
  description = "Slice of the VPC for the public subnet"
  type        = string
  default     = "10.0.1.0/24"
}

variable "az" {
  description = "Availability Zone. A subnet lives in exactly one."
  type        = string
  default     = "ap-south-1a"
}