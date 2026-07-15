---
name: terraform-floci-emulator
description: Use this skill whenever generating or editing Terraform (.tf) files that will be applied against Floci, the local AWS emulator, rather than real AWS. Trigger on any mention of Floci, "local AWS emulator", "test environment infra", localhost:4566, or when the user says infra will be validated/tested locally before real deployment. Also trigger if a previous terraform init/apply against Floci failed with a provider version constraint error or a module-related error — this skill exists specifically because registry modules and version-pinning habits that are fine for real AWS break Floci. Do NOT use this skill's rules when the user explicitly says the Terraform is targeting real AWS/production — use standard module-based best practices there instead.
---

# Terraform for Floci (local AWS emulator)

Floci emulates AWS locally. Some services are fast in-process handlers (S3, SQS,
SNS, DynamoDB, IAM, STS, KMS). Others — RDS, Lambda, ElastiCache, ECS, EKS — are
backed by **real Docker containers** that Floci spawns and manages. This second
group is why naive "looks-like-AWS" Terraform breaks: the resources are real,
but the network topology and provider requirements are not the same as prod.

Generate Terraform for Floci by following the rules below, then use the
reference template in `references/main.tf.template` as the starting structure.

## The four hard rules

These exist because violating them is exactly what broke previous scripts.

### 1. Provider version: pin to `>= 5.0`, never `< 5.0`

```hcl
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
  required_version = ">= 1.0.0"
}
```

Do not copy an older constraint (`>= 4.0, < 5.0`) from memory or from an old
project. If unsure, use `>= 5.0` with no upper bound.

### 2. No `terraform-aws-modules/*` registry modules — use plain `aws_*` resources only

Community modules (`terraform-aws-modules/ecs/aws`, `.../rds/aws`,
`.../s3-bucket/aws`, etc.) each declare their own internal
`required_providers` constraint. These drift out of sync with each other and
with your root module constantly — this is the actual cause of the classic
error:

```
Could not retrieve the list of available versions for provider hashicorp/aws:
no available releases match the given constraints >= 3.62.0, >= 4.0.0, ... < 5.0.0, >= 5.83.0
```

Modules also frequently assume features/APIs Floci hasn't implemented yet.
Always write native resources directly:

| Instead of module | Use |
|---|---|
| `terraform-aws-modules/ecs/aws` | `aws_ecs_cluster`, `aws_ecs_task_definition`, `aws_ecs_service` |
| `terraform-aws-modules/rds/aws` or `rds-aurora` | `aws_db_instance` (standard Postgres/MySQL — Floci runs this as a real container, not Aurora clustering) |
| `terraform-aws-modules/s3-bucket/aws` | `aws_s3_bucket` + `aws_s3_bucket_versioning` + `aws_s3_bucket_lifecycle_configuration` as separate resources |

If the user's request seems to need Aurora specifically, flag that Floci runs
RDS as a single Postgres/MySQL container — Aurora's clustering behavior isn't
what gets emulated, so `aws_db_instance` is the correct mapping, not a like-for-like
Aurora replacement.

### 3. Always include the `provider "aws"` emulator block

Every script must point at the emulator explicitly — Terraform has no way to
infer this:

```hcl
provider "aws" {
  region                      = "us-east-1"
  access_key                  = "test"
  secret_key                  = "test"
  s3_use_path_style           = true
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true

  endpoints {
    ec2      = "http://localhost:4566"
    ecs      = "http://localhost:4566"
    rds      = "http://localhost:4566"
    s3       = "http://localhost:4566"
    iam      = "http://localhost:4566"
    sts      = "http://localhost:4566"
    dynamodb = "http://localhost:4566"
  }
}
```

Add other service endpoints (e.g. `dynamodb`, `sqs`, `sns`) to the block only
if the script actually uses them.

### 4. Know which services are "real containers" vs. in-process, and design connectivity accordingly

For **RDS, Lambda, ElastiCache, ECS, EKS**: port 4566 is control-plane only
(create/describe calls). The actual data traffic (JDBC, Redis protocol, HTTP
to a container) goes **directly to the spawned container**, not through 4566.

- From the host machine: use the dynamic port Floci publishes (e.g. RDS uses
  a port in the `7001–7099` range) — get it via
  `aws rds describe-db-instances --endpoint-url http://localhost:4566`, never
  hardcode `5432`.
- From another Floci-spawned container (e.g. an ECS task talking to RDS):
  they share Floci's Docker network, so use the container network
  address, not `localhost`.
- Wire connection details from Terraform resource attributes
  (`aws_db_instance.postgres.address`, `.port`, `.username`) into consuming
  resources (e.g. `aws_ecs_task_definition` container `environment` block) —
  never hardcode values that Terraform already knows, since Floci assigns
  some of these dynamically per run.

For **S3, SQS, SNS, DynamoDB, IAM, STS, KMS**: these are in-process, so
`http://localhost:4566` is the actual endpoint for both control- and
data-plane. No special container networking needed.

## Workflow

1. Confirm the target is Floci (not real AWS) — if ambiguous, ask.
2. Draft resources using only native `aws_*` types, per rule 2.
3. Include the full provider block from rule 3, trimmed to only the
   `endpoints` this script needs.
4. For any RDS/ECS/Lambda resource, add the connectivity wiring from rule 4
   rather than leaving credentials/hosts as hardcoded placeholders.
5. Before returning the script, self-check:
   - Any `version = "..."` constraint below 5.0 anywhere? → fix.
   - Any `module "..."` block sourced from the public registry? → replace with
     native resources.
   - Any hardcoded `localhost:5432` or similar prod-style port? → replace with
     a lookup or resource attribute reference.
6. Mention `terraform init` → `terraform plan` → `terraform apply` as the
   apply sequence, and `terraform destroy` (see below) for teardown.

## Destroying and resetting

```powershell
terraform destroy                      # respects dependency order
docker ps -a                           # confirm no orphaned containers remain
docker rm -f <container>               # if state drifted and something lingered
Remove-Item -Recurse -Force .terraform, .terraform.lock.hcl, terraform.tfstate, terraform.tfstate.backup
```

For a full reset of everything Floci is tracking (not just one config), stop
Floci, clear its data volume, and restart it before re-running `terraform init`.

## Reference

See `references/main.tf.template` for a complete, verified-working example
covering VPC/subnets, RDS, S3, and an ECS cluster+service, built entirely from
the rules above.
