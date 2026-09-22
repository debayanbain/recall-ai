variable "region" {
  description = "aws region. everyting live here"
  type = string
  default = "ap-south-1"
}

variable "project" {
  description = "prefix on every resource name"
  type = string
  default = "recallai"
}

variable "alert_email" {
  description = "Alerts send on this email"
  type = string
}

variable "monthly_budget_usd" {
  description = "aws cost monthly budget"
  type = number
  default = 30
}