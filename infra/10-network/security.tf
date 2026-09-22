resource "aws_security_group" "node" {
  name        = "${var.project}-node-sg"
  description = "K3s node - web in, all out"
  vpc_id      = aws_vpc.main.id

  tags = {
    Name = "${var.project}-node-sg"
  }
}

# Port 80: Let's Encrypt needs it for the HTTP-01 challenge,
# and Traefik uses it to redirect visitors to HTTPS.
resource "aws_vpc_security_group_ingress_rule" "http" {
  security_group_id = aws_security_group.node.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
  description       = "Lets Encrypt HTTP-01 and redirect"
}

resource "aws_vpc_security_group_ingress_rule" "https" {
  security_group_id = aws_security_group.node.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
  description       = "App Traffic"
}

# "-1" means every protocol. You saw this exact value in the
# CLI output — the console shows a friendly label, the API
# shows the truth.
resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.node.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# NO port 22. SSM Session Manager works over an outbound
# connection, so there is nothing to open.