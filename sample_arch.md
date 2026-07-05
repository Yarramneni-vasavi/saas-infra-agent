# Architecture plan

## Current recommendation
_Last updated: 2026-07-04T12:00:00+00:00_

### Requirements
Building a RAG pipeline for 10,000 daily users with sub-2s latency. Documents
are re-indexed nightly; queries are read-heavy with bursty traffic during
business hours (roughly 8am-6pm local time).

### Recommended stack
- Compute: 2x general-purpose VMs (4 vCPU each) behind a load balancer, autoscaling to 4 during peak hours
- Vector DB: managed vector database, ~2M vectors (document chunks)
- LLM: claude-haiku-4-5 for retrieval/reranking, claude-sonnet-5 for final answer generation
- Object storage: for raw source documents and nightly re-index artifacts
- API gateway: fronting the query endpoint, with request-level auth

### Cost estimate
- Compute: ~$115/month (2 vCPU-hours/day avg * 30 days * $0.04, plus peak autoscale headroom)
- Vector DB: ~$50/month (2M vectors, managed tier)
- LLM (blended haiku + sonnet): ~$180/month at 10k users * ~3 queries/day * ~1.5k tokens/query
- Object storage: ~$10/month
- API gateway: ~$5/month at this request volume
- **Total: ~$360/month**

### Trade-offs
- Managed vector DB costs more than self-hosted pgvector but removes ops burden
  and scales without re-architecting past ~10M vectors.
- Using haiku for retrieval/reranking and reserving sonnet for final generation
  keeps latency under the 2s target; an all-sonnet pipeline would blow the
  latency budget and roughly double LLM spend.
- Autoscaling compute to 4 VMs during business hours costs more than a fixed
  2-VM setup, but a fixed setup risks latency SLA breaches during peak load.

## History
- 2026-07-04T12:00:00+00:00 — Initial RAG pipeline plan for 10k daily users, sub-2s latency target
