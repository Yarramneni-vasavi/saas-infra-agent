---
name: monolith
description: Design infrastructure for traditional monolithic web applications. Use for MVPs, small teams, and single deployable backend applications.
last_updated: "2026-07-05"
doc_source: internal://design-agent/workloads/monolith
---

# Monolith Workload

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

Build a monolith application for 20,000 daily active users on AWS.

