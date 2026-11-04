from scripts.make_facts import build_facts


def test_build_facts_keeps_unmeasured_values_null_and_uses_recorded_tokens():
    workload = [
        {"id": "E1", "band": "easy", "check": "exact", "gold": "safe"},
        {"id": "M1", "band": "medium", "check": "json_fields", "gold": {"amount": 10}},
    ]
    baseline_rows = [
        {
            "id": "E1", "tier": tier, "band": "easy", "correct": True,
            "tokens": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            "latency_ms": 10, "error": None,
        }
        for tier in ("cheap", "strong")
    ]
    sweep = {
        "tune_baselines": {"strong": {"accuracy": 0.5}},
        "holdout_baselines": {
            "cheap": {"accuracy": None, "finished": 0, "errors": 2},
            "strong": {"accuracy": None, "finished": 0, "errors": 2},
        },
        "operating_point": {
            "threshold": 0.5,
            "self_consistency": False,
            "tune": {"accuracy": 0.5},
            "holdout": {"accuracy": None, "details": []},
        },
    }
    tiers = {
        "cheap": {"model_id": "meta/muse-spark-open", "base_url": "https://cheap.example/v1"},
        "strong": {"model_id": "meta/muse-glimmer-30b", "base_url": "https://strong.example/v1"},
    }

    facts = build_facts(baseline_rows, sweep, workload, workload[:1], workload[1:], tiers)

    assert facts["prompt_counts"]["baseline"]["cheap"] == {"expected": 2, "finished": 1, "errors": 0}
    assert facts["baseline"]["averages"]["cheap"]["average_total_tokens"] is None
    assert facts["baseline"]["averages"]["cheap"]["average_prompt_tokens"] is None
    assert facts["baseline"]["averages"]["cheap"]["average_latency_ms"] is None
    assert facts["baseline"]["averages"]["cheap"]["model_time_ms"] is None
    assert facts["holdout"]["overall"]["museroute"]["accuracy"] is None
    assert facts["holdout"]["overall"]["museroute"]["cost_units"] is None
    assert facts["models_and_hosts"]["cheap"] == {
        "model_id": "meta/muse-spark-open", "host": "cheap.example"
    }


def test_build_facts_includes_only_answered_mistakes_and_limits_to_five():
    workload = [
        {"id": f"H{i}", "band": "hard", "check": "numeric", "gold": i}
        for i in range(7)
    ]
    details = [
        {"id": prompt["id"], "band": "hard", "correct": False, "answer": "wrong", "confidence": "cheap_answer_accepted"}
        for prompt in workload
    ]
    details.append({"id": "H8", "band": "hard", "correct": False, "answer": "", "confidence": "cheap_call_error: timeout"})
    sweep = {"operating_point": {"holdout": {"details": details}}}
    facts = build_facts([], sweep, workload, [], workload, {"cheap": {}, "strong": {}})

    mistakes = facts["worst_holdout_mistakes"]
    assert len(mistakes) == 5
    assert all(mistake["answer"] == "wrong" for mistake in mistakes)
    assert facts["holdout"]["overall"]["museroute"]["errors"] == 1
    assert facts["holdout"]["overall"]["museroute"]["tier_call_errors"] == 1
    assert facts["holdout_request_failures"] == [
        {"id": "H8", "band": "hard", "reason": "cheap_call_error: timeout"}
    ]


def test_build_facts_keeps_successful_call_usage_when_another_call_failed():
    workload = [{"id": "E1", "band": "easy", "check": "exact", "gold": "ok"}]
    details = [{
        "id": "E1", "band": "easy", "correct": False, "answer": "",
        "confidence": "cheap_call_error: timeout",
        "trace": {"calls": [
            {"tier": "cheap", "status": "error", "prompt_tokens": None, "completion_tokens": None},
            {"tier": "strong", "status": "ok", "prompt_tokens": 8, "completion_tokens": 5},
        ]},
    }]
    facts = build_facts([], {"operating_point": {"holdout": {"details": details}}}, workload, [], workload, {})

    route_facts = facts["router"]
    assert route_facts["known_successful_call_tokens"] == 13
    assert route_facts["known_successful_call_prompt_tokens"] == 8
    assert route_facts["known_successful_call_completion_tokens"] == 5
    assert route_facts["prompts_with_complete_call_usage"] == 0
    assert route_facts["calls_missing_usage"] == 1
