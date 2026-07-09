# Local Monitoring Stack

This folder provides a local Prometheus setup for the MONITOR agent.

## Start

```bash
docker compose -f monitoring/docker-compose.yml up
```

Prometheus runs at:

```text
http://localhost:9090
```

The sample exporter runs at:

```text
http://localhost:8000/metrics
```

## Sample PromQL

```promql
saas_service_cpu_usage_percent
saas_service_memory_usage_percent
saas_service_error_rate_percent
saas_service_latency_p95_ms
saas_service_token_usage_per_hour
saas_service_estimated_cost_usd_per_hour
```

## Agent Usage

Before asking monitor questions, complete the DESIGN flow so `architecture.md`
exists in the project root. Then use prompts like:

```text
/monitor show service health using simulated data
/monitor what PromQL should I use for latency and error rate?
/monitor query Prometheus for saas_service_latency_p95_ms
```

