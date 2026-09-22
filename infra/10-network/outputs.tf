output "vpc_id"           { value = aws_vpc.main.id }
output "public_subnet_id" { value = aws_subnet.public.id }
output "node_sg_id"       { value = aws_security_group.node.id }
output "igw_id"           { value = aws_internet_gateway.main.id }
output "route_table_id"   { value = aws_route_table.public.id }
