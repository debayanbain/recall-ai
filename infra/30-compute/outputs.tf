output "instance_id" {
  value = aws_instance.node.id
}

output "public_ip" {
  value = aws_eip.node.public_ip
}

output "private_ip" {
  value = aws_instance.node.private_ip
}

output "ssm_command" {
  description = "Copy-paste this to get a shell on the node"
  value       = "aws ssm start-session --target ${aws_instance.node.id} --region ${var.region}"
}