# Architecture: Task-Tracker SaaS API

## Overview
A REST API for a task-tracking SaaS serving ~5,000 daily users. Deployment
target: **Terraform on AWS**.

## Stack
- **Compute**: ECS Fargate service (2 tasks, 0.5 vCPU / 1 GB each) behind an
  Application Load Balancer.
- **Database**: RDS PostgreSQL 16, `db.t4g.micro`, single-AZ (dev), 20 GB gp3.
- **Object storage**: One S3 bucket for user-uploaded attachments,
  versioning enabled, private.
- **Networking**: One VPC with 2 public + 2 private subnets across two AZs.
  ECS tasks and RDS live in private subnets; the ALB in public subnets.
- **Container image**: pulled from ECR (repository created here).

## Constraints
- Region must be configurable; default `us-east-1`.
- Environment name (`dev` / `staging` / `prod`) is an input variable.
- Monthly budget target: under $80/month at dev sizing.
- All resources tagged with `Project`, `Environment`, and `ManagedBy`.
- Database credentials must NOT be hardcoded — use variables or Secrets
  Manager references.

## Out of scope
- CI/CD pipelines, Kubernetes, multi-region.
