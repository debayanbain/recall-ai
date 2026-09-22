# Session 4 (the EC2 node) needs the subnet id and the
# security group id. This is how it gets them.
resource "aws_ssm_parameter" "vpc_id" {
  name  = "/${var.project}/network/vpc_id"
  type  = "String"
  value = aws_vpc.main.id
}

resource "aws_ssm_parameter" "public_subnet_id" {
  name  = "/${var.project}/network/public_subnet_id"
  type  = "String"
  value = aws_subnet.public.id
}

resource "aws_ssm_parameter" "node_sg_id" {
  name  = "/${var.project}/network/node_sg_id"
  type  = "String"
  value = aws_security_group.node.id
}