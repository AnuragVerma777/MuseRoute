import pytest
from fastapi import HTTPException

from museroute import client, server


def _trace():
    return {
        "tier_used": "cheap",
        "reason": "cheap_answer_accepted",
        "calls": [{"tier": "cheap", "status": "ok"}],
        "escalated": False,
        "total_prompt_tokens": 8,
        "total_completion_tokens": 2,
        "total_latency_ms": 15.5,
        "total_cost_units": None,
    }


@pytest.fixture(autouse=True)
def configured_tiers(monkeypatch):
    monkeypatch.setitem(client._TIERS, "cheap", {"model_id": "meta/muse-spark-open", "base_url": "https://cheap.example/v1"})
    monkeypatch.setitem(client._TIERS, "strong", {"model_id": "meta/muse-glimmer-30b", "base_url": "https://strong.example/v1"})


def test_chat_completions_returns_standard_shape_and_compact_trace(monkeypatch):
    monkeypatch.setattr(server.router, "route", lambda messages, policy: ("hello", _trace()))
    body = server.chat_completions({"model": "museroute", "messages": [{"role": "user", "content": "Hi"}]})
    assert body["choices"][0]["message"]["content"] == "hello"
    assert body["usage"] == {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}
    assert body["route_trace"] == {
        "tier_used": "cheap", "reason": "cheap_answer_accepted", "latency_ms": 15.5, "cost_units": None
    }


def test_api_route_returns_full_trace(monkeypatch):
    monkeypatch.setattr(server.router, "route", lambda messages, policy: ("answer", _trace()))
    assert server.api_route({"messages": [{"role": "user", "content": "x"}]}) == {
        "answer": "answer", "route_trace": _trace()
    }


def test_compare_runs_strong_only_and_router_on_same_messages(monkeypatch):
    seen = []
    monkeypatch.setattr(server.client, "chat", lambda tier, messages: (seen.append((tier, messages)) or ("strong answer", {"tier": tier})))
    monkeypatch.setattr(server.router, "route", lambda messages, policy: (seen.append(("router", messages)) or ("routed answer", _trace())))
    messages = [{"role": "user", "content": "same prompt"}]
    response = server.api_compare({"messages": messages})

    assert response["strong_only"]["answer"] == "strong answer"
    assert response["museroute"]["answer"] == "routed answer"
    assert seen[0][1] == seen[1][1] == messages


def test_health_rejects_configured_non_muse_model():
    client._TIERS["strong"]["model_id"] = "provider/not-allowed"

    with pytest.raises(HTTPException) as exc:
        server.health()
    assert exc.value.status_code == 503
    assert "provider/not-allowed" in exc.value.detail


def test_request_rechecks_all_configured_models():
    client._TIERS["cheap"]["model_id"] = "some-other-model"

    with pytest.raises(HTTPException) as exc:
        server.api_route({"messages": [{"role": "user", "content": "x"}]})
    assert exc.value.status_code == 503
    assert "some-other-model" in exc.value.detail
