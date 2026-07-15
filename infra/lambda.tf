resource "aws_lambda_function" "api_handler" {
  function_name = "${var.project}-${var.environment}-api"
  role          = aws_iam_role.lambda_exec.arn

  filename         = "function.zip"
  source_code_hash = filebase64sha256("function.zip")

  handler = "handler.handler"
  runtime = "python3.11"

  memory_size = var.lambda_memory_mb
  timeout     = var.lambda_timeout

  environment {
    variables = {
      ENVIRONMENT = var.environment
      PROJECT     = var.project
    }
  }

  tags = {
    Project     = var.project
    Environment = var.environment
  }
}

resource "aws_lambda_permission" "apigw_invoke" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api_handler.function_name
  principal     = "apigateway.amazonaws.com"
}
