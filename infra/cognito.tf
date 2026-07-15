resource "aws_cognito_user_pool" "user_pool" {
  name = "${var.project}-${var.environment}-users"

  schema {
    name = "email"
    attribute_data_type = "String"
    required = true
    mutable = false
  }

  auto_verified_attributes = ["email"]
}

resource "aws_cognito_user_pool_client" "spa_client" {
  name         = "${var.project}-${var.environment}-spa-client"
  user_pool_id = aws_cognito_user_pool.user_pool.id

  explicit_auth_flows = ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]

  allowed_oauth_flows_user_pool_client = false
  generate_secret                      = false

  prevent_user_existence_errors = "ENABLED"
}
