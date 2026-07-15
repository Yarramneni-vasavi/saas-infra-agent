# Preliminary Design Review — Todo SPA (SPA + API + relational DB)
**Version:** 1.0  
**Date:** 2026-07-14  
**Status:** Approved  
**Author:** SaaS Infrastructure Agent — Design Module  
**Cloud Provider:** AWS  
**Reviewed By:** Pending

## Executive Summary

This design provisions a browser-based single-page application (SPA) served via Amazon S3 and Amazon CloudFront that talks to a REST API implemented with Amazon API Gateway + AWS Lambda and a relational backing store in Amazon RDS for PostgreSQL. The primary workload is a user-facing todo application with CRUD operations and token-based authentication for an expected peak API load of 500 requests per second and a target user base of ~10,000 users. The top architectural constraint is monthly cost control with a budget ceiling of $500, so the design favors serverless, managed services and conservative default resource sizing with operational controls for budget enforcement.

## Requirements Summary

| # | Requirement | Value | Source |
|---|---|---|---|
| R1 | Workload type | SPA + API + relational DB | User input |
| R2 | Expected RPS / concurrency | 500 RPS peak | User input |
| R3 | Data residency / compliance | No special requirements specified | User input |
| R4 | Availability target | No requirement specified | User input |
| R5 | Budget ceiling (monthly) | $500/month | User input |
| R6 | Target user base | 10,000 users | User input |

## 4. Architecture Overview

#### 4.1 Architecture Pattern
Serverless API-driven (Amazon API Gateway + AWS Lambda + Amazon RDS for PostgreSQL). This pattern is chosen because it minimizes operational overhead and fixed-cost infrastructure while supporting autoscaling of the API layer to meet the 500 RPS target. Amazon RDS provides a familiar relational model, point-in-time recovery and managed backups required by the application's relational data needs while RDS Proxy handles connection pooling for scale from short‑lived Lambda execution contexts.

#### 4.2 Architecture Diagram (text)

```mermaid
graph LR
  subgraph Users
    Browser[Browser (SPA)]
  end

  subgraph CDN
    CF[Amazon CloudFront]
    WAF[AWS WAF]
  end

  subgraph Edge
    ACM[ACM Certificate]
  end

  subgraph Frontend
    S3[Amazon S3 — SPA assets]
  end

  subgraph API
    APIGW[Amazon API Gateway (HTTP API)]
    Cognito[Amazon Cognito User Pool]
    Lambda[AWS Lambda — API handlers]
    XRay[AWS X-Ray]
  end

  subgraph AppNetworking
    RDSProxy[Amazon RDS Proxy]
    Secrets[AWS Secrets Manager]
  end

  subgraph Data
    RDS[(Amazon RDS for PostgreSQL)]
    S3Data[Amazon S3 — user uploads, assets]
  end

  subgraph CI_CD
    CodePipeline[AWS CodePipeline / CodeBuild]
  end

  Browser -->|GET SPA| CF --> S3
  Browser -->|API calls (JWT)| CF --> APIGW
  APIGW -->|JWT validation| Cognito
  APIGW -->|Invoke| Lambda
  Lambda -->|Pooled DB connections| RDSProxy --> RDS
  Lambda -->|Read/Write objects| S3Data
  Lambda -->|Read secrets| Secrets
  Lambda --> XRay
  CF --> WAF
  CF --> ACM
  CodePipeline -->|deploy/migrations| Lambda
  CodePipeline -->|deploy/migrations| RDS
  CodePipeline -->|manage infra| APIGW
```

#### 4.3 AWS Services Selected

| Service | Config / Tier | Justification | Alternative Considered |
|---|---|---|---|
| Amazon S3 | Single versioned bucket per environment; SSE-S3 or KMS; origin for CloudFront | Durable, low-cost static hosting for SPA assets with versioning and lifecycle rules | Hosted Jamstack platforms (Netlify/Vercel) — better developer UX but adds platform cost and potential vendor lock-in |
| Amazon CloudFront | CloudFront distribution in front of S3 and API (edge cache), ACM TLS, integration with WAF | Global caching to reduce origin load and latency, supports cache-control immutability | No CDN (serve from S3 directly) — lower complexity but higher origin cost and worse latency |
| AWS Certificate Manager (ACM) | Public certificates attached to CloudFront and API endpoints | Managed free TLS certificates for edge and API endpoints | Bring-your-own certificate — workable but adds management overhead |
| Amazon API Gateway (HTTP API) | HTTP API with JWT authorizer (Cognito), per-route throttling & access logging | Lower-cost managed API with native JWT authorizers and direct Lambda integration | API Gateway REST — richer features but higher cost and slightly higher latency |
| AWS Lambda | 512 MB default memory; reserved concurrency controls per env; ARM (Graviton) where supported | Serverless scaling for spiky traffic, zero server management, cost-efficient for baseline | ECS Fargate / EC2 — more predictable cold starts and connection behavior but higher base cost and operational overhead |
| Amazon RDS for PostgreSQL | db.t3.medium baseline (single-AZ baseline, gp3 storage); automated backups enabled | Managed relational DB with PITR and snapshots; db.t3.medium balances cost and capacity for Todo app baseline | Amazon Aurora or larger RDS instance / Multi-AZ — higher availability and read scaling but materially higher cost |
| Amazon RDS Proxy | Runtime connection pooling in front of RDS; IAM auth integration | Prevents DB connection exhaustion from Lambda concurrency, enables credential rotation | Application-side pooler (pgbouncer) on long‑lived hosts — cheaper but adds servers to manage |
| AWS Secrets Manager | One secret per env for DB credentials and signing keys; rotation enabled | Secure secret storage with rotation and auditability, integrates with RDS/RDS Proxy | AWS Systems Manager Parameter Store (Secure) — lower cost but fewer native rotation features |
| Amazon Cognito | User Pools for SPA auth with JWTs; groups/scopes for RBAC | Managed OAuth2/JWT provider suited to SPAs, integrates with API Gateway | Third-party IdP (Auth0) or custom auth — more features but added cost and integration effort |
| AWS WAF | AWS WAF attached to CloudFront; AWS Managed Rules + custom rate rules | Protects origin from OWASP classes and abusive traffic, reduces unwanted origin load | Rely solely on API Gateway throttling and CloudFront — lower cost but weaker protection |
| Amazon CloudWatch | Metrics, Logs (structured), Alarms; log retention configurable | Integrated observability for metrics, logs, alarms and SLO tracking | SaaS monitoring (Datadog) — richer UX but recurring ingest costs likely exceed budget |
| AWS X-Ray | Adaptive sampling for traces | Tracing for latency/root-cause analysis while controlling trace volume | No tracing — simpler and lower cost but harder to debug latency issues |
| Amazon SNS | SNS topics for alarm notifications and budget alerts | Low-latency notification to on-call channels | Direct email only — less flexible for integrations |
| AWS CodePipeline + CodeBuild | Pipeline per environment; build specs for tests and DB migrations | IaC-driven CI/CD with migration steps and smoke tests before production | GitHub Actions — equivalent option; chosen AWS-native for tight AWS integration |
| Terraform / IaC | All infra defined in Terraform (or CDK/CloudFormation per team preference) | Reproducible, reviewable infra-as-code and environment parity | CloudFormation/CDK — AWS native but may not match team tooling preferences |
| AWS IAM / Organizations | Least-privilege IAM roles, tagging enforcement, optional SCPs | Governance and access control for infra and operator actions | No organization-level guardrails — faster setup but weaker governance |
| AWS Budgets & Cost and Usage Reports | Budget set to $500/month with 50/80/95% alerts | Continuous spend monitoring and automation hooks to limit cost | No budgets — higher risk of surprise spend |

## 5. Network & Security Design

#### 5.1 VPC Layout

VPC CIDR: 10.0.0.0/16 (assumed — see Open Issues & Assumptions)

Public Subnets (2 AZs):
  - 10.0.1.0/24 — AZ-a → NAT Gateway (if required for any outbound from private subnets)
  - 10.0.2.0/24 — AZ-b → NAT Gateway

Private App Subnets (2 AZs):
  - 10.0.11.0/24 — AZ-a → Lambda ENIs (if Lambdas need VPC access) / RDS Proxy
  - 10.0.12.0/24 — AZ-b → Lambda ENIs / RDS Proxy

Private Data Subnets (2 AZs):
  - 10.0.21.0/24 — AZ-a → Amazon RDS for PostgreSQL
  - 10.0.22.0/24 — AZ-b → Amazon RDS for PostgreSQL (if Multi-AZ enabled)

Note: If Lambdas do not need VPC access for other services, prefer keeping them out of VPC to avoid ENI-related cold-starts; RDS remains in private subnets reachable via RDS Proxy.

If a pure serverless design without private resources were chosen, state: "No VPC required — all services are fully managed AWS endpoints." This design includes a private RDS instance; therefore a VPC is required.

#### 5.2 Security Posture

| Control | Approach |
|---|---|
| Authentication | Amazon Cognito User Pools issuing JWTs for SPA; JWT validated by API Gateway authorizer and service-side checks in Lambda |
| Authorization | RBAC via Cognito groups / token scopes enforced in Lambda handlers; least-privilege IAM roles per service |
| Secrets management | AWS Secrets Manager for DB credentials and signing keys with rotation enabled |
| Encryption at rest | AES-256 via AWS KMS for RDS storage and S3 objects (SSE-KMS or SSE-S3 for S3 as configured) |
| Encryption in transit | TLS 1.2+ enforced; ACM certificates on CloudFront and API endpoints |
| WAF | AWS WAF attached to CloudFront with AWS Managed Rules (OWASP) plus simple custom rate/IP rules |
| Logging | CloudTrail for control-plane audit, VPC Flow Logs for network telemetry, CloudFront access logs → S3, CloudWatch Logs for API + Lambda structured logs |

#### 5.3 IAM Design

- **`lambda-exec-role`** — Lambda runtime role: PutLogEvents to CloudWatch Logs; PublishMetrics to CloudWatch; ReadSecretsManager for secrets named `todo/*`; No EC2 or IAM management permissions; network connect to RDS Proxy endpoint permitted via Security Groups.
- **`rds-monitoring-role`** — RDS monitoring role scoped to Enhanced Monitoring for the RDS instance(s) only (read-only monitoring metrics).
- **`ci-deploy-role`** — CI/CD deployment role scoped to environment-specific resource ARNs: apply Terraform changes or CloudFormation stack updates limited to the project environments; allowed to create/restore RDS snapshots and run migration steps; scoped by tags/environment.
- **`secrets-rotation-role`** — Role assumed by rotation Lambda with permissions to update Secrets Manager secret versions and, if needed, rotate RDS credentials via RDS API.
- **`budget-automation-role`** — Role used by budget automation runbooks to perform cost-saving actions (scale-down, stop non-critical resources) limited to specific resource ARNs and tags.

## 6. Data Architecture

#### 6.1 Data Stores

| Store | Service | Schema / Structure | Retention | Backup |
|---|---|---|---|---|
| Primary DB | Amazon RDS for PostgreSQL | Relational — users, todos, metadata; schema versioned via migration tooling (Flyway-like) | Retention not specified by user — default assumption 7 days snapshot retention (see Open Issues) | Automated daily snapshots + point-in-time recovery (PITR) enabled |
| Object store | Amazon S3 | Object store for SPA assets and optional user uploads; immutable production builds via versioned keys | Lifecycle policy configurable; default: Glacier/Archive after policy-defined period (user to specify) | Versioning enabled; on-demand snapshots via lifecycle |

#### 6.2 Data Flow

The SPA (Browser) fetches static assets from Amazon CloudFront backed by an S3 origin. User-initiated API calls (authenticated with Cognito-issued JWTs) go to Amazon API Gateway (HTTP API), which validates tokens and invokes AWS Lambda handlers. Lambda reads/writes relational data in Amazon RDS for PostgreSQL through Amazon RDS Proxy to avoid connection exhaustion; for larger objects or uploads Lambda reads/writes to Amazon S3. Secrets for DB credentials and signing keys are retrieved from AWS Secrets Manager. Observability data (metrics and logs) flows to Amazon CloudWatch and sampled traces to AWS X-Ray. No special PII handling or compliance was specified by the user; PII storage and retention policies are subject to the open assumptions listed below.

## 7. Scalability & Availability Design

#### 7.1 Scaling Strategy

| Component | Scaling Mechanism | Min | Max | Trigger |
|---|---:|---:|---:|---|
| AWS Lambda (API handlers) | AWS Lambda autoscaling (invocations-driven) | 1 reserved concurrency (assumption for warm baseline) | Account concurrency limit (default) — explicit max set as needed (assumption) | Incoming request rate / Lambda provisioned concurrency thresholds |
| Amazon API Gateway (HTTP API) | AWS managed scaling with per-stage throttling and usage plans | N/A | N/A | Request rate; configured throttles and usage plans |
| Amazon RDS for PostgreSQL | Vertical scaling via instance size change or manual promotion to Multi-AZ | db.t3.medium (baseline) | Larger instance / Multi-AZ (manual/CI-driven scaling) | Sustained CPU/connection saturation / capacity planning alerts |
| Amazon RDS Proxy | Managed proxy capacity scaling | Default proxy capacity | Scales per AWS account limits (managed) | Number of connections and configured proxy capacity |
| Amazon CloudFront | AWS-managed edge scaling | N/A | N/A | Global request volume |

Notes:
- Several numeric limits (account concurrency, proxy capacity) depend on AWS account limits and can be raised as needed; these are captured as assumptions in Open Issues. Lambda reserved concurrency of 1 is a conservative warm baseline to reduce cold-start impact at low cost (assumption).

#### 7.2 Availability Targets

| Tier | Target SLA | How achieved |
|---|---|---|
| Application | 99.9% (assumed — user did not provide SLA) | API Gateway + Lambda across multiple AZs (Lambda is regional) and CloudFront edge caching; API health checks and alarm-driven remediation |
| Database | 99.95% (assumed if Multi-AZ enabled; baseline single-AZ is lower) | Amazon RDS Multi-AZ provides automatic failover; baseline single-AZ is cost-optimized and has lower availability |
| Overall system | 99.9% (assumed) | Weakest link governs; design recommends Multi-AZ or read-replicas for stricter DB availability if SLA increases |

These availability figures are assumptions made to drive build decisions while respecting the $500/month budget; see Open Issues for confirmation or higher‑availability options.

#### 7.3 Fault Tolerance

Top failure modes and mitigations:

1. AZ outage → Mitigation: design supports Multi-AZ RDS configuration option and CloudFront + API Gateway regional routing; RDS Proxy and Lambda across AZs reduce single-AZ impact. Baseline cost-optimized configuration uses single-AZ RDS (see trade-off).
2. Lambda cold starts or function errors under peak load → Mitigation: reserved concurrency for critical handlers or provisioned concurrency where latency SLOs require; adaptive sampling and alarms to detect error spikes.
3. Database connection exhaustion → Mitigation: Amazon RDS Proxy provides managed connection pooling; CI/CD controlled connection limits and application-side sensible pooling/retries.

## 8. Cost Estimate

Below are baseline monthly estimate ranges taken from the architecture draft and used for budget planning. These are estimates only; actual costs depend on traffic, retention, and chosen SLA options.

| Service | Config | Est. Monthly Cost |
|---|---|---|
| Amazon S3 + CloudFront | SPA assets + small CDN traffic | $6 – $90 |
| Amazon API Gateway (HTTP API) | 500 RPS peak traffic volume estimate | $5 – $60 |
| AWS Lambda | 512 MB average, invocation-driven | $5 – $150 |
| Amazon RDS for PostgreSQL | db.t3.medium single-AZ baseline | $30 – $100 |
| Amazon RDS Proxy | Pooled connections for Lambda | $15 – $120 |
| AWS Secrets Manager | 1–3 secrets with rotation | $0.50 – $8 |
| Amazon Cognito | MAU dependent | $0 – $70 |
| AWS WAF | Managed rules + small custom rules | $10 – $120 |
| CloudWatch + X-Ray | Metrics, logs (modest retention), sampled traces | $12 – $150 |
| CI/CD (CodeBuild) | Occasional builds & migrations | $5 – $80 |
| SNS / Route53 / IAM / Budgets | Misc governance & notifications | $1 – $10 |
| **Total (baseline ranges)** | | **~$100 – $350 / month** |

Cost Overrun Flag: No — the baseline estimated range (~$100–$350/month) is within the stated $500/month budget ceiling. If the project opts into higher-availability (Multi-AZ RDS), increased log retention, heavier CloudFront egress, or Shield Advanced, costs can approach or exceed $500/month and require additional cost controls.

Cost Optimization Options (if needed):
- Use smaller Lambda memory and optimize function duration; prefer Graviton (ARM) runtimes.
- Enforce CloudWatch log retention limits (shorter retention for staging).
- Reserve capacity where predictable (Savings Plans) for sustained baseline if usage patterns support it; prefer on-demand serverless savings options carefully.
- Avoid Multi-AZ RDS in baseline; enable as paid upgrade only when SLA requires it.

## 9. Operational Design

#### 9.1 Observability Stack

| Signal | Service | Retention |
|---|---|---|
| Metrics | Amazon CloudWatch Metrics + Container/Function Insights | 15 months for standard metrics; custom metrics retention configurable |
| Logs | Amazon CloudWatch Logs (structured JSON); CloudFront access logs → S3 | Default prod retention 14–30 days (configurable for budget) |
| Traces | AWS X-Ray (adaptive sampling) | 30 days (configurable) |
| Dashboards | CloudWatch Dashboards (auto-generated) | — |
| Alerts | CloudWatch Alarms → SNS → Slack/Email | — |

SLOs to monitor: request rate (RPS), p50/p95/p99 latency, error rates (4xx/5xx), DB connections, DB query latency, Lambda errors/duration.

#### 9.2 Deployment Strategy

- Pipeline: AWS CodePipeline + AWS CodeBuild for build, test, migration and deploy stages (team may substitute GitHub Actions if preferred).
- Strategy: Blue/Green or Canary via Lambda alias traffic shifting and API Gateway stage management; rollout gates include smoke tests and health checks.
- Rollback: Automatic rollback on failed health checks or pipeline stage failure; manual rollback by promoting prior Lambda alias and reverting infra changes via IaC.
- IaC: Terraform (preferred by team) or CDK/CloudFormation per team choice — all infra defined as code.

#### 9.3 Runbook Pointers

1. Scale-out manually — run pipeline or use Lambda reserved concurrency adjustments: `Adjust reserved concurrency for critical Lambda functions in Console or via AWS CLI`.
2. Force RDS failover (if Multi-AZ enabled) — `aws rds reboot-db-instance --db-instance-identifier <id> --force-failover` or use RDS Console failover controls.
3. Roll back deployment — promote prior Lambda alias and revert Terraform state change; follow pipeline rollback playbook.

## 10. Open Issues & Assumptions

All unresolved items are marked as Assumptions (none are blocking Open issues). The Build Agent may proceed using these assumptions; human confirmation recommended before production promotion.

| # | Item | Type | Resolution needed by |
|---|---|---|---|
| A1 | VPC CIDR and exact subnet CIDRs are not specified; design assumes 10.0.0.0/16 with /24 subnets as described | Assumption | Build start |
| A2 | Availability target (SLA) not specified by user; design assumes Application 99.9% and Database 99.95 (Multi-AZ optional) to balance cost | Assumption | Human approval before production |
| A3 | Backup retention and legal data retention windows not specified; design assumes 7-day automated backup retention baseline and configurable PITR | Assumption | Human approval before production |
| A4 | Region and multi-region requirements not specified; design assumes single AWS region deployment with CloudFront for global edge delivery | Assumption | Build start |
| A5 | Acceptance of AWS-managed TLS (ACM) for production certificates assumed | Assumption | Build start |
| A6 | Log/metric retention windows tuned for budget (prod 14–30 days) assumed; exact retention not provided | Assumption | Build start |
| A7 | Account limits (Lambda concurrent execution limits, RDS Proxy capacity) treated as default AWS limits; quota increases not requested | Assumption | Build start |

Rule: No rows are marked as "Open issue". If any of the above must be treated as blocking (Open issue) the human approver must mark them so before Build Agent execution.

## 11. Approval Sign-Off

Architecture reviewed and approved for Build Agent execution.

Approved by: ___________________  Date: ___________  
Comments:
