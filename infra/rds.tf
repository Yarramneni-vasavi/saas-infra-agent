resource "aws_db_subnet_group" "rds_subnets" {
  name       = "${var.project}-${var.environment}-rds-subnets"
  subnet_ids = []

  tags = {
    Project     = var.project
    Environment = var.environment
  }
}

resource "aws_db_instance" "postgres" {
  identifier = "${var.project}-${var.environment}-db"
  engine     = "postgres"
  instance_class = "db.t3.micro"
  allocated_storage = 20
  name     = "todos"
  username = "todo_admin"
  password = random_password.db_password.result
  skip_final_snapshot = true

  db_subnet_group_name = aws_db_subnet_group.rds_subnets.name

  tags = {
    Project     = var.project
    Environment = var.environment
  }
}

resource "random_password" "db_password" {
  length  = 16
  special = true
}
