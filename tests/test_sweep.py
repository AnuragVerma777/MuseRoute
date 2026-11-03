import json

from museroute import router
from scripts import sweep


def _prompt(prompt_id="T001", check="exact"):
    return {"id": prompt_id, "prompt": "say hello", "gold": "hello", "check": check, "band": "easy"}


def _entry(answer):
    return {"answer": answer, "stats": {"prompt_tokens": 3, "completion_tokens": 2, "cost_units": None}}


def _cache_for(prompt, confidence=0.9, second_answer="hello", second_confidence=0.9):
    messages = [{"role": "user", "content": prompt["prompt"]}]
    prepared = router._with_confidence_request(messages, "text")
    params = {"temperature": sweep.ROUTER_TEMP}
    cache = {
        sweep._ckey("cheap", prompt["id"], sweep.ROUTER_TEMP, 0): _entry("hello"),
        sweep._ckey("strong", prompt["id"], sweep.ROUTER_TEMP, 0): _entry("hello"),
        router._cache_key("cheap", prepared, params, 0): _entry(
            json.dumps({"answer": "hello", "confidence": confidence})
        ),
    }
    if second_answer is not None:
        cache[router._cache_key("cheap", prepared, params, 1)] = _entry(
            json.dumps({"answer": second_answer, "confidence": second_confidence})
        )
    cache[router._cache_key("strong", prepared, params, 0)] = _entry(
        json.dumps({"answer": "hello", "confidence": 0.9})
    )
    return cache, prepared


def test_holdout_preflight_requires_only_selected_calls_without_self_consistency():
    prompt = _prompt()
    cache, prepared = _cache_for(prompt, confidence=0.9)
    required = sweep._required_holdout_cache_keys(cache, [prompt], 0.7, False)

    assert router._cache_key("cheap", prepared, {"temperature": sweep.ROUTER_TEMP}, 0) in required
    assert router._cache_key("strong", prepared, {"temperature": sweep.ROUTER_TEMP}, 0) not in required
    assert sweep._cache_misses(cache, required) == []


def test_holdout_preflight_includes_strong_only_when_confidence_requires_escalation():
    prompt = _prompt()
    cache, prepared = _cache_for(prompt, confidence=0.2)
    required = sweep._required_holdout_cache_keys(cache, [prompt], 0.7, False)

    assert router._cache_key("strong", prepared, {"temperature": sweep.ROUTER_TEMP}, 0) in required


def test_holdout_preflight_requires_two_cheap_samples_for_self_consistency():
    prompt = _prompt()
    cache, prepared = _cache_for(prompt, confidence=0.9, second_answer="hello")
    required = sweep._required_holdout_cache_keys(cache, [prompt], 0.7, True)

    assert router._cache_key("cheap", prepared, {"temperature": sweep.ROUTER_TEMP}, 1) in required
    assert router._cache_key("strong", prepared, {"temperature": sweep.ROUTER_TEMP}, 0) not in required
