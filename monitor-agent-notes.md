# Monitor Agent Notes

Branch: `memory-agent`

This note captures what was built for the monitor agent, why each part exists, how the local test stack works, and the main questions that came up while building it.

## 1. What Was Built

The project now has a monitor-agent path that can:

- Read `architecture.md` before answering monitoring questions.
- Use simulated metrics for local validation.
- Query a live local Prometheus instance with PromQL.
- Return compact JSON-friendly results to the LLM or caller.
- Fall back cleanly when Prometheus is unavailable.

### Main Files Added or Changed

- [saas_infra_agent/agent/agents.py](saas_infra_agent/agent/agents.py)
- [saas_infra_agent/agent/tools/monitoring.py](saas_infra_agent/agent/tools/monitoring.py)
- [saas_infra_agent/monitoring/prometheus.py](saas_infra_agent/monitoring/prometheus.py)
- [saas_infra_agent/monitoring/simulation.py](saas_infra_agent/monitoring/simulation.py)
- [monitoring/docker-compose.yml](monitoring/docker-compose.yml)
- [monitoring/prometheus.yml](monitoring/prometheus.yml)
- [monitoring/sample_exporter.py](monitoring/sample_exporter.py)
- [monitoring/README.md](monitoring/README.md)
- [saas_infra_agent/config.yaml](saas_infra_agent/config.yaml)
- [.env.example](.env.example)

## 2. Why It Was Built This Way

### Why `agents.py`

This file is the central agent factory. It already decides whether the user goes to `design`, `build`, or `monitor`, so it is the right place to register the monitor tools and monitor prompt.

### Why `agent/tools/monitoring.py`

This file is the LangChain tool layer. The monitor agent needs explicit tools for:

- reading `architecture.md`
- simulated metrics
- simulated health
- PromQL query examples
- live Prometheus queries

Keeping those as tools makes them usable by the monitor agent without mixing them into unrelated code.

### Why `saas_infra_agent/monitoring/`

This folder is for the actual monitoring logic, not tool wrappers.

- `prometheus.py` handles HTTP calls and response parsing.
- `simulation.py` holds the sample data and sample PromQL.

This separation keeps the code testable and avoids coupling Prometheus logic to the agent runtime.

### Why `monitoring/` at the repo root

This folder is for local runtime files only:

- Docker Compose
- Prometheus config
- sample exporter
- local usage notes

That keeps the Python package clean and makes the local stack easy to run.

## 3. Internal Flow

The local flow is:

1. User asks a monitor question.
2. Router sends it to the monitor agent.
3. Monitor agent reads `architecture.md`.
4. Monitor agent either:
   - uses simulated metrics, or
   - queries Prometheus with PromQL.
5. Results are summarized into a compact response.

## 4. Local Test Stack

The local stack is running with:

- Prometheus on `http://localhost:9090`
- Sample exporter on `http://localhost:8000/metrics`

Docker Compose file:

[monitoring/docker-compose.yml](monitoring/docker-compose.yml)

Prometheus scrape config:

[monitoring/prometheus.yml](monitoring/prometheus.yml)

Sample exporter:

[monitoring/sample_exporter.py](monitoring/sample_exporter.py)

## 5. PromQL To Check

Use these in Prometheus UI or via curl:

```promql
up
saas_service_latency_p95_ms
saas_service_error_rate_percent
saas_service_token_usage_per_hour
saas_service_estimated_cost_usd_per_hour
```

## 6. Sample Services

The simulated/local data models 5 services:

- `api-service`
- `worker-service`
- `db`
- `cache`
- `llm-gateway`

Each service exposes:

- CPU usage
- memory usage
- request rate
- error rate
- p95 latency
- token usage
- estimated cost

## 7. Why `architecture.md` Matters

The monitor agent needs the design handoff first. It is treated as source of truth for:

- expected services
- deployment context
- observability assumptions

If it is missing, the monitor tool returns a direct message telling the user to complete the design flow first.

## 8. Questions And Answers

### Q: Can I test it locally?

Yes. The deterministic local test path is working.

### Q: Do I need OpenAI for local validation?

No, not for the Prometheus/simulation test path.

### Q: Where do I check Prometheus?

- UI: `http://localhost:9090`
- Sample metrics: `http://localhost:8000/metrics`
- API: `http://localhost:9090/api/v1/query`

### Q: What happens if Prometheus is down?

The tool returns a structured fallback error and suggests simulated metrics.

### Q: Why is `architecture.md` required?

Because the monitor agent is not meant to guess the system shape. It reads the design first, then explains runtime health against that contract.

### Q: Why use Docker and Prometheus locally?

So you can test the monitor agent against live metrics without needing production infrastructure.

### Q: Why is tree-sitter not part of this local monitoring test?

Tree-sitter is for code indexing and retrieval. It is unrelated to the monitor stack, so it was intentionally left out of the local monitoring setup.

## 9. What Was Verified

- `architecture.md` exists in the project root.
- Docker containers are running.
- Prometheus answers `up`.
- Sample exporter exposes local metrics.
- Monitor tools can read `architecture.md`.
- Monitor tools can query Prometheus for latency, errors, cost, and token usage.

## 10. Practical Commands

Start stack:

```bash
docker-compose -f monitoring/docker-compose.yml up -d
```

Check containers:

```bash
docker ps
```

Query Prometheus:

```bash
curl -s 'http://localhost:9090/api/v1/query?query=up'
curl -s 'http://localhost:9090/api/v1/query?query=saas_service_latency_p95_ms'
```

Test monitor tools:

```bash
poetry run python - <<'PY'
from saas_infra_agent.agent.tools.monitoring import read_architecture_for_monitoring, query_prometheus
print(read_architecture_for_monitoring.invoke({})[:120])
print(query_prometheus.invoke({"promql": "saas_service_latency_p95_ms"})[:120])
PY
```

