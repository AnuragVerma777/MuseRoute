"""FastAPI service exposing MuseRoute through chat-completions-style endpoints."""

from __future__ import annotations

import os
import json
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from . import client, router

app = FastAPI(title="MuseRoute", version="0.1.0")
_OPERATING_POINT_PATH = Path(__file__).resolve().parents[1] / "results" / "sweep.json"


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


@app.get("/health")
def health() -> dict[str, str]:
    try:
        _validate_configured_models()
    except client.ConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"status": "ok"}


@app.post("/v1/chat/completions")
def chat_completions(payload: dict[str, Any]) -> dict[str, Any]:
    messages = _messages(payload)
    answer, trace = _run_route(messages)
    if not answer:
        raise HTTPException(status_code=502, detail={"message": "No tier returned an answer", "route_trace": trace})
    prompt_tokens = trace.get("total_prompt_tokens", 0)
    completion_tokens = trace.get("total_completion_tokens", 0)
    return {
        "id": f"museroute-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": payload.get("model") or "museroute",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        "route_trace": _route_trace(trace),
    }


@app.post("/api/route")
def api_route(payload: dict[str, Any]) -> dict[str, Any]:
    messages = _messages(payload)
    answer, trace = _run_route(messages)
    return {"answer": answer, "route_trace": trace}


@app.post("/api/compare")
def api_compare(payload: dict[str, Any]) -> dict[str, Any]:
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
