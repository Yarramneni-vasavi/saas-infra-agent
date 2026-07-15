from __future__ import annotations

import json
from pathlib import Path

from langchain.tools import tool

from saas_infra_agent.monitoring.prometheus import (
    PrometheusClient,
    PrometheusError,
    summarize_vector_result,
)
from saas_infra_agent.monitoring.simulation import (
    extract_services_from_design,
    get_sample_metrics,
    recommended_promql_queries,
    service_health,
)
from saas_infra_agent.observability.logger import get_logger


logger = get_logger(__name__)


def _architecture_path() -> Path:
       """Resolve the design doc the monitor agent should read.

       The upstream DESIGN agent writes ``pdr.md``. Older setups used
       ``architecture.md``. Prefer the new file, fall back to the old one,
       and only report "missing" if neither exists.
       """
       cwd = Path.cwd()
       for name in ("pdr.md", "architecture.md"):
           candidate = cwd / name
           if candidate.exists():
               return candidate
       return cwd / "pdr.md"


def _json(data: object) -> str:
    return json.dumps(data, indent=2, sort_keys=True)


@tool
def read_architecture_for_monitoring() -> str:
    """
    Read architecture.md before answering monitoring questions.

    Use this as the source of truth for expected services, deployment target,
    and observability assumptions. If it is missing, ask the user to complete
    the DESIGN agent flow first.
    """
    logger.info("Tool called: read_architecture_for_monitoring")
    path = _architecture_path()
    if not path.exists():
        return (
            "architecture.md is missing. Monitoring needs the approved design first. "
            "Ask the user to run the DESIGN agent and approve the architecture before "
            "requesting runtime health, metrics, or cost analysis."
        )
    return path.read_text(encoding="utf-8")


@tool
def get_simulated_service_metrics() -> str:
    """
    Return simulated metrics for the AWS services defined in the active design.

    Reads pdr.md (or architecture.md fallback) and generates deterministic
    metrics for each service. Use when real Prometheus data is unavailable,
    when the user asks for a demo, or for local validation.
    """
    logger.info("Tool called: get_simulated_service_metrics")
    design = read_architecture_for_monitoring.invoke({})
    if "is missing" in design:
        return _json({
            "source": "simulated",
            "ok": False,
            "error": "No approved design found. Run the DESIGN agent and approve before requesting metrics.",
        })
    services = extract_services_from_design(design)
    if not services:
        return _json({
            "source": "simulated",
            "ok": False,
            "error": "Could not find any services in the active design.",
            "design_excerpt": design[:500],
        })
    return _json({"source": "simulated", "ok": True, "services": get_sample_metrics(services)})


@tool
def get_simulated_service_health() -> str:
    """
    Return health classifications for the AWS services defined in the active design.

    Reads pdr.md (or architecture.md fallback), generates deterministic metrics
    per service, and classifies them as healthy / warning / critical.
    """
    logger.info("Tool called: get_simulated_service_health")
    design = read_architecture_for_monitoring.invoke({})
    if "is missing" in design:
        return _json({
            "source": "simulated",
            "ok": False,
            "error": "No approved design found. Run the DESIGN agent and approve before requesting health.",
        })
    services = extract_services_from_design(design)
    if not services:
        return _json({
            "source": "simulated",
            "ok": False,
            "error": "Could not find any services in the active design.",
        })
    return _json({"source": "simulated", "ok": True, "health": service_health(services)})


@tool
def get_recommended_promql_queries() -> str:
    """
    Return recommended PromQL queries for SaaS service monitoring.

    Use this when the user asks what PromQL should be used for CPU, memory,
    request rate, error rate, or p95 latency.
    """
    logger.info("Tool called: get_recommended_promql_queries")
    return _json({"queries": recommended_promql_queries()})


@tool
def query_prometheus(promql: str) -> str:
    """
    Run an instant PromQL query against Prometheus.

    Requires PROMETHEUS_URL or monitoring.prometheus_url. If Prometheus is not
    reachable, report the error and suggest using simulated metrics for local
    validation.
    """
    logger.info(f"Tool called: query_prometheus promql={promql!r}")
    try:
        data = PrometheusClient.from_config().query(promql)
    except PrometheusError as exc:
        return _json(
            {
                "source": "prometheus",
                "ok": False,
                "error": str(exc),
                "fallback": "Use get_simulated_service_metrics for local validation.",
            }
        )
    return _json(
        {
            "source": "prometheus",
            "ok": True,
            "result_type": data.get("resultType"),
            "results": summarize_vector_result(data),
        }
    )


@tool
def query_prometheus_range(promql: str, start: str, end: str, step: str = "60s") -> str:
    """
    Run a range PromQL query against Prometheus.

    Args:
        promql: PromQL expression.
        start: Start time as RFC3339 or Unix timestamp.
        end: End time as RFC3339 or Unix timestamp.
        step: Query resolution such as 30s, 60s, or 5m.
    """
    logger.info(
        f"Tool called: query_prometheus_range promql={promql!r} start={start!r} end={end!r} step={step!r}"
    )
    try:
        data = PrometheusClient.from_config().query_range(promql, start=start, end=end, step=step)
    except PrometheusError as exc:
        return _json(
            {
                "source": "prometheus",
                "ok": False,
                "error": str(exc),
                "fallback": "Use get_simulated_service_metrics for local validation.",
            }
        )
    return _json(
        {
            "source": "prometheus",
            "ok": True,
            "result_type": data.get("resultType"),
            "results": summarize_vector_result(data),
        }
    )

