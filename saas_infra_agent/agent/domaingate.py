"""Domain gate for saas-cli.

This CLI is intentionally narrow: SaaS infrastructure design/build/monitor.
We use a small LLM check (in the orchestrator) to decide if the message is
in-domain; out-of-domain requests get a short deflection.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage

from saas_infra_agent.llm.factory import get_small_llm

DomainFlag = Literal["in_domain", "out_of_domain", "unclear"]

_SHORT_ACKS = {
    "y", "yes", "ok", "okay", "proceed", "continue",
    "n", "no", "nope", "cancel", "stop",
}


@dataclass(frozen=True)
class DomainResult:
    flag: DomainFlag
    reason: str = ""


def _coerce_compact_json(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return t
    start = t.find("{")
    end = t.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return t
    return t[start : end + 1]


def _llm_domain_check(query: str) -> DomainResult:
    system = (
        "You are the domain gate for a CLI called saas-cli. "
        "The ONLY supported domain is SaaS infrastructure: cloud architecture, "
        "deployment, IaC, CI/CD, networking, security, reliability, scaling, "
        "monitoring/observability, and cost optimization.\n\n"
        "Classify the user's message as in-domain or out-of-domain.\n\n"
        "Return ONLY compact JSON, no prose, no markdown fences:\n"
        '{"in_domain": true | false, "reasoning": "<one sentence>"}\n\n'
        'Examples out-of-domain: "place an order on Myntra", "write a poem", '
        '"solve my math homework", "relationship advice".\n'
        "If the message is ambiguous but could plausibly be infra-related, "
        "set in_domain=true."
    )

    llm = get_small_llm()
    resp = llm.invoke([
        SystemMessage(content=system),
        HumanMessage(content=(query or "").strip()),
    ])
    text = getattr(resp, "content", str(resp)).strip()
    try:
        data = json.loads(_coerce_compact_json(text))
        in_domain = data.get("in_domain")
        reasoning = data.get("reasoning", "")
        if not isinstance(in_domain, bool):
            raise ValueError("Invalid in_domain field.")
        if not isinstance(reasoning, str):
            raise ValueError("Invalid reasoning field.")
        return DomainResult(
            flag="in_domain" if in_domain else "out_of_domain",
            reason=reasoning.strip(),
        )
    except Exception:
        # Fail open: ambiguous parsing shouldn't block legitimate infra usage.
        return DomainResult(flag="unclear", reason="Domain check response could not be parsed.")


def check_domain(query: str) -> DomainResult:
    q = (query or "").strip().lower()
    if not q:
        return DomainResult(flag="unclear", reason="Empty query.")

    # Let acknowledgement replies through (they're usually for approval/interrupt flows).
    if q in _SHORT_ACKS:
        return DomainResult(flag="in_domain", reason="Ack reply.")

    return _llm_domain_check(query)
