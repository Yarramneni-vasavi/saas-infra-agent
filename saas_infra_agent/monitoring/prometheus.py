from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from saas_infra_agent.config.config import config
from saas_infra_agent.observability.logger import get_logger


logger = get_logger(__name__)


class PrometheusError(RuntimeError):
    """Raised when Prometheus returns an unusable response."""


@dataclass(frozen=True)
class PrometheusClient:
    """Small HTTP client for the Prometheus query API."""

    base_url: str
    timeout_s: float = 5.0

    @classmethod
    def from_config(cls) -> "PrometheusClient":
        monitor_cfg = (config.get("monitoring") or {}) if isinstance(config, dict) else {}
        base_url = os.getenv("PROMETHEUS_URL") or monitor_cfg.get("prometheus_url") or "http://localhost:9090"
        timeout_s = float(monitor_cfg.get("timeout_s", 5))
        return cls(base_url=str(base_url).rstrip("/"), timeout_s=timeout_s)

    def query(self, promql: str, time: str | None = None) -> dict[str, Any]:
        params = {"query": promql}
        if time:
            params["time"] = time
        return self._get("/api/v1/query", params)

    def query_range(
        self,
        promql: str,
        *,
        start: str,
        end: str,
        step: str,
    ) -> dict[str, Any]:
        params = {
            "query": promql,
            "start": start,
            "end": end,
            "step": step,
        }
        return self._get("/api/v1/query_range", params)

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"
        logger.info(f"Prometheus query url={url}")
        req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise PrometheusError(f"Prometheus request failed: {exc}") from exc

        if payload.get("status") != "success":
            raise PrometheusError(f"Prometheus returned non-success status: {payload}")
        return payload.get("data") or {}


def summarize_vector_result(data: dict[str, Any], max_series: int = 10) -> list[dict[str, Any]]:
    """Convert Prometheus vector/matrix data into compact LLM-friendly records."""

    result = data.get("result") or []
    summary: list[dict[str, Any]] = []
    for item in result[:max_series]:
        metric = item.get("metric") or {}
        if "value" in item:
            value = item.get("value") or []
            summary.append(
                {
                    "metric": metric,
                    "timestamp": value[0] if len(value) > 0 else None,
                    "value": value[1] if len(value) > 1 else None,
                }
            )
        elif "values" in item:
            values = item.get("values") or []
            summary.append(
                {
                    "metric": metric,
                    "points": len(values),
                    "first": values[0] if values else None,
                    "last": values[-1] if values else None,
                }
            )
    return summary

