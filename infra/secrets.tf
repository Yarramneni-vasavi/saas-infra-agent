resource "aws_secretsmanager_secret" "db_secret" {
  name = "${var.project}-${var.environment}-db-secret"

  tags = {
    Project     = var.project
    Environment = var.environment
  }
}

resource "aws_secretsmanager_secret_version" "db_secret_version" {
  secret_id     = aws_secretsmanager_secret.db_secret.id
  secret_string = jsonencode({
    username = aws_db_instance.postgres.username,
    password = aws_db_instance.postgres.password,
    host     = aws_db_instance.postgres.address,
    port     = aws_db_instance.postgres.port,
    dbname   = aws_db_instance.postgres.name
  })
}
