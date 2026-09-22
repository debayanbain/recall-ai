# ── VPC ─────────────────────────────────────────────────────
# Console: VPC → Create VPC → "VPC only"
resource "aws_vpc" "main" {
  cidr_block = var.vpc_cidr

  # Without these, ECR image pulls fail with DNS errors that
  # look like network errors. You checked this by hand.
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "${var.project}-vpc"
  }
}

# ── Internet Gateway ────────────────────────────────────────
# In the console this was TWO steps: create, then attach.
# In Terraform it is one resource — `vpc_id` IS the attachment.
resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${var.project}-igw"
  }
}

# ── Public subnet ───────────────────────────────────────────
resource "aws_subnet" "public" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = var.public_subnet_cidr
  availability_zone = var.az

  # Instances launched here get a public IP automatically.
  map_public_ip_on_launch = true

  tags = {
    Name = "${var.project}-public-a"
  }
}

# ── Route table ─────────────────────────────────────────────
# Note: the `local` route (10.0.0.0/16 → local) is NOT here.
# AWS creates it and it cannot be edited or deleted, so
# Terraform does not manage it. You saw it in the CLI output.
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = {
    Name = "${var.project}-public-rt"
  }
}

# ── The association ─────────────────────────────────────────
# THIS is what makes the subnet public. The step everyone skips.
resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}