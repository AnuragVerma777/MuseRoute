"""FastAPI service exposing MuseRoute through chat-completions-style endpoints."""

from __future__ import annotations

import os
import json
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import calculator, client, router

app = FastAPI(title="MuseRoute", version="0.1.0")
_OPERATING_POINT_PATH = Path(__file__).resolve().parents[1] / "results" / "sweep.json"
_FACTS_PATH = Path(__file__).resolve().parents[1] / "results" / "facts.json"
_STATIC_DIR = Path(__file__).with_name("static")
_WORKLOAD_PATH = Path(__file__).resolve().parents[1] / "data" / "workload.jsonl"
_ANSWER_CACHE_PATH = Path(__file__).resolve().parents[1] / "results" / "answer_cache.json"
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


def _read_demo_sources() -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    try:
        workload = [json.loads(line) for line in _WORKLOAD_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
        answer_cache = json.loads(_ANSWER_CACHE_PATH.read_text(encoding="utf-8"))
        sweep = json.loads(_OPERATING_POINT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=503, detail=f"Cached demo data is unavailable: {exc}") from exc
    if not isinstance(answer_cache, dict) or not isinstance(sweep, dict):
        raise HTTPException(status_code=503, detail="Cached demo data has an invalid format")
    return workload, answer_cache, sweep


def _cache_entry(answer_cache: dict[str, Any], tier: str, prompt_id: str) -> dict[str, Any] | None:
    entry = answer_cache.get(f"{tier}|{prompt_id}|t0.2|a0")
    if isinstance(entry, dict) and isinstance(entry.get("answer"), str) and isinstance(entry.get("stats"), dict):
        return entry
    return None


def _demo_records() -> list[dict[str, Any]]:
    _validate_configured_models()
    workload, answer_cache, sweep = _read_demo_sources()
    holdout = ((sweep.get("operating_point") or {}).get("holdout") or {}).get("details") or []
    prompts = {row.get("id"): row for row in workload if isinstance(row, dict)}
    by_id = {row.get("id"): row for row in holdout if isinstance(row, dict)}
    selected: list[dict[str, Any]] = []
    for band in ("easy", "medium", "hard"):
        candidates = []
        for prompt_id, decision in by_id.items():
            prompt = prompts.get(prompt_id)
            if not prompt or prompt.get("band") != band or not decision.get("answer"):
                continue
            cheap = _cache_entry(answer_cache, "cheap", prompt_id)
            strong = _cache_entry(answer_cache, "strong", prompt_id)
            if cheap is None or strong is None:
                continue
            candidates.append((prompt_id, prompt, decision, cheap, strong))
        for prompt_id, prompt, decision, cheap, strong in sorted(candidates, key=lambda item: item[0])[:2]:
            selected.append({
                "id": prompt_id,
                "band": band,
                "prompt": prompt["prompt"],
                "decision": decision,
                "cheap_cache": cheap,
                "strong_cache": strong,
            })
    if len(selected) != 6:
        raise HTTPException(
            status_code=503,
            detail=f"Cached demo needs 2 successful cached prompts per band; found {len(selected)} total.",
        )
    return selected


def _find_demo_record(payload: dict[str, Any]) -> dict[str, Any]:
    prompt_id = payload.get("prompt_id")
    messages = _messages(payload)
    user_text = next((message.get("content") for message in reversed(messages) if message.get("role") == "user"), None)
    try:
        records = _demo_records()
    except client.ConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    for record in records:
        if record["prompt"] == user_text and (not prompt_id or record["id"] == prompt_id):
            return record
    raise HTTPException(status_code=404, detail="This prompt has no complete disk-cache replay; demo mode made no live call.")


def _demo_call(
    tier: str,
    entry: dict[str, Any],
    saved_call: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stats = entry["stats"]
    # Some saved tier answers retain the router confidence envelope; unwrap
    # that exact shape so it is not shown as if it were the model's answer.
    answer = (saved_call or {}).get("answer", entry["answer"])
    confidence = (saved_call or {}).get("confidence")
    if saved_call is None:
        try:
            envelope = json.loads(answer)
        except (TypeError, json.JSONDecodeError):
            envelope = None
        if (
            isinstance(envelope, dict)
            and set(envelope) == {"answer", "confidence"}
            and isinstance(envelope.get("answer"), str)
        ):
            answer = envelope["answer"]
            value = envelope.get("confidence")
            if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1:
                confidence = float(value)
    status = (saved_call or {}).get("status", "ok")
    return {
        "tier": tier,
        "status": status,
        "sample": 0,
        "answer": answer,
        "confidence": confidence,
        "prompt_tokens": (saved_call or {}).get("prompt_tokens", stats.get("prompt_tokens")),
        "completion_tokens": (saved_call or {}).get("completion_tokens", stats.get("completion_tokens")),
        # Only new active request measurements may be presented as latency.
        "latency_ms": (saved_call or {}).get("request_time_ms", (saved_call or {}).get("latency_ms"))
        if saved_call is not None else stats.get("request_time_ms"),
        "legacy_latency_ms": (saved_call or {}).get("legacy_latency_ms"),
        "retry_count": (saved_call or {}).get("retry_count", stats.get("retry_count")),
        "retry_sleep_ms": (saved_call or {}).get("retry_sleep_ms", stats.get("retry_sleep_ms")),
        "cost_units": (saved_call or {}).get("cost_units", stats.get("cost_units")),
        "cached": True,
        "source": "saved tier-answer cache",
        "error": (saved_call or {}).get("error"),
    }


def _demo_result(record: dict[str, Any]) -> dict[str, Any]:
    decision = record["decision"]
    saved_trace = decision.get("trace") or {}
    reason = saved_trace.get("reason") or decision.get("confidence")
    escalated = bool(saved_trace.get("escalated", decision.get("escalated")))
    strong_failed = isinstance(reason, str) and "strong_call_error" in reason
    saved_calls = saved_trace.get("calls") or []
    cheap_call = next((call for call in saved_calls if call.get("tier") == "cheap"), None)
    calls = [_demo_call("cheap", record["cheap_cache"], cheap_call)]
    if escalated:
        saved_strong_call = next((call for call in saved_calls if call.get("tier") == "strong"), None)
        strong_call = _demo_call("strong", record["strong_cache"], saved_strong_call)
        if strong_failed:
            strong_call.update({"status": "error", "answer": "", "error": "Saved holdout record says the strong call failed."})
        calls.append(strong_call)
    return {
        "id": record["id"],
        "band": record["band"],
        "answer": decision.get("answer") or "",
        "route_trace": {
            "tier_used": saved_trace.get("tier_used") or ("strong" if escalated and not strong_failed else "cheap"),
            "reason": reason,
            "calls": calls,
            "decision": "escalate" if escalated else "stay",
            "escalated": escalated,
            "total_prompt_tokens": saved_trace.get("total_prompt_tokens"),
            "total_completion_tokens": saved_trace.get("total_completion_tokens"),
            "total_tokens": saved_trace.get("total_tokens", decision.get("tokens")),
            "total_latency_ms": saved_trace.get("total_latency_ms"),
            "decision_latency_ms": saved_trace.get("decision_latency_ms"),
            "total_cost_units": saved_trace.get("total_cost_units", decision.get("cost_units")),
            "confidence": calls[0].get("confidence"),
            "retry_count": saved_trace.get("retry_count"),
            "retry_sleep_ms": saved_trace.get("retry_sleep_ms"),
            "cached": True,
            "cache_note": "Tier calls and the holdout decision are replayed from disk cache.",
        },
    }


def _validate_configured_models() -> None:
    """Recheck the Muse-only policy whenever an API route is invoked."""
    client._validate_policy(client._TIERS, client._POLICY)
    for tier_name, config in client._TIERS.items():
        model_id = config.get("model_id") if isinstance(config, dict) else None
        if model_id and "muse" not in str(model_id).casefold():
            raise client.ConfigurationError(
                f"Tier '{tier_name}' model_id {model_id!r} must contain 'muse'"
            )


def _policy() -> dict[str, Any]:
    _validate_configured_models()
    saved: dict[str, Any] = {}
    try:
        result = json.loads(_OPERATING_POINT_PATH.read_text(encoding="utf-8"))
        saved = result.get("operating_point") or {}
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    try:
        threshold = float(os.getenv("MUSEROUTE_THRESHOLD", str(saved.get("threshold", 0.5))))
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="MUSEROUTE_THRESHOLD must be a number from 0 to 1") from exc
    if not 0 <= threshold <= 1:
        raise HTTPException(status_code=500, detail="MUSEROUTE_THRESHOLD must be a number from 0 to 1")
    sc_default = str(bool(saved.get("self_consistency", False))).lower()
    self_consistency = os.getenv("MUSEROUTE_SELF_CONSISTENCY", sc_default).casefold() in {"1", "true", "yes", "on"}
    return {"threshold": threshold, "self_consistency": self_consistency}


def _messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=422, detail="messages must be a non-empty array")
    if any(not isinstance(m, dict) or not isinstance(m.get("role"), str) or "content" not in m for m in messages):
        raise HTTPException(status_code=422, detail="each message must contain role and content")
    return messages


def _run_route(messages: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    try:
        return router.route(messages, _policy())
    except HTTPException:
        raise
    except client.ConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"MuseRoute request failed: {exc}") from exc


def _route_trace(trace: dict[str, Any]) -> dict[str, Any]:
    return {
        "tier_used": trace.get("tier_used"),
        "reason": trace.get("reason"),
        "latency_ms": trace.get("total_latency_ms"),
        "cost_units": trace.get("total_cost_units"),
    }


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/api/demo-prompts")
def api_demo_prompts() -> dict[str, Any]:
    try:
        records = _demo_records()
    except client.ConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "mode": "cached",
        "prompts": [{"id": row["id"], "band": row["band"], "prompt": row["prompt"]} for row in records],
    }


@app.get("/health")
def health() -> dict[str, str]:
    try:
        _validate_configured_models()
    except client.ConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"status": "ok"}


@app.get("/api/facts")
def api_facts() -> dict[str, Any]:
    try:
        _validate_configured_models()
    except client.ConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        return json.loads(_FACTS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Facts are not generated; run python scripts/make_facts.py") from exc
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="results/facts.json is not valid JSON") from exc


@app.post("/api/calculate")
def api_calculate(payload: dict[str, Any]) -> dict[str, Any]:
    facts = api_facts()
    holdout = facts.get("holdout", {}).get("overall", {})
    route_facts = holdout.get("museroute", {})
    strong_facts = holdout.get("strong_only", {})
    return calculator.estimate_monthly_costs(
        monthly_requests=payload.get("monthly_requests"),
        average_prompt_tokens=payload.get("average_prompt_tokens"),
        average_completion_tokens=payload.get("average_completion_tokens"),
        cheap_input_price_per_million=payload.get("cheap_input_price_per_million"),
        cheap_output_price_per_million=payload.get("cheap_output_price_per_million"),
        strong_input_price_per_million=payload.get("strong_input_price_per_million"),
        strong_output_price_per_million=payload.get("strong_output_price_per_million"),
        escalation_rate=route_facts.get("escalation_rate"),
        museroute_accuracy=route_facts.get("accuracy"),
        strong_accuracy=strong_facts.get("accuracy"),
    )


@app.post("/v1/chat/completions")
def chat_completions(payload: dict[str, Any]) -> dict[str, Any]:
    messages = _messages(payload)
    answer, trace = _run_route(messages)
    if not answer:
        raise HTTPException(status_code=502, detail={"message": "No tier returned an answer", "route_trace": trace})
    prompt_tokens = trace.get("total_prompt_tokens")
    completion_tokens = trace.get("total_completion_tokens")
    total_tokens = (
        prompt_tokens + completion_tokens
        if isinstance(prompt_tokens, int) and isinstance(completion_tokens, int)
        else None
    )
    return {
        "id": f"museroute-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": payload.get("model") or "museroute",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
        "route_trace": _route_trace(trace),
    }


@app.post("/api/route")
def api_route(payload: dict[str, Any], demo: bool = False) -> dict[str, Any]:
    if demo:
        return _demo_result(_find_demo_record(payload))
    messages = _messages(payload)
    answer, trace = _run_route(messages)
    return {"answer": answer, "route_trace": trace}


@app.post("/api/recommend")
def api_recommend(payload: dict[str, Any], demo: bool = False) -> dict[str, Any]:
    """Recommend a tier from prompt text only; never generate or return an answer."""
    messages = _messages(payload)
    try:
        _validate_configured_models()
    except client.ConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    user_text = "\n".join(
        str(message.get("content", ""))
        for message in messages
        if message.get("role") == "user"
    ).casefold()
    strong_cues = (
        ("multi-step reasoning", ("step by step", "step-by-step", "multi-step", "multi step", "several steps", "multiple steps")),
        ("proof or deduction", ("prove that", "proof", "derive", "deduce", "logic puzzle", "formal reasoning")),
        ("comparison or trade-offs", ("compare and contrast", "compare both", "evaluate trade-offs", "trade-offs", "tradeoffs", "pros and cons")),
        ("sequential reasoning", ("after that", "before that", "in sequence")),
        ("multiple constraints", ("at least", "at most", "exactly one", "only if", "unless", "constraint", "constraints")),
    )
    matched_cue = next(
        ((label, cue) for label, cues in strong_cues for cue in cues if cue in user_text),
        None,
    )
    if matched_cue is None and "first" in user_text and "then" in user_text:
        matched_cue = ("sequential reasoning", "first…then")
    tier = "strong" if matched_cue else "cheap"
    prompt_class = "complex" if matched_cue else "direct"
    reason = (
        f"Strong recommended: detected {matched_cue[0]} cue ({matched_cue[1]!r})."
        if matched_cue
        else "Cheap recommended: no recognized multi-step, proof, comparison, sequence, or constraint cue."
    )
    config = client._TIERS.get(tier)
    if not isinstance(config, dict):
        config = {}
    if not config.get("model_id"):
        return {
            "selected_tier": None,
            "model_id": None,
            "extra_params": None,
            "prompt_class": prompt_class,
            "reason": f"{tier.title()} tier has no configured model_id.",
            "selection_method": "text_cue_heuristic",
        }
    return {
        "selected_tier": tier,
        "model_id": config.get("model_id"),
        "extra_params": config.get("extra_params"),
        "prompt_class": prompt_class,
        "reason": reason,
        "selection_method": "text_cue_heuristic",
    }


@app.post("/api/compare")
def api_compare(payload: dict[str, Any], demo: bool = False) -> dict[str, Any]:
    if demo:
        record = _find_demo_record(payload)
        result = _demo_result(record)
        saved_calls = (record["decision"].get("trace") or {}).get("calls") or []
        saved_strong_call = next((call for call in saved_calls if call.get("tier") == "strong"), None)
        strong = _demo_call("strong", record["strong_cache"], saved_strong_call)
        strong_total = (
            strong["prompt_tokens"] + strong["completion_tokens"]
            if isinstance(strong.get("prompt_tokens"), int) and isinstance(strong.get("completion_tokens"), int)
            else None
        )
        after_trace = result["route_trace"]
        return {
            "strong_only": {
                "answer": strong["answer"],
                "stats": {
                    "prompt_tokens": strong.get("prompt_tokens"),
                    "completion_tokens": strong.get("completion_tokens"),
                    "total_tokens": strong_total,
                    "latency_ms": strong.get("latency_ms"),
                    "cost_units": strong.get("cost_units"),
                    "cached": True,
                },
            },
            "museroute": {"answer": result["answer"], "route_trace": after_trace},
        }
    messages = _messages(payload)
    _policy()
    try:
        strong_answer, strong_stats = client.chat("strong", messages)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Strong-only request failed: {exc}") from exc
    route_answer, trace = _run_route(messages)
    return {
        "strong_only": {"answer": strong_answer, "stats": strong_stats},
        "museroute": {"answer": route_answer, "route_trace": trace},
    }
