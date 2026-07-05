---
name: serverless-api
description: Design infrastructure for serverless APIs. Use for Lambda-based applications, event-driven APIs, webhooks, and bursty workloads.
last_updated: "2026-07-05"
doc_source: internal://design-agent/workloads/serverless-api
---

# Serverless-Api Workload

## When to Use

Describe a project that matches this workload.

## Typical Characteristics

- Primary workload
- Expected traffic pattern
- Scalability requirements
- Availability requirements

## Recommended AWS Services

> These are candidate services. The Design Agent should choose based on project requirements.

- Compute
- Networking
- Database
- Cache
- Messaging
- Storage
- Monitoring

## Design Considerations

- High Availability
- Scalability
- Security
- Cost Optimization
- Operational Simplicity

## Related Decision Skills

- ecs-vs-eks
- ec2-vs-fargate
- rds-vs-aurora
- lambda-vs-ecs

## Related AWS Skills

- ecs
- eks
- ec2
- lambda
- rds
- s3
- cloudwatch

## Example Prompt

Build a serverless-api application for 20,000 daily active users on AWS.

