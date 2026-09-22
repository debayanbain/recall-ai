resource "aws_instance" "node" {
  ami                    = data.aws_ssm_parameter.ubuntu_ami.value
  instance_type          = var.instance_type
  subnet_id              = data.aws_ssm_parameter.subnet_id.value
  vpc_security_group_ids = [data.aws_ssm_parameter.node_sg_id.value]
  iam_instance_profile   = aws_iam_instance_profile.node.name

  # No key_name. No SSH. Access is only through SSM.

  root_block_device {
    volume_size           = var.root_volume_gb
    volume_type           = "gp3" # always gp3: cheaper and faster than gp2
    encrypted             = true
    delete_on_termination = true
  }

  # IMDSv2 required: blocks the SSRF attack behind the Capital One breach.
  # Hop limit 2: pods run one network hop away from the host, so they
  # need 2 to reach the metadata service. With 1, pods get no credentials.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  user_data                   = file("${path.module}/user_data.sh")
  user_data_replace_on_change = true

  # Stop an accidental API call from deleting the server.
  # Terraform can still replace it on purpose.
  disable_api_termination = false

  lifecycle {
    # Canonical publishes a new Ubuntu AMI every few weeks.
    # Without this line, the next `terraform plan` after a new AMI
    # would want to DESTROY and REBUILD your server.
    ignore_changes = [ami]
  }

  tags = {
    Name = "${var.project}-k3s-node"
  }
}

# ── Elastic IP: a public address that never changes ────────
resource "aws_eip" "node" {
  domain   = "vpc"
  instance = aws_instance.node.id

  tags = {
    Name = "${var.project}-eip"
  }
}