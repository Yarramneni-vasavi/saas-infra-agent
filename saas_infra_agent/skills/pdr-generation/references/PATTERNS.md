---
name: pdr-architecture/patterns
description: "Architecture pattern selection decision tree and per-pattern AWS service defaults for the SaaS Infrastructure Agent Design module. Always read alongside SKILL.md."
---

# Architecture Patterns Reference

Used by the Design Agent during the `architecture_draft` stage to select the correct pattern from requirements gathered, and populate the PDR Services table with sensible defaults. The Build Agent uses pattern identity to select the correct Terraform module set.

---

## Pattern Selection Decision Tree

Work top-to-bottom. Stop at the first match.

```
1. Is the workload a REAL-TIME streaming / event pipeline?
   └─ YES → Pattern: Event-Driven Streaming          [P4]

2. Is the workload primarily ML training or batch inference?
   └─ YES → Pattern: ML Training Pipeline             [P5]

3. Is the expected peak RPS < 100 AND no persistent connection requirement?
   └─ YES → Pattern: Serverless Web API               [P2]

4. Does the workload have > 3 independent services that need independent scaling?
   └─ YES → Pattern: Container Microservices          [P3]

5. Is the workload a standard web app or SaaS product with a database?
   └─ YES → Pattern: Three-Tier VPC                   [P1]

6. Is the workload primarily analytical / data warehousing?
   └─ YES → Pattern: Data Lake / Analytics            [P6]

DEFAULT → Pattern: Three-Tier VPC                     [P1]
```

If requirements are ambiguous between two patterns, present both options to the user as a clarification question before proceeding to `architecture_draft`.

---

## P1 — Three-Tier VPC

**When to use:** Standard SaaS web applications, REST APIs with a relational database, B2B/B2C products with a persistent user model.

**Not when:** Pure async/event workloads, ML-heavy workloads, < 100 RPS with no stateful session.

### Default Service Stack

| Layer | Service | Default Config |
|---|---|---|
| DNS | Route 53 | Alias record → ALB |
| CDN | CloudFront | Optional; add if static assets or global latency matters |
| Load Balancer | ALB | HTTPS only; HTTP → HTTPS redirect; WAF attached |
| Compute | ECS Fargate | 2 vCPU / 4 GB per task; min 2, max 10 |
| Autoscaling | ECS Application Auto Scaling | Target CPU 70% |
| Primary DB | Aurora PostgreSQL (Serverless v2) | Min 0.5 ACU, Max 16 ACU; Multi-AZ |
| Cache | ElastiCache Redis (Serverless) | Session cache and DB query cache |
| Object store | S3 | User uploads, static assets; versioning on |
| Secrets | Secrets Manager | All DB credentials and API keys |
| Container registry | ECR | Image scanning on push |
| CI/CD | GitHub Actions → ECR → ECS | Blue/Green via CodeDeploy |
| Observability | CloudWatch + Container Insights + X-Ray | |

### VPC Default Layout

```
VPC: 10.0.0.0/16

Public:        10.0.1.0/24, 10.0.2.0/24   (ALB, NAT GW)
Private App:   10.0.11.0/24, 10.0.12.0/24 (ECS Tasks)
Private Data:  10.0.21.0/24, 10.0.22.0/24 (RDS, ElastiCache)
```

### Terraform Module Set (for Build Agent)

- `modules/vpc` — VPC, subnets, IGW, NAT GW, route tables
- `modules/alb` — ALB, target group, listener, ACM cert, WAF
- `modules/ecs` — ECS cluster, task definition, service, auto scaling
- `modules/rds-aurora` — Aurora cluster, parameter group, subnet group
- `modules/elasticache` — Redis replication group
- `modules/s3-app` — Application S3 bucket, lifecycle, versioning
- `modules/iam-roles` — Task role, execution role, CI deploy role
- `modules/monitoring` — CloudWatch dashboard, alarms, SNS topic

---

## P2 — Serverless Web API

**When to use:** Low-to-medium traffic APIs (< 100 RPS sustained, spiky or unpredictable load), event-driven handlers, webhook receivers, MVP/prototype SaaS products prioritizing cost.

**Not when:** Long-running processes (> 15 min), persistent WebSocket connections, CPU-heavy compute, workloads needing > 10 GB memory.

### Default Service Stack

| Layer | Service | Default Config |
|---|---|---|
| DNS | Route 53 | Alias → API Gateway custom domain |
| API Gateway | HTTP API (v2) | Prefer over REST API unless request transformation needed |
| Compute | Lambda | Runtime per language; 512 MB default; 30s timeout |
| Database | DynamoDB | On-demand billing; single-table design |
| Cache | DynamoDB DAX | Optional; add if read latency is a stated requirement |
| Object store | S3 | Versioning on |
| Auth | Cognito User Pools + JWT authorizer | |
| Secrets | Secrets Manager | Any third-party API keys |
| Events | EventBridge | Async fan-out between Lambda functions |
| Observability | Lambda Insights + CloudWatch + X-Ray | |

### VPC Default Layout

No VPC by default. Add VPC only if Lambda must access resources in a private subnet (e.g., RDS).

If VPC required:
```
VPC: 10.0.0.0/16
Private Lambda: 10.0.1.0/24, 10.0.2.0/24
Private Data:   10.0.11.0/24, 10.0.12.0/24
```

### Terraform Module Set (for Build Agent)

- `modules/api-gateway-http` — HTTP API, stage, custom domain, throttle
- `modules/lambda-function` — Function, IAM role, log group (parameterized per function)
- `modules/dynamodb` — Table, GSI, TTL, backup
- `modules/cognito` — User pool, app client, domain
- `modules/eventbridge` — Event bus, rules, targets
- `modules/monitoring-serverless` — Lambda error alarms, duration alarms, dashboard

---

## P3 — Container Microservices (ECS)

**When to use:** Multiple independently deployable services, polyglot teams, workloads needing per-service auto-scaling, SaaS products with clear domain boundaries.

**Not when:** Single-service apps (P1 is simpler), teams without container expertise, budget-constrained MVPs.

**ECS vs EKS decision:** Default to ECS Fargate. Use EKS only if: team has existing Kubernetes expertise, workload requires custom Kubernetes operators, or cluster count > 10 microservices with complex service mesh needs.

### Default Service Stack

| Layer | Service | Default Config |
|---|---|---|
| DNS + CDN | Route 53 + CloudFront | CloudFront in front of ALB |
| Load Balancer | ALB | One ALB per environment; path-based routing to services |
| Service Mesh | AWS App Mesh | Optional; add when > 5 services need mutual TLS or canary routing |
| Compute | ECS Fargate | Per-service task definitions; independent auto scaling |
| Service Registry | AWS Cloud Map | Service discovery within VPC |
| Primary DB | Aurora PostgreSQL | Shared cluster with per-service schemas OR per-service clusters |
| Message Broker | Amazon SQS | Standard queues for async inter-service messaging |
| Events | EventBridge | Domain events across service boundaries |
| Cache | ElastiCache Redis | Shared cache cluster; per-service key namespacing |
| Object store | S3 | Shared bucket with per-service prefix |
| CI/CD | GitHub Actions → ECR → ECS | Per-service pipelines; Blue/Green |
| Observability | CloudWatch Container Insights + X-Ray + Service Map | |

### VPC Default Layout

```
VPC: 10.0.0.0/16

Public:             10.0.1.0/24, 10.0.2.0/24    (ALB, NAT GW)
Private Services:   10.0.10.0/23, 10.0.12.0/23  (ECS Tasks — large range for service growth)
Private Data:       10.0.20.0/24, 10.0.21.0/24  (RDS, SQS VPC Endpoints, ElastiCache)
```

### Terraform Module Set (for Build Agent)

All P1 modules plus:
- `modules/sqs` — Queue, DLQ, redrive policy
- `modules/cloud-map` — Namespace, service records
- `modules/eventbridge-bus` — Custom event bus, cross-service rules

---

## P4 — Event-Driven Streaming

**When to use:** Real-time data ingestion, IoT telemetry, clickstream processing, log aggregation pipelines, financial market data.

### Default Service Stack

| Layer | Service | Default Config |
|---|---|---|
| Ingestion | Amazon Kinesis Data Streams | On-demand capacity mode; 7-day retention |
| Stream processing | Kinesis Data Firehose | S3 delivery; optional Lambda transform |
| Batch processing | AWS Glue | PySpark jobs for aggregations |
| Serving DB | DynamoDB | Hot path query results |
| Data warehouse | Amazon Redshift Serverless | Analytical queries |
| Object store | S3 | Raw data lake; partitioned by date |
| Orchestration | Step Functions | Pipeline DAG orchestration |
| Observability | CloudWatch + Kinesis metrics + Glue job metrics | |

---

## P5 — ML Training Pipeline

**When to use:** Model training, batch inference, feature engineering, experiment tracking.

### Default Service Stack

| Layer | Service | Default Config |
|---|---|---|
| Feature store | SageMaker Feature Store | Online + offline store |
| Training | SageMaker Training Jobs | On-demand; Spot instances for cost |
| Experiment tracking | SageMaker Experiments | |
| Model registry | SageMaker Model Registry | Approval workflow before deploy |
| Inference | SageMaker Endpoints | Real-time; or Batch Transform for offline |
| Orchestration | Step Functions | Training → evaluation → register → deploy pipeline |
| Object store | S3 | Training data, model artifacts |
| Monitoring | SageMaker Model Monitor | Data drift, model quality |

---

## P6 — Data Lake / Analytics

**When to use:** Business intelligence, reporting, multi-source data consolidation, ad-hoc analytical queries.

### Default Service Stack

| Layer | Service | Default Config |
|---|---|---|
| Ingestion | AWS Glue / DMS / Kinesis Firehose | Source-dependent |
| Storage | S3 | Partitioned by source/date; Parquet format |
| Catalog | AWS Glue Data Catalog | Schema registry |
| ETL | AWS Glue Jobs | PySpark; triggered by EventBridge schedule |
| Querying | Amazon Athena | S3 queries; workgroup cost controls |
| Warehouse | Redshift Serverless | Curated aggregations |
| BI | Amazon QuickSight | Dashboards connected to Athena + Redshift |
| Observability | Glue job metrics + Athena query stats + CloudWatch | |

---

## Cross-Pattern Defaults (always apply)

These decisions are pattern-agnostic and should always appear in the PDR regardless of which pattern is selected.

| Decision | Default |
|---|---|
| Multi-AZ | Always enabled for stateful services |
| Encryption at rest | AWS KMS, AES-256, for all storage services |
| Encryption in transit | TLS 1.2+ enforced; ACM certificates |
| Secrets management | Secrets Manager only; never environment variables for secrets |
| S3 bucket access | Block all public access; bucket policy explicit allow only |
| CloudTrail | Always enabled; management events + data events for S3 |
| AWS Config | Always enabled; capture resource compliance history |
| Tagging strategy | `Project`, `Environment`, `Owner`, `CostCenter` on all resources |
| Terraform state | S3 backend + DynamoDB lock table |
| Terraform version | Latest stable; pin in `required_version` |
