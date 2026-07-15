from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceMetric:
    service: str
    cpu_usage_percent: float
    memory_usage_percent: float
    request_rate_per_second: float
    error_rate_percent: float
    latency_p95_ms: float
    token_usage_per_hour: int
    estimated_cost_usd_per_hour: float

    def to_dict(self) -> dict[str, float | int | str]:
        return {
            "service": self.service,
            "cpu_usage_percent": self.cpu_usage_percent,
            "memory_usage_percent": self.memory_usage_percent,
            "request_rate_per_second": self.request_rate_per_second,
            "error_rate_percent": self.error_rate_percent,
            "latency_p95_ms": self.latency_p95_ms,
            "token_usage_per_hour": self.token_usage_per_hour,
            "estimated_cost_usd_per_hour": self.estimated_cost_usd_per_hour,
        }


def _normalize_service_name(raw: str) -> str:
    """Convert 'Amazon S3' -> 's3', 'AWS Lambda — API handlers' -> 'lambda'.

    Splits on em-dash (the typical separator between service and description
    in PDR tables) and on opening parenthesis (handles 'AWS ACM (Certificate
    Manager)'). Does NOT split on regular hyphen so service names like
    'X-Ray' and 'CodePipeline' survive intact.
    """
    name = re.sub(r"^(AWS|Amazon)\s+", "", raw.strip(), flags=re.IGNORECASE)
    name = re.split(r"—|\s*\(", name, maxsplit=1)[0]
    name = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return name


def extract_services_from_design(design_text: str) -> list[str]:
    """Parse the design doc and return AWS service names from any service table.

    Only picks rows whose first cell starts with 'AWS' or 'Amazon' (the
    convention used by the DESIGN agent's AWS Services Selected table). This
    is a structural check on the row format, not a hardcoded service list.
    Skips requirement IDs (r1, r2...), assumption IDs (a1, a2...), security
    tiers (Application, Authentication...), and observability signal types
    (Metrics, Logs, Traces...) which all appear in other tables.
    """
    services: set[str] = set()
    for line in design_text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        if re.match(r"^\|[\s\-:|]+\|$", line):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells:
            continue
        first = cells[0]
        if not first or first.lower() in ("service", "tier", "component", ""):
            continue
        if not re.match(r"^(AWS|Amazon)\b", first, re.IGNORECASE):
            continue
        normalized = _normalize_service_name(first)
        if normalized:
            services.add(normalized)
    return sorted(services)


def _deterministic_metrics(service_name: str) -> ServiceMetric:
    """Stable per-service metric values (hash-seeded). Same name -> same numbers."""
    h = hashlib.sha256(service_name.encode("utf-8")).digest()
    return ServiceMetric(
        service=service_name,
        cpu_usage_percent=30.0 + (h[0] % 60),
        memory_usage_percent=30.0 + (h[1] % 60),
        request_rate_per_second=5.0 + (h[2] % 500),
        error_rate_percent=round((h[3] % 50) / 10.0, 1),
        latency_p95_ms=50.0 + (h[4] % 2000),
        token_usage_per_hour=(h[5] << 8 | h[6]) * 10 if "llm" in service_name or "bedrock" in service_name else 0,
        estimated_cost_usd_per_hour=round(0.10 + (h[7] % 100) / 10.0, 2),
    )


def get_sample_metrics(services: list[str]) -> list[dict[str, float | int | str]]:
    return [_deterministic_metrics(s).to_dict() for s in services]


def service_health(services: list[str]) -> list[dict[str, str | float | int]]:
    health: list[dict[str, str | float | int]] = []
    for service in services:
        m = _deterministic_metrics(service)
        warnings: list[str] = []
        if m.cpu_usage_percent >= 75:
            warnings.append("high_cpu")
        if m.memory_usage_percent >= 80:
            warnings.append("high_memory")
        if m.error_rate_percent >= 2:
            warnings.append("high_error_rate")
        if m.latency_p95_ms >= 1_000:
            warnings.append("high_latency")

        status = "healthy"
        if warnings:
            status = "warning"
        if m.error_rate_percent >= 5 or m.latency_p95_ms >= 2_000:
            status = "critical"

        health.append({
            "service": m.service,
            "status": status,
            "signals": ", ".join(warnings) if warnings else "normal",
            "cpu_usage_percent": m.cpu_usage_percent,
            "memory_usage_percent": m.memory_usage_percent,
            "error_rate_percent": m.error_rate_percent,
            "latency_p95_ms": m.latency_p95_ms,
            "estimated_cost_usd_per_hour": m.estimated_cost_usd_per_hour,
        })
    return health


def recommended_promql_queries() -> list[dict[str, str]]:
    return [
        {"name": "sample_cpu_usage_percent", "promql": "saas_service_cpu_usage_percent",
         "purpose": "Read CPU usage from the local sample exporter."},
        {"name": "sample_memory_usage_percent", "promql": "saas_service_memory_usage_percent",
         "purpose": "Read memory usage from the local sample exporter."},
        {"name": "sample_error_rate_percent", "promql": "saas_service_error_rate_percent",
         "purpose": "Read service error rate from the local sample exporter."},
        {"name": "sample_latency_p95_ms", "promql": "saas_service_latency_p95_ms",
         "purpose": "Read p95 latency from the local sample exporter."},
        {"name": "sample_estimated_cost_usd_per_hour", "promql": "saas_service_estimated_cost_usd_per_hour",
         "purpose": "Read estimated hourly cost from the local sample exporter."},
        {"name": "production_cpu_usage_percent",
         "promql": '100 * (1 - avg by (service) (rate(node_cpu_seconds_total{mode="idle"}[5m])))',
         "purpose": "Find services or nodes under sustained CPU pressure."},
        {"name": "production_memory_usage_percent",
         "promql": "100 * (1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))",
         "purpose": "Find memory saturation before OOM risk."},
        {"name": "production_request_rate",
         "promql": "sum by (service) (rate(http_requests_total[5m]))",
         "purpose": "Measure incoming traffic by service."},
        {"name": "production_error_rate_percent",
         "promql": '100 * sum by (service) (rate(http_requests_total{status=~"5.."}[5m])) / sum by (service) (rate(http_requests_total[5m]))',
         "purpose": "Find services returning elevated 5xx responses."},
        {"name": "production_latency_p95_ms",
         "promql": "1000 * histogram_quantile(0.95, sum by (service, le) (rate(http_request_duration_seconds_bucket[5m])))",
         "purpose": "Track p95 request latency by service."},
    ]
