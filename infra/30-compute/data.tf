# ── From 10-network (published in SSM) ─────────────────────
data "aws_ssm_parameter" "subnet_id" {
  name = "/${var.project}/network/public_subnet_id"
}

data "aws_ssm_parameter" "node_sg_id" {
  name = "/${var.project}/network/node_sg_id"
}

# ── From 20-data (published in SSM) ────────────────────────
data "aws_ssm_parameter" "uploads_bucket_arn" {
  name = "/${var.project}/data/uploads_bucket_arn"
}

# ── Latest Ubuntu 24.04 ARM64 image, published by Canonical ─
# Never hardcode an AMI id: it differs per region and gets old.
data "aws_ssm_parameter" "ubuntu_ami" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id"
}