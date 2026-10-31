"""HTTP client for configured Muse-compatible chat completion models."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import requests
import yaml
from dotenv import load_dotenv


class MuseRouteError(RuntimeError):
    """Base error raised by the museroute client."""


class ConfigurationError(MuseRouteError):
    """The requested tier or its configuration is invalid."""


class AuthenticationError(MuseRouteError):
    """The API key is missing or was rejected."""


class UnknownModelError(MuseRouteError):
    """The configured model is not known to the endpoint."""


class RejectedParameterError(MuseRouteError):
    """The endpoint rejected a request parameter."""


_CONFIG_PATH = Path(__file__).with_name("config.yaml")
load_dotenv()


def _load_config() -> dict[str, Any]:
    try:
        with _CONFIG_PATH.open(encoding="utf-8") as config_file:
            data = yaml.safe_load(config_file) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"Could not load model configuration: {exc}") from exc
    tiers = data.get("tiers")
    if not isinstance(tiers, dict):
        raise ConfigurationError("Configuration must contain a 'tiers' mapping")
    for tier_name, tier_config in tiers.items():
        model_id = tier_config.get("model_id") if isinstance(tier_config, dict) else None
        if model_id and "muse" not in str(model_id).casefold():
            raise ConfigurationError(
                f"Configured model for tier '{tier_name}' must contain 'muse' (case-insensitive); "
                f"got {model_id!r}"
            )
    return tiers


_TIERS = _load_config()


def _server_error(response: requests.Response, tier: str) -> MuseRouteError:
    try:
        detail = response.json()
    except ValueError:
        detail = response.text
    message = str(detail)
    lowered = message.casefold()
    if response.status_code in (401, 403):
        return AuthenticationError(f"Authentication failed for tier '{tier}': {message}")
    if response.status_code == 404 or "model_not_found" in lowered or "unknown model" in lowered:
        return UnknownModelError(f"Unknown model for tier '{tier}': {message}")
    if response.status_code in (400, 422) and any(
        marker in lowered for marker in ("parameter", "unsupported", "invalid_request", "extra_forbidden")
    ):
        return RejectedParameterError(f"Endpoint rejected a parameter for tier '{tier}': {message}")
    return MuseRouteError(f"Chat request failed for tier '{tier}' (HTTP {response.status_code}): {message}")


def chat(tier: str, messages: list[dict[str, Any]], **overrides: Any) -> tuple[str, dict[str, Any]]:
    """Send a chat completion request and return its text and usage/cost stats.

    ``overrides`` are merged over the tier's ``extra_params`` in the JSON body.
    Transient HTTP 429 and 5xx responses are retried up to three times after the
    initial request, with 1, 2, and 4 second exponential delays.
    """
    if tier not in _TIERS:
        raise ConfigurationError(f"Unknown tier '{tier}'. Configured tiers: {', '.join(_TIERS)}")
    config = _TIERS[tier]
    model_id = config.get("model_id")
    if not model_id:
        raise ConfigurationError(f"Tier '{tier}' has no model_id configured")
    if "muse" not in str(model_id).casefold():
        raise ConfigurationError(f"Configured model for tier '{tier}' must contain 'muse'")
    base_url = config.get("base_url")
    if not base_url:
        raise ConfigurationError(f"Tier '{tier}' has no base_url configured")
    key_env = config.get("api_key_env")
    api_key = os.getenv(str(key_env)) if key_env else None
    if not api_key:
        raise AuthenticationError(
            f"Missing API key for tier '{tier}': set environment variable {key_env or '<unset>'}"
        )

    body = {"model": model_id, "messages": messages}
    body.update(config.get("extra_params") or {})
    body.update(overrides)
    url = f"{str(base_url).rstrip('/')}/chat/completions"
    started = time.perf_counter()
    response: requests.Response | None = None
    for attempt in range(4):
        try:
            response = requests.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                timeout=60,
            )
        except requests.RequestException as exc:
            raise MuseRouteError(f"Request failed for tier '{tier}': {exc}") from exc
        if response.status_code == 429 or 500 <= response.status_code <= 599:
            if attempt < 3:
                time.sleep(2**attempt)
                continue
        break

    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    assert response is not None
    if not response.ok:
        raise _server_error(response, tier)
    try:
        payload = response.json()
        text = payload["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise MuseRouteError(f"Invalid chat completion response for tier '{tier}'") from exc
    usage = payload.get("usage") or {}
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    price_in = config.get("price_in_per_million")
    price_out = config.get("price_out_per_million")
    cost_usd = None
    if price_in is not None and price_out is not None and prompt_tokens is not None and completion_tokens is not None:
        cost_usd = (prompt_tokens * price_in + completion_tokens * price_out) / 1_000_000
    stats = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "latency_ms": latency_ms,
        "tier": tier,
        "cost_usd": cost_usd,
    }
    return text, stats
