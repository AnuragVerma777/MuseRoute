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


def test_chat_completions_preserves_unmeasured_usage_as_null(monkeypatch):
    trace = _trace()
    trace["total_prompt_tokens"] = None
    trace["total_completion_tokens"] = None
    monkeypatch.setattr(server.router, "route", lambda messages, policy: ("hello", trace))

    body = server.chat_completions({"messages": [{"role": "user", "content": "Hi"}]})

    assert body["usage"] == {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}


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


def test_api_facts_reads_generated_facts(monkeypatch, tmp_path):
    facts_path = tmp_path / "facts.json"
    facts_path.write_text('{"prompt_counts":{"holdout":45}}', encoding="utf-8")
    monkeypatch.setattr(server, "_FACTS_PATH", facts_path)

    assert server.api_facts() == {"prompt_counts": {"holdout": 45}}


def test_api_facts_returns_clear_missing_artifact_error(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_FACTS_PATH", tmp_path / "missing.json")

    with pytest.raises(HTTPException) as exc:
        server.api_facts()

    assert exc.value.status_code == 404
    assert "scripts/make_facts.py" in exc.value.detail


def test_api_facts_rejects_non_muse_model():
    client._TIERS["strong"]["model_id"] = "unrelated-model"

    with pytest.raises(HTTPException) as exc:
        server.api_facts()

    assert exc.value.status_code == 503
    assert "unrelated-model" in exc.value.detail


def _write_demo_sources(monkeypatch, tmp_path):
    import json

    workload = []
    answer_cache = {}
    details = []
    for band, prefix in (("easy", "E"), ("medium", "M"), ("hard", "H")):
        for index in (1, 2):
            prompt_id = f"{prefix}{index}"
            prompt = f"Cached {band} prompt {index}?"
            workload.append({"id": prompt_id, "band": band, "prompt": prompt})
            for tier in ("cheap", "strong"):
                answer_cache[f"{tier}|{prompt_id}|t0.2|a0"] = {
                    "answer": f"{tier} answer {prompt_id}",
                    "stats": {
                        "prompt_tokens": 7,
                        "completion_tokens": 3,
                        "latency_ms": 12.5 if tier == "cheap" else 24.5,
                        "cost_units": None,
                    },
                }
            details.append({
                "id": prompt_id,
                "band": band,
                "escalated": index == 2,
                "confidence": "cheap_confidence_below_threshold" if index == 2 else "cheap_answer_accepted",
                "answer": f"routed answer {prompt_id}",
                "tokens": None,
                "latency_ms": 31.0,
                "cost_units": None,
            })
    workload_path = tmp_path / "workload.jsonl"
    workload_path.write_text("".join(json.dumps(row) + "\n" for row in workload), encoding="utf-8")
    answer_cache_path = tmp_path / "answer_cache.json"
    answer_cache_path.write_text(json.dumps(answer_cache), encoding="utf-8")
    sweep_path = tmp_path / "sweep.json"
    sweep_path.write_text(json.dumps({"operating_point": {"holdout": {"details": details}}}), encoding="utf-8")
    monkeypatch.setattr(server, "_WORKLOAD_PATH", workload_path)
    monkeypatch.setattr(server, "_ANSWER_CACHE_PATH", answer_cache_path)
    monkeypatch.setattr(server, "_OPERATING_POINT_PATH", sweep_path)
    return workload


def test_get_root_serves_plain_static_page():
    response = server.home()

    assert response.path.name == "index.html"
    assert "Hackathon teams evaluating Meta Muse" in open(response.path, encoding="utf-8").read()
    assert any(route.path == "/" for route in server.app.routes)


def test_demo_prompts_are_six_cached_examples(monkeypatch, tmp_path):
    _write_demo_sources(monkeypatch, tmp_path)
    monkeypatch.setattr(server.client, "chat", lambda *args, **kwargs: pytest.fail("demo must not call a tier"))

    result = server.api_demo_prompts()

    assert len(result["prompts"]) == 6
    assert [sum(row["band"] == band for row in result["prompts"]) for band in ("easy", "medium", "hard")] == [2, 2, 2]


def test_demo_route_replays_cache_and_returns_timeline(monkeypatch, tmp_path):
    _write_demo_sources(monkeypatch, tmp_path)
    monkeypatch.setattr(server.client, "chat", lambda *args, **kwargs: pytest.fail("demo must not call a tier"))
    payload = {"prompt_id": "E2", "messages": [{"role": "user", "content": "Cached easy prompt 2?"}]}

    result = server.api_route(payload, demo=True)

    assert result["answer"] == "routed answer E2"
    assert result["route_trace"]["decision"] == "escalate"
    assert [call["tier"] for call in result["route_trace"]["calls"]] == ["cheap", "strong"]
    assert all(call["cached"] for call in result["route_trace"]["calls"])
    assert result["route_trace"]["confidence"] is None


def test_demo_compare_uses_saved_answers_and_null_costs(monkeypatch, tmp_path):
    _write_demo_sources(monkeypatch, tmp_path)
    monkeypatch.setattr(server.client, "chat", lambda *args, **kwargs: pytest.fail("demo must not call a tier"))
    payload = {"prompt_id": "M1", "messages": [{"role": "user", "content": "Cached medium prompt 1?"}]}

    result = server.api_compare(payload, demo=True)

    assert result["strong_only"]["answer"] == "strong answer M1"
    assert result["strong_only"]["stats"]["total_tokens"] == 10
    assert result["museroute"]["answer"] == "routed answer M1"
    assert result["strong_only"]["stats"]["cost_units"] is None
    assert result["museroute"]["route_trace"]["total_cost_units"] is None


def test_demo_cache_miss_is_clear_and_never_calls_tier(monkeypatch, tmp_path):
    _write_demo_sources(monkeypatch, tmp_path)
    monkeypatch.setattr(server.client, "chat", lambda *args, **kwargs: pytest.fail("demo must not call a tier"))
    payload = {"prompt_id": "unknown", "messages": [{"role": "user", "content": "uncached"}]}

    with pytest.raises(HTTPException) as exc:
        server.api_route(payload, demo=True)

    assert exc.value.status_code == 404
    assert "demo mode made no live call" in exc.value.detail
