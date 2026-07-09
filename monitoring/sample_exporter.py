from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer


SERVICES = (
    {
        "service": "api-service",
        "cpu": 64.0,
        "memory": 71.5,
        "request_rate": 128.0,
        "error_rate": 1.2,
        "latency_p95": 240.0,
        "tokens": 18_000,
        "cost": 1.42,
    },
    {
        "service": "worker-service",
        "cpu": 78.5,
        "memory": 69.0,
        "request_rate": 42.0,
        "error_rate": 0.6,
        "latency_p95": 510.0,
        "tokens": 9_500,
        "cost": 0.96,
    },
    {
        "service": "db",
        "cpu": 52.0,
        "memory": 83.0,
        "request_rate": 310.0,
        "error_rate": 0.1,
        "latency_p95": 120.0,
        "tokens": 0,
        "cost": 2.35,
    },
    {
        "service": "cache",
        "cpu": 31.0,
        "memory": 45.0,
        "request_rate": 540.0,
        "error_rate": 0.0,
        "latency_p95": 18.0,
        "tokens": 0,
        "cost": 0.38,
    },
    {
        "service": "llm-gateway",
        "cpu": 47.0,
        "memory": 58.0,
        "request_rate": 24.0,
        "error_rate": 3.8,
        "latency_p95": 1_450.0,
        "tokens": 122_000,
        "cost": 6.75,
    },
)


def render_metrics() -> str:
    lines = [
        "# HELP saas_service_cpu_usage_percent Simulated service CPU usage.",
        "# TYPE saas_service_cpu_usage_percent gauge",
        "# HELP saas_service_memory_usage_percent Simulated service memory usage.",
        "# TYPE saas_service_memory_usage_percent gauge",
        "# HELP saas_service_request_rate_per_second Simulated request rate.",
        "# TYPE saas_service_request_rate_per_second gauge",
        "# HELP saas_service_error_rate_percent Simulated service error rate.",
        "# TYPE saas_service_error_rate_percent gauge",
        "# HELP saas_service_latency_p95_ms Simulated p95 latency.",
        "# TYPE saas_service_latency_p95_ms gauge",
        "# HELP saas_service_token_usage_per_hour Simulated LLM token usage.",
        "# TYPE saas_service_token_usage_per_hour gauge",
        "# HELP saas_service_estimated_cost_usd_per_hour Simulated cost estimate.",
        "# TYPE saas_service_estimated_cost_usd_per_hour gauge",
    ]
    for item in SERVICES:
        label = f'{{service="{item["service"]}"}}'
        lines.extend(
            [
                f"saas_service_cpu_usage_percent{label} {item['cpu']}",
                f"saas_service_memory_usage_percent{label} {item['memory']}",
                f"saas_service_request_rate_per_second{label} {item['request_rate']}",
                f"saas_service_error_rate_percent{label} {item['error_rate']}",
                f"saas_service_latency_p95_ms{label} {item['latency_p95']}",
                f"saas_service_token_usage_per_hour{label} {item['tokens']}",
                f"saas_service_estimated_cost_usd_per_hour{label} {item['cost']}",
            ]
        )
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return

        body = render_metrics().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()

