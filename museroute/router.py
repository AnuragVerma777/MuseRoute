"""Policy-based cheap/strong Muse routing with a local response cache."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from pathlib import Path
from typing import Any

from . import client

_CACHE_PATH = Path(__file__).resolve().parents[1] / "results" / "router_cache.json"
_REFUSAL = re.compile(
    r"^\s*(?:sorry\b|i(?:'m| am) sorry\b|i (?:cannot|can't|am unable to)\b|as an ai language model\b)",
    re.IGNORECASE,
)


def _load_cache() -> dict[str, Any]:
    try:
        value = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_cache(cache: dict[str, Any]) -> None:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = _CACHE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(_CACHE_PATH)


def _cache_key(tier: str, messages: list[dict[str, Any]], params: dict[str, Any], sample: int) -> str:
    content = json.dumps(
        {"tier": tier, "messages": messages, "params": params, "sample": sample},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _cached_chat(
    cache: dict[str, Any],
    tier: str,
    messages: list[dict[str, Any]],
    params: dict[str, Any],
    sample: int,
) -> tuple[str, dict[str, Any], bool]:
    key = _cache_key(tier, messages, params, sample)
    saved = cache.get(key)
    if isinstance(saved, dict) and isinstance(saved.get("answer"), str) and isinstance(saved.get("stats"), dict):
        return saved["answer"], saved["stats"], True
    if isinstance(saved, dict) and isinstance(saved.get("error"), str):
        error_type = getattr(client, saved.get("error_type", "MuseRouteError"), client.MuseRouteError)
        error = error_type(saved["error"])
        error.cached = True
        raise error
    try:
        answer, stats = client.chat(tier, messages, **params)
    except Exception as exc:
        cache[key] = {
            "error_type": type(exc).__name__,
            "error": client.redact_secrets(exc),
        }
        _save_cache(cache)
        raise
    cache[key] = {"answer": answer, "stats": stats}
    _save_cache(cache)
    return answer, stats, False


def _with_confidence_request(messages: list[dict[str, Any]], output_format: str) -> list[dict[str, Any]]:
    instruction = (
        "Answer the user's request. Return only a JSON object with exactly these keys: "
        '"answer" (the requested answer as a string) and "confidence" (a number from 0 to 1). '
        "Confidence is your estimated probability that the answer is correct."
    )
    if output_format == "json":
        instruction += " The value of answer must itself be valid JSON text."
    prepared = [dict(message) for message in messages]
    if prepared and prepared[0].get("role") == "system":
        prepared[0]["content"] = f"{prepared[0].get('content', '')}\n\n{instruction}"
    else:
        prepared.insert(0, {"role": "system", "content": instruction})
    return prepared


def _parse_envelope(raw: str) -> tuple[str, float, str | None]:
    if not isinstance(raw, str) or not raw.strip():
        return "", 0.0, "empty response"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return "", 0.0, f"response is not valid JSON: {exc}"
    if not isinstance(parsed, dict) or "answer" not in parsed:
        return "", 0.0, 'response must be a JSON object with an "answer" field'
    value = parsed.get("answer")
    if value is None:
        answer = ""
    elif isinstance(value, str):
        answer = value
    else:
        answer = json.dumps(value, ensure_ascii=False)
    confidence_value = parsed.get("confidence", 0)
    confidence = 0.0
    if isinstance(confidence_value, (int, float)) and not isinstance(confidence_value, bool):
        number = float(confidence_value)
        if math.isfinite(number) and 0.0 <= number <= 1.0:
            confidence = number
    return answer, confidence, None


def _format_error(answer: str, output_format: str) -> str | None:
    if not answer.strip():
        return "answer field is empty"
    if _REFUSAL.search(answer):
        return "answer is a refusal"
    if output_format == "json":
        try:
            json.loads(answer)
        except json.JSONDecodeError as exc:
            return f"answer does not satisfy JSON format: {exc}"
    return None


def _agreement_key(answer: str, output_format: str) -> str:
    if output_format == "json":
        try:
            return json.dumps(json.loads(answer), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        except json.JSONDecodeError:
            pass
    return " ".join(answer.casefold().split())


def _record_call(
    calls: list[dict[str, Any]],
    tier: str,
    sample: int,
    stats: dict[str, Any] | None = None,
    error: Exception | None = None,
    cached: bool = False,
) -> None:
    row: dict[str, Any] = {"tier": tier, "sample": sample, "cached": cached}
    if error is not None:
        row.update({"status": "error", "error": f"{type(error).__name__}: {client.redact_secrets(error)}"})
    else:
        stats = stats or {}
        row.update(
            {
                "status": "ok",
                "prompt_tokens": stats.get("prompt_tokens"),
                "completion_tokens": stats.get("completion_tokens"),
                "latency_ms": stats.get("latency_ms"),
                "cost_units": stats.get("cost_units"),
            }
        )
    calls.append(row)


def _attempt(
    cache: dict[str, Any],
    calls: list[dict[str, Any]],
    tier: str,
    messages: list[dict[str, Any]],
    temperature: float,
    sample: int,
) -> tuple[str | None, float, dict[str, Any] | None, Exception | None]:
    started = time.perf_counter()
    try:
        answer, stats, cached = _cached_chat(
            cache,
            tier,
            messages,
            {"temperature": temperature},
            sample,
        )
    except Exception as exc:
        _record_call(calls, tier, sample, error=exc, cached=bool(getattr(exc, "cached", False)))
        calls[-1]["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return None, 0.0, None, exc
    _record_call(calls, tier, sample, stats=stats, cached=cached)
    parsed_answer, confidence, parse_error = _parse_envelope(answer)
    if parse_error:
        calls[-1]["answer"] = ""
        calls[-1]["confidence"] = None
        calls[-1]["parse_error"] = parse_error
        return None, 0.0, stats, ValueError(parse_error)
    calls[-1]["answer"] = parsed_answer
    calls[-1]["confidence"] = confidence
    return parsed_answer, confidence, stats, None


def _totals(calls: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [c for c in calls if c.get("status") == "ok"]
    prompt_tokens = [c.get("prompt_tokens") for c in successful]
    completion_tokens = [c.get("completion_tokens") for c in successful]
    latencies = [c.get("latency_ms") for c in calls]
    successful = [c for c in calls if c.get("status") == "ok"]
    costs = [c.get("cost_units") for c in successful]
    has_errors = any(c.get("status") != "ok" for c in calls)
    return {
        "total_prompt_tokens": (
            sum(prompt_tokens) if not has_errors and prompt_tokens and all(isinstance(v, int) for v in prompt_tokens) else None
        ),
        "total_completion_tokens": (
            sum(completion_tokens) if not has_errors and completion_tokens and all(isinstance(v, int) for v in completion_tokens) else None
        ),
        "total_latency_ms": (
            round(sum(latencies), 2) if latencies and all(isinstance(v, (int, float)) for v in latencies) else None
        ),
        "total_cost_units": (
            None if has_errors or not costs or any(value is None for value in costs) else sum(costs)
        ),
    }


def route(messages: list[dict[str, Any]], policy: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Try cheap first, escalating on format, confidence, or agreement failure.

    ``policy`` accepts ``threshold`` (default 0.5), ``format_check`` (``"text"``
    or ``"json"``), ``self_consistency`` (default false), and
    ``low_temperature`` (default 0.2).
    """
    if not isinstance(policy, dict):
        raise TypeError("policy must be a mapping")
    threshold = policy.get("threshold", 0.5)
    try:
        threshold = float(threshold)
    except (TypeError, ValueError) as exc:
        raise ValueError("policy.threshold must be a number between 0 and 1") from exc
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("policy.threshold must be a number between 0 and 1")
    output_format = str(policy.get("format_check", "text")).casefold()
    if output_format not in {"text", "json"}:
        raise ValueError("policy.format_check must be 'text' or 'json'")
    temperature = float(policy.get("low_temperature", 0.2))
    prepared = _with_confidence_request(messages, output_format)
    cache = _load_cache()
    calls: list[dict[str, Any]] = []
    cheap_answers: list[str] = []
    cheap_confidences: list[float] = []

    first_answer, first_confidence, _stats, cheap_error = _attempt(
        cache, calls, "cheap", prepared, temperature, 0
    )
    escalation_reason: str | None = None
    stop_for_payment = False
    if cheap_error:
        escalation_reason = f"cheap_call_error: {type(cheap_error).__name__}: {cheap_error}"
        if isinstance(cheap_error, client.FatalPaymentError):
            cheap_host = (client._TIERS.get("cheap") or {}).get("base_url")
            strong_host = (client._TIERS.get("strong") or {}).get("base_url")
            if cheap_host == strong_host:
                stop_for_payment = True
                escalation_reason += "; payment error on shared host; not retrying that host"
    else:
        assert first_answer is not None
        cheap_answers.append(first_answer)
        cheap_confidences.append(first_confidence)
        error = _format_error(first_answer, output_format)
        if error:
            escalation_reason = f"cheap_format_check_failed: {error}"

    if escalation_reason is None and policy.get("self_consistency", False):
        second_answer, second_confidence, _stats, second_error = _attempt(
            cache, calls, "cheap", prepared, temperature, 1
        )
        if second_error:
            escalation_reason = f"cheap_self_consistency_call_error: {type(second_error).__name__}: {second_error}"
        else:
            assert second_answer is not None
            cheap_answers.append(second_answer)
            cheap_confidences.append(second_confidence)
            error = _format_error(second_answer, output_format)
            if error:
                escalation_reason = f"cheap_format_check_failed: {error}"
            elif _agreement_key(cheap_answers[0], output_format) != _agreement_key(second_answer, output_format):
                escalation_reason = "cheap_self_consistency_disagreement"

    if escalation_reason is None and min(cheap_confidences, default=0.0) < threshold:
        escalation_reason = "cheap_confidence_below_threshold"

    tier_used: str | None = "cheap" if cheap_answers else None
    final_answer = cheap_answers[-1] if cheap_answers else ""
    escalated = False
    reason = escalation_reason or "cheap_answer_accepted"

    if escalation_reason and not stop_for_payment:
        escalated = True
        strong_answer, _strong_confidence, _stats, strong_error = _attempt(
            cache, calls, "strong", prepared, temperature, 0
        )
        if strong_error is None and strong_answer is not None:
            format_error = _format_error(strong_answer, output_format)
            if format_error:
                strong_error = ValueError(format_error)
            else:
                final_answer = strong_answer
                tier_used = "strong"
        if strong_error:
            if cheap_answers:
                final_answer = cheap_answers[-1]
                tier_used = "cheap"
                reason = f"{escalation_reason}; strong_call_error_fallback_to_cheap: {type(strong_error).__name__}: {strong_error}"
            else:
                tier_used = None
                final_answer = ""
                reason = f"{escalation_reason}; strong_call_error: {type(strong_error).__name__}: {strong_error}"
    elif stop_for_payment:
        tier_used = None
        final_answer = ""
        reason = escalation_reason or "payment_error_stop"

    trace = {
        "tier_used": tier_used,
        "reason": reason,
        "calls": calls,
        "escalated": escalated,
        **_totals(calls),
    }
    return final_answer, trace
