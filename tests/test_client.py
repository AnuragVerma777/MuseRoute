from unittest.mock import Mock

import pytest

from museroute import client


def _configure(monkeypatch, **changes):
    config = {
        "name": "cheap",
        "base_url": "https://local.example/v1",
        "model_id": "org/muse-spark-open-v1",
        "api_key_env": "TEST_MUSE_KEY",
        "cost_weight": 0.25,
        "extra_params": {},
    }
    config.update(changes)
    monkeypatch.setitem(client._TIERS, "cheap", config)
    monkeypatch.setenv("TEST_MUSE_KEY", "test-secret")


def test_startup_policy_rejects_model_outside_allowlist():
    with pytest.raises(client.ConfigurationError, match="other-model.*allowed_models"):
        client._validate_policy(
            {"cheap": {"model_id": "other-model", "base_url": "https://local.example"}},
            {"allowed_models": ["muse-spark-open", "muse-glimmer"], "blocked_hosts": []},
        )


def test_startup_policy_rejects_blocked_host():
    with pytest.raises(client.ConfigurationError, match="https://api.meta.ai/v1.*blocked host"):
        client._validate_policy(
            {"strong": {"model_id": "muse-glimmer-test", "base_url": "https://api.meta.ai/v1"}},
            {"allowed_models": ["muse-spark-open", "muse-glimmer"], "blocked_hosts": ["api.meta.ai"]},
        )


def test_model_allowlist_is_case_insensitive_muse_substring():
    client._validate_policy(
        {"strong": {"model_id": "Vendor/MuSe-Model", "base_url": "https://demo.vendor.example/v1"}},
        {
            "allowed_models": ["muse"],
            "blocked_hosts": ["api.meta.ai"],
        },
    )


def test_policy_allows_any_nonblocked_host():
    client._validate_policy(
        {"strong": {"model_id": "vendor/muse-model", "base_url": "https://api.vendor.example/v1"}},
        {"allowed_models": ["muse"], "blocked_hosts": ["api.meta.ai"]},
    )


def test_402_is_fatal_and_not_retried(monkeypatch, capsys):
    _configure(monkeypatch)
    response = Mock(ok=False, status_code=402)
    response.json.return_value = {"error": {"message": "Payment required"}}
    post = Mock(return_value=response)
    monkeypatch.setattr(client.requests, "post", post)

    with pytest.raises(client.FatalPaymentError, match="stop and switch host"):
        client.chat("cheap", [{"role": "user", "content": "hello"}])

    assert post.call_count == 1
    assert "this host requires payment: stop and switch host" in capsys.readouterr().out


def test_retries_429_then_returns_stats(monkeypatch):
    _configure(monkeypatch)
    rate_limited = Mock(ok=False, status_code=429)
    success = Mock(ok=True, status_code=200)
    success.json.return_value = {
        "choices": [{"message": {"content": "hello"}}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2},
    }
    post = Mock(side_effect=[rate_limited, success])
    monkeypatch.setattr(client.requests, "post", post)
    sleep = Mock()
    monkeypatch.setattr(client.time, "sleep", sleep)

    text, stats = client.chat("cheap", [{"role": "user", "content": "hello"}])

    assert text == "hello"
    assert post.call_count == 2
    sleep.assert_called_once_with(1)
    assert stats["prompt_tokens"] == 4
    assert stats["completion_tokens"] == 2
    assert stats["cost_units"] == 1.5
    assert stats["retry_count"] == 1
    assert stats["retry_sleep_ms"] == 1000
    assert stats["attempt_count"] == 2
    assert isinstance(stats["request_time_ms"], (int, float))
    assert stats["latency_ms"] == stats["request_time_ms"]
    assert stats["model_time_ms"] is None  # host does not expose isolated compute time
    assert post.call_args.kwargs["timeout"] == 120
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer test-secret"


def test_request_latency_excludes_backoff_sleep(monkeypatch):
    _configure(monkeypatch)
    limited = Mock(ok=False, status_code=429)
    success = Mock(ok=True, status_code=200)
    success.json.return_value = {
        "choices": [{"message": {"content": "hello"}}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2},
    }
    monkeypatch.setattr(client.requests, "post", Mock(side_effect=[limited, success]))
    monkeypatch.setattr(client.time, "sleep", Mock())
    clock = Mock(side_effect=[10.0, 10.01, 50.0, 50.04])
    monkeypatch.setattr(client.time, "perf_counter", clock)

    _text, stats = client.chat("cheap", [{"role": "user", "content": "hello"}])

    assert stats["request_time_ms"] == 50.0
    assert stats["retry_sleep_ms"] == 1000
    assert stats["latency_ms"] == 50.0
