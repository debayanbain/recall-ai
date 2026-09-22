data "aws_caller_identity" "current" {}

locals {
  bucket = "${var.project}-uploads-${data.aws_caller_identity.current.account_id}"
}
