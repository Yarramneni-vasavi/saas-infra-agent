---
name: terraform-scaffold
description: How the Build agent must structure generated Terraform output — file layout, provider pinning, variables, state, tagging, and secrets handling. Use every time Terraform artifacts are generated from an approved architecture plan.
---

# Terraform Scaffold (Build Agent Output Contract)

Rules for the Terraform project the Build agent writes into the artifact directory.

## File Layout

Always generate this set, even when a file is short:

```
infra/
├── main.tf          # Resources, grouped by concern with comment headers
├── variables.tf     # Every tunable input, each with description + type + sane default
├── outputs.tf       # Endpoints, ARNs, connection info the app/monitoring needs
├── versions.tf      # required_version + pinned provider versions
└── terraform.tfvars.example   # Example values; never a real tfvars with secrets
```

## versions.tf Pattern

```hcl
terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
```

## Variables

- Everything that varies per deploy is a variable: region, environment, instance
  sizes/counts, CIDRs, domain names, scaling bounds.
- Every variable has `description` and `type`; give defaults so `terraform plan`
  works out of the box, except for genuinely account-specific values.
- Secrets are `sensitive = true` variables or references to Secrets Manager/SSM —
  never literals in `.tf` files.

## State & Environments

- Add a commented-out S3 backend block in `versions.tf` so switching to remote
  state is one uncomment away; default to local state for the scaffold.
- Use `var.environment` in resource names (`"${var.project_name}-${var.environment}-..."`)
  so multiple environments can coexist.

## Sizing from the Plan

- Take instance types, node counts, and autoscaling bounds from architecture.md —
  do not silently upsize or downsize what the Design agent chose.
- When the plan gives only user counts/latency targets and no explicit sizes,
  pick the smallest size that plausibly meets them and note the assumption in
  the summary.

## Checklist Before Finishing

- [ ] `terraform fmt`-clean formatting (2-space indent, aligned `=` not required)
- [ ] No hardcoded region, account ID, ARN, or credential
- [ ] Every resource reachable from a variable-driven name prefix
- [ ] Outputs cover everything the app deploy and monitoring stack will need

## Related Skills

- `terraform-module-library` — module structure when the stack is big enough to split
- `cost-optimization` — tagging standards and right-sizing defaults
- Per-service AWS skills (`ecs`, `rds`, `s3`, `lambda`, ...) for resource-level patterns
