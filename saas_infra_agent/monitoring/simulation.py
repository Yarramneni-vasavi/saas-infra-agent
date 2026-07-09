from __future__ import annotations

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


SAMPLE_SERVICES: tuple[ServiceMetric, ...] = (
    ServiceMetric("api-service", 64.0, 71.5, 128.0, 1.2, 240.0, 18_000, 1.42),
    ServiceMetric("worker-service", 78.5, 69.0, 42.0, 0.6, 510.0, 9_500, 0.96),
    ServiceMetric("db", 52.0, 83.0, 310.0, 0.1, 120.0, 0, 2.35),
    ServiceMetric("cache", 31.0, 45.0, 540.0, 0.0, 18.0, 0, 0.38),
    ServiceMetric("llm-gateway", 47.0, 58.0, 24.0, 3.8, 1_450.0, 122_000, 6.75),
)


def get_sample_metrics() -> list[dict[str, float | int | str]]:
    return [metric.to_dict() for metric in SAMPLE_SERVICES]


def service_health() -> list[dict[str, str | float | int]]:
    health: list[dict[str, str | float | int]] = []
    for metric in SAMPLE_SERVICES:
        warnings: list[str] = []
        if metric.cpu_usage_percent >= 75:
            warnings.append("high_cpu")
        if metric.memory_usage_percent >= 80:
            warnings.append("high_memory")
        if metric.error_rate_percent >= 2:
            warnings.append("high_error_rate")
        if metric.latency_p95_ms >= 1_000:
            warnings.append("high_latency")

        status = "healthy"
        if warnings:
            status = "warning"
        if metric.error_rate_percent >= 5 or metric.latency_p95_ms >= 2_000:
            status = "critical"

        health.append(
            {
                "service": metric.service,
                "status": status,
                "signals": ", ".join(warnings) if warnings else "normal",
                "cpu_usage_percent": metric.cpu_usage_percent,
                "memory_usage_percent": metric.memory_usage_percent,
                "error_rate_percent": metric.error_rate_percent,
                "latency_p95_ms": metric.latency_p95_ms,
                "estimated_cost_usd_per_hour": metric.estimated_cost_usd_per_hour,
            }
        )
    return health


def recommended_promql_queries() -> list[dict[str, str]]:
    return [
        {
            "name": "sample_cpu_usage_percent",
            "promql": "saas_service_cpu_usage_percent",
            "purpose": "Read CPU usage from the local sample exporter.",
        },
        {
            "name": "sample_memory_usage_percent",
            "promql": "saas_service_memory_usage_percent",
            "purpose": "Read memory usage from the local sample exporter.",
        },
        {
            "name": "sample_error_rate_percent",
            "promql": "saas_service_error_rate_percent",
            "purpose": "Read service error rate from the local sample exporter.",
        },
        {
            "name": "sample_latency_p95_ms",
            "promql": "saas_service_latency_p95_ms",
            "purpose": "Read p95 latency from the local sample exporter.",
        },
        {
            "name": "sample_estimated_cost_usd_per_hour",
            "promql": "saas_service_estimated_cost_usd_per_hour",
            "purpose": "Read estimated hourly cost from the local sample exporter.",
        },
        {
            "name": "production_cpu_usage_percent",
            "promql": '100 * (1 - avg by (service) (rate(node_cpu_seconds_total{mode="idle"}[5m])))',
            "purpose": "Find services or nodes under sustained CPU pressure.",
        },
        {
            "name": "production_memory_usage_percent",
            "promql": "100 * (1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))",
            "purpose": "Find memory saturation before OOM risk.",
        },
        {
            "name": "production_request_rate",
            "promql": "sum by (service) (rate(http_requests_total[5m]))",
            "purpose": "Measure incoming traffic by service.",
        },
        {
            "name": "production_error_rate_percent",
            "promql": '100 * sum by (service) (rate(http_requests_total{status=~"5.."}[5m])) / sum by (service) (rate(http_requests_total[5m]))',
            "purpose": "Find services returning elevated 5xx responses.",
        },
        {
            "name": "production_latency_p95_ms",
            "promql": "1000 * histogram_quantile(0.95, sum by (service, le) (rate(http_request_duration_seconds_bucket[5m])))",
            "purpose": "Track p95 request latency by service.",
        },
    ]
