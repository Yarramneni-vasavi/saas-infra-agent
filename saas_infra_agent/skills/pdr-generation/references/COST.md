---
name: pdr-architecture/cost
description: "Cost estimation methodology, per-service pricing levers, and reserved instance guidance for SaaS infrastructure PDR documents. Read when populating Section 8 (Cost Estimate) of the PDR. Always use web search for actual $/unit values — never use figures from this file."
---

# Cost Estimation Reference — SaaS Infrastructure PDR

> ⚠️ **This file contains NO dollar figures.** All $/unit values MUST come from web search or the AWS Pricing Calculator at the time of PDR generation. Figures baked into skill files become silently wrong within weeks of an AWS pricing change.

This file defines the **methodology** for estimating costs: which dimensions to measure, which levers drive cost, and how to reason about optimization trade-offs.

---

## Estimation Methodology

For each service in the PDR Services table, compute the monthly cost estimate as:

```
Monthly Cost = (Unit Count) × (Unit Price) × (Hours or GBs or Requests per Month)
```

Fetch `Unit Price` from web search: `"AWS <service> pricing <current year>"` or directly from `https://aws.amazon.com/pricing/`.

Round all estimates **up** to the nearest $5. Present a range (low / expected / high) when a dimension is uncertain (e.g., data transfer volume).

---

## Cost Dimensions by Service

### Compute

**ECS Fargate**
- Dimensions: vCPU-hours, GB-hours, ephemeral storage GB-hours
- Key lever: task size × running hours × number of tasks
- Cost driver: right-sizing is the single biggest lever; halving vCPU allocation roughly halves cost
- Optimization: Fargate Spot for non-critical workloads (up to 70% savings, but tasks can be interrupted)

**Lambda**
- Dimensions: number of invocations, GB-seconds (memory × duration)
- Key lever: memory allocation (also affects CPU allocation in Lambda)
- Cost driver: duration × memory; very long functions are expensive
- Optimization: right-size memory using Lambda Power Tuning tool

**EC2 (if used)**
- Dimensions: instance-hours, instance type
- Key lever: On-Demand vs Reserved vs Savings Plan
- Reserved Instances: 1-year No Upfront ≈ 30-40% savings; 3-year All Upfront ≈ 55-60% savings
- Savings Plans: Compute Savings Plans apply across EC2, Fargate, Lambda automatically

### Database

**Aurora PostgreSQL / MySQL**
- Dimensions: ACU-hours (Serverless v2) or instance-hours (provisioned), storage GB-month, I/O requests (if standard mode), backup storage GB-month
- Key lever: Aurora Serverless v2 eliminates over-provisioning; cost scales to actual usage
- Aurora I/O-Optimized mode: eliminates per-I/O charges; break-even at ~25% I/O cost as % of total Aurora bill
- Optimization: Use Aurora Serverless v2 for workloads with variable traffic; provisioned for steady high-traffic

**RDS (non-Aurora)**
- Dimensions: instance-hours, storage GB-month, backup storage, Multi-AZ doubles instance cost
- Key lever: Multi-AZ adds ~100% on instance cost but is required for production

**DynamoDB**
- Dimensions: on-demand (read/write request units) or provisioned (RCU/WCU-hours), storage GB-month, optional DAX node-hours
- Key lever: On-demand vs provisioned — on-demand has higher per-request cost but no minimum; provisioned needs careful capacity planning
- Optimization: Use on-demand for unpredictable traffic; switch to provisioned + reserved capacity once traffic pattern is stable

**ElastiCache Redis**
- Dimensions: node-hours (provisioned) or ECPU/GB-hours (Serverless)
- Serverless is cost-effective for bursty or low-baseline workloads

### Networking

**ALB**
- Dimensions: ALB-hours + Load Balancer Capacity Units (LCU-hours)
- LCUs are driven by: new connections/sec, active connections, bandwidth, rule evaluations
- Key lever: at low traffic, ALB fixed hour cost dominates; at high traffic, LCU cost dominates

**NAT Gateway**
- Dimensions: NAT GW-hours + GB processed
- Key lever: data processing cost is often the largest bill item at scale
- Optimization: VPC Endpoints for S3 and DynamoDB eliminate NAT GW data transfer for those services (free); can reduce NAT GW bill significantly

**Data Transfer**
- Dimensions: GB out to internet, GB between AZs (same-region)
- Cross-AZ traffic: charged in both directions; ALB → ECS in different AZ incurs cross-AZ charges
- Optimization: Pin ECS tasks to same AZ as RDS read replica for read-heavy workloads

**CloudFront**
- Dimensions: GB transferred out, HTTPS requests, Lambda@Edge invocations
- Key lever: CloudFront data transfer out is cheaper than ALB/EC2 data transfer out to internet
- Always cheaper for global or bandwidth-heavy workloads

### Storage

**S3**
- Dimensions: GB-month (by storage class), PUT/GET/LIST requests, data retrieval (for Glacier)
- Storage class ladder: Standard → Standard-IA (30+ day objects, infrequent access) → Glacier Instant (90+ day) → Glacier Flexible (rare access)
- Optimization: Lifecycle rules to tier objects automatically; Intelligent-Tiering for unknown access patterns

### Observability

**CloudWatch**
- Dimensions: custom metrics (per metric per month), log ingestion (GB), log storage (GB-month), dashboard (per dashboard per month), alarms (per alarm per month)
- Key lever: log ingestion is usually the largest CloudWatch cost at scale
- Optimization: Structured JSON logs with log-level filtering at Lambda/ECS (don't log DEBUG in production); export to S3 after 7 days rather than retaining in CloudWatch

**X-Ray**
- Dimensions: traces recorded (per million), traces scanned during queries (per million)
- Sampling: default 5% sampling recommended for production; adjust based on RPS and budget

---

## Cost Estimate Template for PDR Section 8

```markdown
### Cost Estimate

All figures are monthly estimates based on the workload profile in Section 3 (Requirements Summary).
Actual $/unit values retrieved from AWS pricing pages on [DATE OF PDR].

| Service | Config | Quantity | Unit | Est. Monthly |
|---|---|---|---|---|
| ECS Fargate | 2 vCPU / 4 GB, ~720 hrs/mo | 2 tasks average | vCPU-hr + GB-hr | $XXX |
| Aurora Serverless v2 | Min 0.5, Max 8 ACU | ~3 ACU average | ACU-hr | $XXX |
| ALB | 500 RPS average | 1 ALB + X LCU | ALB-hr + LCU-hr | $XXX |
| NAT Gateway | ~50 GB/mo processed | 2 AZs | GW-hr + GB | $XXX |
| ElastiCache Serverless | ~1 GB cache | Variable | ECPU + GB-hr | $XXX |
| S3 | 100 GB storage, 1M requests | Standard class | GB-mo + requests | $XXX |
| CloudWatch | 5 GB logs/day | Ingest + 30d retain | GB ingest + GB-mo | $XXX |
| Secrets Manager | 10 secrets, 10K API calls | | secret-mo + API calls | $XXX |
| **Subtotal** | | | | **$XXX** |
| **Contingency (+15%)** | | | | **$XXX** |
| **Total Estimate** | | | | **$XXX/month** |

Budget ceiling (R5): $X,XXX/month
Status: ✅ Within budget / ⚠️ Exceeds budget by $XXX
```

---

## Cost Overrun Playbook

If the estimate exceeds R5 (budget ceiling), present these levers in order of impact. List only levers applicable to the chosen pattern.

### Lever 1 — Compute Right-sizing (immediate, high impact)

- Reduce ECS Fargate task size (e.g., 2 vCPU → 1 vCPU if CPU utilization target allows)
- Reduce minimum task count (e.g., min 2 → min 1 with careful health check config)
- Enable Fargate Spot for non-production environments
- Lambda memory right-sizing with Power Tuning

### Lever 2 — Reserved Capacity (medium-term, high impact)

- Compute Savings Plan (1-year, no upfront) applies automatically to ECS Fargate and Lambda
- Aurora Reserved Instances for provisioned clusters with stable load
- ElastiCache Reserved Nodes for provisioned Redis

### Lever 3 — Architecture Optimizations (design change, medium impact)

- Replace NAT Gateway with VPC Endpoints for S3 and DynamoDB access
- Move static assets to S3 + CloudFront to reduce ALB LCU cost
- Aurora I/O-Optimized if I/O cost > 25% of Aurora bill
- Reduce log retention (30 days → 7 days) and export to S3 Glacier

### Lever 4 — Pattern Shift (significant change, consider carefully)

- Three-Tier VPC → Serverless (P1 → P2): eliminates ALB, NAT GW, Fargate fixed costs
- Only recommend if workload profile actually fits P2 (re-run decision tree)

---

## Cost Tagging Strategy

All resources must carry these tags for cost allocation:

| Tag Key | Example Values | Purpose |
|---|---|---|
| `Project` | `saas-infra-agent` | Top-level cost bucket |
| `Environment` | `prod`, `staging`, `dev` | Environment-level breakdown |
| `Owner` | `platform-team` | Team-level accountability |
| `CostCenter` | `engineering` | Finance allocation |
| `Component` | `api`, `database`, `monitoring` | Per-component cost analysis |

Enable AWS Cost Explorer tag-based cost allocation after tagging all resources. Set up a monthly budget with 80% and 100% threshold alerts → SNS → email.
