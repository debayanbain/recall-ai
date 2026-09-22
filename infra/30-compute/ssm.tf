# Published for GitHub Actions (Session 5/8): it deploys by
# sending commands to this instance id through SSM.
resource "aws_ssm_parameter" "instance_id" {
  name  = "/${var.project}/compute/instance_id"
  type  = "String"
  value = aws_instance.node.id
}

resource "aws_ssm_parameter" "public_ip" {
  name  = "/${var.project}/compute/public_ip"
  type  = "String"
  value = aws_eip.node.public_ip
}