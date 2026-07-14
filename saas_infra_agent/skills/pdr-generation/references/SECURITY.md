---
name: pdr-architecture/security
description: "Expanded IAM policy templates, security control decision trees, and compliance mapping for SaaS infrastructure PDR documents. Read when populating Section 5 (Network & Security) and Section 6.2 (Data Flow PII) of the PDR."
---

# Security Reference — SaaS Infrastructure PDR

The Design Agent reads this file when populating `arch.md` Section 5. It is also used to validate that security controls are appropriate for the compliance posture stated in requirements (R3).

---

## Compliance Tier Decision Tree

Use the user's answer to R3 (data residency / compliance) to set the tier. Higher tiers inherit all controls from lower tiers.

```
R3 = "No special requirements"
└─ Tier 0: AWS Security Baseline

R3 mentions GDPR or "EU data residency"
└─ Tier 1: Baseline + GDPR controls

R3 mentions SOC 2 or "security certification"
└─ Tier 2: Tier 1 + SOC 2 controls

R3 mentions HIPAA or "health data" or "PHI"
└─ Tier 3: Tier 2 + HIPAA controls (BAA required)

R3 mentions PCI-DSS or "payment card" or "cardholder"
└─ Tier 4: Tier 2 + PCI-DSS controls (separate CDE VPC required)
```

If compliance tier is Tier 2+, add an explicit note in Section 10 (Open Issues): "Compliance audit scope must be confirmed before Build Agent generates IAM policies."

---

## Tier 0 — AWS Security Baseline (always applied)

### IAM Controls

| Control | Implementation |
|---|---|
| Root account MFA | Assumed enabled at account level; note in PDR |
| No long-lived access keys | IAM roles only; no access key pairs in application code |
| Least-privilege policies | Per-service roles scoped to specific resource ARNs |
| Permission boundaries | Applied to developer and CI/CD roles |
| SCPs (if AWS Organizations) | Deny IAM user creation, deny disabling CloudTrail |

### Network Controls

| Control | Implementation |
|---|---|
| Security Groups | Default deny all; explicit allow only |
| NACLs | Default allow within VPC; deny-list known malicious CIDRs |
| VPC Flow Logs | Enabled; 90-day retention in CloudWatch Logs |
| No 0.0.0.0/0 ingress | Except ALB port 443; all others denied |
| Private subnets for compute | App and data tiers never in public subnet |

### Data Controls

| Control | Implementation |
|---|---|
| Encryption at rest | KMS CMK for RDS, S3, ElastiCache, Secrets Manager |
| Encryption in transit | TLS 1.2+ everywhere; enforce via S3 bucket policy `aws:SecureTransport` |
| S3 Block Public Access | All four settings enabled on every bucket |
| RDS deletion protection | Enabled in production |

### Detection Controls

| Control | Implementation |
|---|---|
| CloudTrail | All regions, management + S3 data events |
| AWS Config | All resources; config history to S3 |
| GuardDuty | Enabled; findings → SNS → Slack |
| Security Hub | CIS AWS Foundations Benchmark enabled |
| CloudWatch Alarms | Root login, console sign-in without MFA, IAM policy changes |

---

## Tier 1 — GDPR Additions

| Control | Implementation |
|---|---|
| Data residency | All services deployed in `eu-west-1` or `eu-central-1` only |
| Cross-region replication | Disabled on S3 buckets holding PII unless explicit consent captured |
| PII tagging | S3 objects containing PII tagged `DataClassification=PII` |
| Data retention | S3 lifecycle rules enforce deletion after retention period |
| Access logging | All access to PII data logged; logs retained 1 year |
| Right to erasure | Data deletion procedure documented in Section 10 as open issue for Build Agent |

---

## Tier 2 — SOC 2 Additions

| Control | Implementation |
|---|---|
| Audit logging | CloudTrail + CloudWatch Logs immutable; no deletion policy |
| Change management | All infra changes via Terraform; no console changes in production |
| Vulnerability scanning | ECR image scanning on push; Inspector for EC2/ECS |
| Patch management | ECS Fargate — no patch management needed; note this as a Fargate benefit |
| Penetration test | Documented in Section 10 as required before go-live |
| Incident response | CloudWatch Alarms → PagerDuty/OpsGenie (note as open issue) |
| Backup testing | RDS snapshot restore test documented as quarterly runbook |

---

## Tier 3 — HIPAA Additions

| Control | Implementation |
|---|---|
| AWS BAA | Must be signed; note as blocker in Section 10 Open Issues |
| PHI encryption | Dedicated KMS CMK for PHI data; key rotation enabled |
| PHI access logging | All reads/writes to PHI logged with user identity + timestamp |
| PHI isolation | PHI stored only in designated private subnets; dedicated S3 bucket |
| Minimum necessary access | IAM policies restrict to minimum PHI fields required per role |
| Audit log integrity | CloudTrail log file validation enabled |

---

## Tier 4 — PCI-DSS Additions

| Control | Implementation |
|---|---|
| CDE isolation | Separate VPC for cardholder data environment (CDE) |
| CDE VPC CIDR | `10.1.0.0/16` (isolated from main VPC `10.0.0.0/16`) |
| No cardholder data in logs | Lambda/ECS log sanitization required; note in Section 10 |
| WAF PCI ruleset | AWS Managed Rules — `AWSManagedRulesPCIRuleSet` enabled |
| Quarterly ASV scans | Note as open issue — third-party ASV required |
| Penetration test | Annual; note as open issue |
| Tokenization | Recommend Stripe or Braintree for card tokenization (do not store raw PANs) |

---

## IAM Policy Templates

Use these as starting points. Scope resource ARNs to the specific account/region/resource before writing to PDR.

### ECS Task Role (P1/P3 standard)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadAppSecrets",
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": "arn:aws:secretsmanager:REGION:ACCOUNT:secret:APP_NAME/*"
    },
    {
      "Sid": "ReadWriteAppBucket",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::APP_BUCKET_NAME/*"
    },
    {
      "Sid": "ListAppBucket",
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::APP_BUCKET_NAME"
    },
    {
      "Sid": "XRayWrite",
      "Effect": "Allow",
      "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
      "Resource": "*"
    }
  ]
}
```

### CI/CD Deploy Role (ECS Blue/Green)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ECRPush",
      "Effect": "Allow",
      "Action": [
        "ecr:GetAuthorizationToken",
        "ecr:BatchCheckLayerAvailability",
        "ecr:PutImage",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload"
      ],
      "Resource": "arn:aws:ecr:REGION:ACCOUNT:repository/APP_NAME"
    },
    {
      "Sid": "ECSDeployOnly",
      "Effect": "Allow",
      "Action": [
        "ecs:RegisterTaskDefinition",
        "ecs:UpdateService",
        "ecs:DescribeServices",
        "ecs:DescribeTaskDefinition"
      ],
      "Resource": "*",
      "Condition": {
        "ArnEquals": {
          "ecs:cluster": "arn:aws:ecs:REGION:ACCOUNT:cluster/APP_NAME-ENVIRONMENT"
        }
      }
    }
  ]
}
```

### Lambda Execution Role (P2 standard)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BasicExecution",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:REGION:ACCOUNT:log-group:/aws/lambda/FUNCTION_NAME:*"
    },
    {
      "Sid": "DynamoDBAccess",
      "Effect": "Allow",
      "Action": [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:Query"
      ],
      "Resource": [
        "arn:aws:dynamodb:REGION:ACCOUNT:table/TABLE_NAME",
        "arn:aws:dynamodb:REGION:ACCOUNT:table/TABLE_NAME/index/*"
      ]
    },
    {
      "Sid": "ReadSecrets",
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": "arn:aws:secretsmanager:REGION:ACCOUNT:secret:APP_NAME/*"
    }
  ]
}
```

---

## Security Anti-Patterns — Block These in PDR Review

If the Design Agent's draft architecture contains any of the following, it MUST be corrected before reaching human approval:

| Anti-Pattern | Correct Alternative |
|---|---|
| Database in public subnet | Database always in private subnet |
| Security group `0.0.0.0/0` inbound except ALB 443 | Restrict to minimum required CIDR |
| Secrets in Lambda/ECS environment variables | Secrets Manager with task role access |
| S3 bucket with public access enabled | Block public access; use pre-signed URLs or CloudFront |
| IAM role with `*` resource on `*` action | Scope to specific resource ARNs and minimum actions |
| No MFA on human IAM users | Enforce via SCP or IAM password policy |
| RDS without encryption | KMS encryption always on |
| CloudTrail disabled in any region | Multi-region trail required |
| Long-lived access keys in application | IAM task/instance roles only |
| Hardcoded credentials in Terraform | Terraform variables + Secrets Manager |
