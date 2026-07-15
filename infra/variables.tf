variable "project" {
  type    = string
  default = "todo-spa"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "lambda_memory_mb" {
  type    = number
  default = 512
}

variable "lambda_timeout" {
  type    = number
  default = 10
}
