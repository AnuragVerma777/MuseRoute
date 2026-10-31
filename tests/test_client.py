import importlib
import os
from unittest.mock import Mock

import pytest
import requests

from museroute import client


def _configure(monkeypatch, **changes):
    cfg = {
        "name": "cheap",
        "base_url": "https://example.test/v1",
        "model_id": "Muse-Test-1",
        "api_key_env": "TEST_API_KEY",
        "price_in_per_million": None,
        "price_out_per_million": None,
        "extra_params": {},
    }
    cfg.update(changes)
    monkeypatch.setitem(client._TIERS, "cheap", cfg)
    monkeypatch.setenv("TEST_API_KEY", "secret")


def test_startup_model_check_from_config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("tiers:\n  cheap:\n    model_id: other-model\n", encoding="utf-8")
    monkeypatch.setattr(client, "_CONFIG_PATH", path)
    with pytest.raises(client.ConfigurationError, match="contain 'muse'"):
        client._load_config()


def test_chat_posts_and_returns_stats(monkeypatch):
    _configure(monkeypatch)
    response = Mock(ok=True, status_code=200)
    response.json.return_value = {
        "choices": [{"message": {"content": "hello"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }
    post = Mock(return_value=response)
    monkeypatch.setattr(client.requests, "post", post)

    text, stats = client.chat("cheap", [{"role": "user", "content": "hi"}], temperature=0.2)

    assert text == "hello"
    assert stats["prompt_tokens"] == 3
    assert stats["completion_tokens"] == 2
    assert stats["tier"] == "cheap"
    assert stats["cost_usd"] is None
    args, kwargs = post.call_args
    assert args[0] == "https://example.test/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer secret"
    assert kwargs["timeout"] == 60
    assert kwargs["json"]["temperature"] == 0.2


def test_retries_rate_limit_then_succeeds(monkeypatch):
    _configure(monkeypatch)
    rate_limited = Mock(ok=False, status_code=429)
    success = Mock(ok=True, status_code=200)
    success.json.return_value = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
    post = Mock(side_effect=[rate_limited, success])
    monkeypatch.setattr(client.requests, "post", post)
    sleep = Mock()
    monkeypatch.setattr(client.time, "sleep", sleep)

    assert client.chat("cheap", [])[0] == "ok"
    assert post.call_count == 2
    sleep.assert_called_once_with(1)


def test_missing_key_has_clear_error(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.delenv("TEST_API_KEY")
    with pytest.raises(client.AuthenticationError, match="TEST_API_KEY"):
        client.chat("cheap", [])


def test_rejected_parameter_has_clear_error(monkeypatch):
    _configure(monkeypatch)
    response = Mock(ok=False, status_code=400)
    response.json.return_value = {"error": {"message": "unsupported parameter: temperature"}}
    monkeypatch.setattr(client.requests, "post", Mock(return_value=response))
    with pytest.raises(client.RejectedParameterError, match="rejected a parameter"):
        client.chat("cheap", [], temperature=0)

