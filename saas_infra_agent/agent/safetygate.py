"""Lightweight safety gate.

Runs on every user turn -- before intent classification, and before any
continuation fast-path. Kept cheap on purpose: a regex pre-filter first,
and the LLM only gets called when the pre-filter is ambiguous, not on
every single message.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from saas_infra_agent.llm.factory import get_small_llm

SafetyFlag = Literal["none", "needs_review", "block"]

# Patterns unambiguous enough to block without an LLM call at all.
# Keep this list narrow and high-precision: false positives here block
# legitimate infra questions. The LLM fallback below is the real backstop
# for anything less clear-cut.
_HARD_BLOCK_PATTERNS = [
    r"\bhow (do|can) i (hack|break into|gain unauthorized access)\b",
    r"\bexploit (this|the) (system|server|infra)\b",
    r"\bbypass (auth|authentication|security controls?)\b",
    r"\bexfiltrate (data|credentials)\b",
]
_HARD_BLOCK_REGEXES = [re.compile(p) for p in _HARD_BLOCK_PATTERNS]

# Words worth a second look, but never blockable on their own -- "pen test",
# "vulnerability scan", "rotate credentials" are all legitimate infra asks.
_REVIEW_TRIGGERS = [
    "penetration test", "pen test", "vulnerability", "exploit",
    "credentials", "bypass", "privilege escalation", "backdoor",
]


@dataclass
class SafetyResult:
    flag: SafetyFlag
    reasoning: str


def _extract_response_text(resp) -> str:
    """Handle both string and block-based LangChain response content."""
    content = getattr(resp, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            else:
                text = getattr(item, "text", None)
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part.strip() for part in parts if part.strip()).strip()
    return str(content).strip()

def _coerce_compact_json(text: str) -> str:
    """Best-effort extraction of a single JSON object from a model response."""
    t = text.strip()
    if not t:
        return t
    start = t.find("{")
    end = t.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return t
    return t[start : end + 1]


def _regex_prefilter(query: str) -> SafetyFlag:
    q = query.lower()
    for rx in _HARD_BLOCK_REGEXES:
        if rx.search(q):
            return "block"
    for trigger in _REVIEW_TRIGGERS:
        if trigger in q:
            return "needs_review"
    return "none"


def _llm_safety_check(query: str) -> SafetyResult:
    """Second-opinion check for anything the regex prefilter flagged.
    Deliberately small: one short prompt, structured JSON out, no
    conversation history -- it only has to judge this single turn.
    """
    max_chars = 4000
    query_for_llm = query.strip()
    if len(query_for_llm) > max_chars:
        half = max_chars // 2
        query_for_llm = f"{query_for_llm[:half]}\n...\n{query_for_llm[-half:]}"

    system = (
        "You are a safety gate for an infrastructure design/build/monitor "
        "assistant. Classify the user's message.\n\n"
        "Return ONLY compact JSON, no prose, no markdown fences:\n"
        '{"flag": "none" | "needs_review" | "block", "reasoning": "<one sentence>"}\n\n'
        '- "block": the user is asking how to gain unauthorized access, '
        "exploit, attack, or damage a system that is not clearly their own, "
        "or requests any other unethical/illegal action.\n"
        '- "needs_review": legitimate-sounding but touches sensitive '
        "territory (security testing, credential handling, access control "
        "changes) -- allow it through, but flag for downstream awareness.\n"
        '- "none": an ordinary infra/design/build/monitor question.\n'
        "Legitimate requests (e.g. 'set up a WAF', 'run a vulnerability "
        "scan on our own staging env', 'rotate these credentials') are "
        '"none" or "needs_review", never "block".'
    )

    llm = get_small_llm()
    resp = llm.invoke([
        SystemMessage(content=system),
        HumanMessage(content=query_for_llm),
    ])

    text = _extract_response_text(resp)
    try:
        data = json.loads(_coerce_compact_json(text))
        flag = data.get("flag")
        reasoning = data.get("reasoning", "")
        if flag not in {"none", "needs_review", "block"}:
            raise ValueError("Invalid safety flag.")
        if not isinstance(reasoning, str):
            raise ValueError("Invalid safety reasoning.")
        return SafetyResult(flag=flag, reasoning=reasoning.strip())
    except Exception:
        # Fail closed on parse errors -- don't silently let an unparseable
        # response through as "none".
        return SafetyResult(
            flag="needs_review",
            reasoning="Safety check response could not be parsed.",
        )


def check_safety(query: str) -> SafetyResult:
    """Entry point. Cheap regex pass first; LLM only called when the
    prefilter is ambiguous or flags something borderline.
    """
    pre = _regex_prefilter(query)
    if pre == "none":
        return SafetyResult(flag="none", reasoning="")
    if pre == "block":
        return SafetyResult(flag="block", reasoning="Request appears to ask for unauthorized access or exploitation.")
    return _llm_safety_check(query)
