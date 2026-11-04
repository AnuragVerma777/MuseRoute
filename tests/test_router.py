import json
from unittest.mock import Mock

import pytest

from museroute import client, router


def _stats(tokens=10, latency=12.5, cost=0.25):
    return {
        "prompt_tokens": tokens - 3,
        "completion_tokens": 3,
        "latency_ms": latency,
        "request_time_ms": latency,
        "model_time_ms": None,
        "retry_count": 0,
        "retry_sleep_ms": 0,
        "attempt_count": 1,
        "cost_units": cost,
    }


def _envelope(answer, confidence=0.9):
    return json.dumps({"answer": answer, "confidence": confidence})


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(router, "_CACHE_PATH", tmp_path / "router_cache.json")
    monkeypatch.setitem(client._TIERS, "cheap", {"base_url": "https://cheap.example/v1"})
    monkeypatch.setitem(client._TIERS, "strong", {"base_url": "https://strong.example/v1"})


def test_accepts_cheap_when_confident_and_format_valid(monkeypatch):
    call = Mock(return_value=(_envelope("hello"), _stats()))
    monkeypatch.setattr(router.client, "chat", call)

    answer, trace = router.route([{"role": "user", "content": "Say hello"}], {"threshold": 0.7})

    assert answer == "hello"
    assert trace["tier_used"] == "cheap"
    assert trace["reason"] == "cheap_answer_accepted"
    assert trace["escalated"] is False
    assert trace["calls"][0]["prompt_tokens"] == 7
    assert trace["calls"][0]["completion_tokens"] == 3
    assert trace["calls"][0]["request_time_ms"] == 12.5
    assert trace["retry_count"] == 0
    assert trace["calls"][0]["answer"] == "hello"
    assert trace["calls"][0]["confidence"] == 0.9
    assert trace["calls"][0]["cached"] is False
    assert trace["total_cost_units"] == 0.25
    call.assert_called_once()


def test_invalid_answer_json_format_escalates(monkeypatch):
    call = Mock(side_effect=[(_envelope("not json"), _stats()), (_envelope('{"ok":true}'), _stats())])
    monkeypatch.setattr(router.client, "chat", call)

    answer, trace = router.route(
        [{"role": "user", "content": "Return JSON"}],
        {"threshold": 0.5, "format_check": "json"},
    )

    assert answer == '{"ok":true}'
    assert trace["tier_used"] == "strong"
    assert "format_check_failed" in trace["reason"]
    assert trace["escalated"] is True
    assert [c["tier"] for c in trace["calls"]] == ["cheap", "strong"]


def test_empty_answer_escalates(monkeypatch):
    call = Mock(side_effect=[(_envelope(""), _stats()), (_envelope("recovered"), _stats())])
    monkeypatch.setattr(router.client, "chat", call)

    answer, trace = router.route([{"role": "user", "content": "Answer"}], {})

    assert answer == "recovered"
    assert "answer field is empty" in trace["reason"]
    assert trace["tier_used"] == "strong"


def test_refusal_escalates(monkeypatch):
    call = Mock(side_effect=[(_envelope("I cannot help with that."), _stats()), (_envelope("safe answer"), _stats())])
    monkeypatch.setattr(router.client, "chat", call)

    answer, trace = router.route([{"role": "user", "content": "Answer"}], {})

    assert answer == "safe answer"
    assert "refusal" in trace["reason"]


def test_low_confidence_escalates(monkeypatch):
    call = Mock(side_effect=[(_envelope("guess", 0.2), _stats()), (_envelope("certain", 0.95), _stats())])
    monkeypatch.setattr(router.client, "chat", call)

    answer, trace = router.route([{"role": "user", "content": "Answer"}], {"threshold": 0.6})

    assert answer == "certain"
    assert trace["tier_used"] == "strong"
    assert trace["reason"] == "cheap_confidence_below_threshold"
    assert trace["total_cost_units"] == 0.5
    assert trace["total_prompt_tokens"] == 14
    assert trace["total_completion_tokens"] == 6


def test_missing_confidence_is_zero_and_escalates(monkeypatch):
    missing = json.dumps({"answer": "maybe"})
    call = Mock(side_effect=[(missing, _stats()), (_envelope("resolved"), _stats())])
    monkeypatch.setattr(router.client, "chat", call)

    answer, trace = router.route([{"role": "user", "content": "Answer"}], {"threshold": 0.01})

    assert answer == "resolved"
    assert trace["reason"] == "cheap_confidence_below_threshold"


def test_self_consistency_disagreement_escalates(monkeypatch):
    call = Mock(side_effect=[
        (_envelope("first", 0.9), _stats()),
        (_envelope("second", 0.9), _stats()),
        (_envelope("agreed answer", 0.9), _stats()),
    ])
    monkeypatch.setattr(router.client, "chat", call)

    answer, trace = router.route(
        [{"role": "user", "content": "Answer"}],
        {"threshold": 0.5, "self_consistency": True, "low_temperature": 0.1},
    )

    assert answer == "agreed answer"
    assert trace["reason"] == "cheap_self_consistency_disagreement"
    assert [c["tier"] for c in trace["calls"]] == ["cheap", "cheap", "strong"]
    assert call.call_args_list[0].kwargs["temperature"] == 0.1


def test_cheap_error_falls_back_to_strong(monkeypatch):
    call = Mock(side_effect=[RuntimeError("cheap unavailable"), (_envelope("strong answer"), _stats())])
    monkeypatch.setattr(router.client, "chat", call)

    answer, trace = router.route([{"role": "user", "content": "Answer"}], {})

    assert answer == "strong answer"
    assert trace["tier_used"] == "strong"
    assert trace["escalated"] is True
    assert trace["calls"][0]["status"] == "error"
    assert trace["calls"][0]["prompt_tokens"] is None
    assert trace["calls"][0]["completion_tokens"] is None
    assert trace["calls"][0]["request_time_ms"] is None
    assert trace["calls"][1]["prompt_tokens"] == 7
    assert trace["successful_call_prompt_tokens"] == 7
    assert trace["total_prompt_tokens"] is None
    assert trace["total_latency_ms"] is None

    # Failures are not cached, but the successful strong answer is.
    repeated_answer, repeated_trace = router.route([{"role": "user", "content": "Answer"}], {})
    assert repeated_answer == answer
    assert repeated_trace["calls"][0]["status"] == "error"
    assert repeated_trace["calls"][0]["cached"] is False
    assert repeated_trace["calls"][1]["cached"] is True
    assert call.call_count == 3


def test_stale_cached_failure_is_removed_and_retried(monkeypatch):
    messages = [{"role": "user", "content": "retry me"}]
    cache = {}
    prepared = router._with_confidence_request(messages, "text")
    key = router._cache_key("cheap", prepared, {"temperature": 0.2}, 0)
    cache[key] = {"error_type": "MuseRouteError", "error": "old connection failure", "metrics": {}}
    router._CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
    call = Mock(return_value=(_envelope("recovered"), _stats()))
    monkeypatch.setattr(router.client, "chat", call)

    answer, trace = router.route(messages, {})

    assert answer == "recovered"
    assert trace["calls"][0]["cached"] is False
    saved_cache = json.loads(router._CACHE_PATH.read_text(encoding="utf-8"))
    assert saved_cache[key]["answer"]
    call.assert_called_once()


def test_router_records_retry_metadata_per_call(monkeypatch):
    stats = _stats()
    stats.update({"request_time_ms": 8.25, "retry_count": 2, "retry_sleep_ms": 3000, "attempt_count": 3})
    monkeypatch.setattr(router.client, "chat", Mock(return_value=(_envelope("answer"), stats)))

    _answer, trace = router.route([{"role": "user", "content": "Answer"}], {})

    call = trace["calls"][0]
    assert call["prompt_tokens"] == 7
    assert call["completion_tokens"] == 3
    assert call["retry_count"] == 2
    assert call["retry_sleep_ms"] == 3000
    assert trace["retry_count"] == 2
    assert trace["retry_sleep_ms"] == 3000


def test_legacy_cached_latency_is_not_mislabeled_as_active_request_time(monkeypatch):
    legacy_stats = {"prompt_tokens": 7, "completion_tokens": 3, "latency_ms": 9999, "cost_units": None}
    monkeypatch.setattr(router.client, "chat", Mock(return_value=(_envelope("answer"), legacy_stats)))

    _answer, trace = router.route([{"role": "user", "content": "Answer"}], {})

    assert trace["calls"][0]["request_time_ms"] is None
    assert trace["calls"][0]["latency_ms"] is None
    assert trace["calls"][0]["legacy_latency_ms"] == 9999
    assert trace["total_latency_ms"] is None

def test_strong_error_falls_back_to_cheap_answer(monkeypatch):
    call = Mock(side_effect=[(_envelope("cheap answer", 0.1), _stats()), RuntimeError("strong unavailable")])
    monkeypatch.setattr(router.client, "chat", call)

    answer, trace = router.route([{"role": "user", "content": "Answer"}], {"threshold": 0.8})

    assert answer == "cheap answer"
    assert trace["tier_used"] == "cheap"
    assert "fallback_to_cheap" in trace["reason"]
    assert trace["calls"][1]["status"] == "error"


def test_shared_host_payment_error_does_not_retry_other_tier(monkeypatch):
    monkeypatch.setitem(client._TIERS, "strong", {"base_url": "https://cheap.example/v1"})
    call = Mock(side_effect=client.FatalPaymentError("this host requires payment"))
    monkeypatch.setattr(router.client, "chat", call)

    answer, trace = router.route([{"role": "user", "content": "Answer"}], {})

    assert answer == ""
    assert trace["tier_used"] is None
    assert trace["escalated"] is False
    assert call.call_count == 1


def test_repeated_route_uses_disk_cache(monkeypatch):
    call = Mock(return_value=(_envelope("cached"), _stats()))
    monkeypatch.setattr(router.client, "chat", call)
    messages = [{"role": "user", "content": "Answer"}]

    first = router.route(messages, {})
    second = router.route(messages, {})

    assert first[0] == second[0]
    assert first[1]["reason"] == second[1]["reason"]
    assert first[1]["calls"][0]["cached"] is False
    assert second[1]["calls"][0]["cached"] is True
    call.assert_called_once()
