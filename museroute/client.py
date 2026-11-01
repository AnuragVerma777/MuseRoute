"""OpenAI-compatible HTTP client with startup policy enforcement."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import yaml
from dotenv import load_dotenv


class MuseRouteError(RuntimeError):
    """Base client error."""


class ConfigurationError(MuseRouteError):
    """A tier or policy configuration is invalid."""


class AuthenticationError(MuseRouteError):
    """The endpoint rejected authentication."""


class UnknownModelError(MuseRouteError):
    """The endpoint does not recognize the configured model."""


class RejectedParameterError(MuseRouteError):
    """The endpoint rejected a request parameter."""


class FatalPaymentError(MuseRouteError):
    """The host requires payment and cannot be treated as free."""


PAYMENT_FATAL_MESSAGE = "this host requires payment: stop and switch host"
_CONFIG_PATH = Path(__file__).with_name("config.yaml")
load_dotenv()


def _load_config() -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        with _CONFIG_PATH.open(encoding="utf-8") as stream:
            data = yaml.safe_load(stream) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"Could not load configuration: {exc}") from exc
    tiers = data.get("tiers")
    policy = data.get("policy")
    if not isinstance(tiers, dict):
        raise ConfigurationError("Configuration must contain a 'tiers' mapping")
    if not isinstance(policy, dict):
        raise ConfigurationError("Configuration must contain a 'policy' mapping")
    # Resolve whole-value ${ENV_VAR} references while preserving null TODOs.
    for tier_config in tiers.values():
        if not isinstance(tier_config, dict):
            continue
        for field in ("base_url", "model_id"):
            value = tier_config.get(field)
            if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
                variable = value[2:-1]
                tier_config[field] = os.getenv(variable) or None
    return tiers, policy


def _validate_policy(tiers: dict[str, Any], policy: dict[str, Any]) -> None:
    allowed_models = policy.get("allowed_models") or []
    blocked_hosts = policy.get("blocked_hosts") or []
    if not isinstance(allowed_models, list) or not all(isinstance(item, str) for item in allowed_models):
        raise ConfigurationError("policy.allowed_models must be a list of strings")
    if not isinstance(blocked_hosts, list) or not all(isinstance(item, str) for item in blocked_hosts):
        raise ConfigurationError("policy.blocked_hosts must be a list of strings")

    for tier_name, config in tiers.items():
        if not isinstance(config, dict):
            raise ConfigurationError(f"Tier '{tier_name}' configuration must be a mapping")
        model_id = config.get("model_id")
        if model_id is not None:
            model_text = str(model_id)
            if not any(allowed.casefold() in model_text.casefold() for allowed in allowed_models):
                raise ConfigurationError(
                    f"Tier '{tier_name}' model_id {model_text!r} does not match any policy.allowed_models value"
                )
        base_url = config.get("base_url")
        if base_url is not None:
            url_text = str(base_url)
            hostname = (urlparse(url_text).hostname or "").casefold()
            if any(
                hostname == host.casefold()
                or hostname.endswith("." + host.casefold().lstrip("."))
                for host in blocked_hosts
            ):
                raise ConfigurationError(
                    f"Tier '{tier_name}' base_url {url_text!r} contains blocked host"
                )


_TIERS, _POLICY = _load_config()
_validate_policy(_TIERS, _POLICY)


def _error_from_response(response: requests.Response, tier: str) -> MuseRouteError:
    try:
        payload = response.json()
    except ValueError:
        payload = response.text
    message = str(payload)
    lowered = message.casefold()
    if response.status_code == 402 or any(
        marker in lowered for marker in ("billing", "payment required", "payment_required", "requires payment")
    ):
        print(PAYMENT_FATAL_MESSAGE)
        return FatalPaymentError(PAYMENT_FATAL_MESSAGE)
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
    """Call ``{base_url}/chat/completions`` and return assistant text and stats.

    Retries 429 and 5xx responses three times after the initial request, using
    exponential delays of 1, 2, and 4 seconds. Payment errors are fatal and are
    never retried.
    """
    if tier not in _TIERS:
        raise ConfigurationError(f"Unknown tier '{tier}'. Configured tiers: {', '.join(_TIERS)}")
    config = _TIERS[tier]
    model_id = config.get("model_id")
    if not model_id:
        raise ConfigurationError(f"Tier '{tier}' has no model_id configured")
    base_url = config.get("base_url")
    if not base_url:
        raise ConfigurationError(f"Tier '{tier}' has no base_url configured")

    key_env = config.get("api_key_env")
    api_key = os.getenv(str(key_env)) if key_env else None
    body = {"model": model_id, "messages": messages}
    body.update(config.get("extra_params") or {})
    body.update(overrides)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    url = f"{str(base_url).rstrip('/')}/chat/completions"
    started = time.perf_counter()
    response: requests.Response | None = None
    for attempt in range(4):
        try:
            response = requests.post(url, json=body, headers=headers, timeout=120)
        except requests.RequestException as exc:
            raise MuseRouteError(f"Request failed for tier '{tier}': {exc}") from exc
        # Handle 402/payment language before retry logic, so it can never retry.
        if response.status_code == 402:
            raise _error_from_response(response, tier)
        if not response.ok:
            error = _error_from_response(response, tier)
            if isinstance(error, FatalPaymentError):
                raise error
        else:
            break
        if (response.status_code == 429 or 500 <= response.status_code <= 599) and attempt < 3:
            time.sleep(2**attempt)
            continue
        raise error

    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    try:
        payload = response.json()
        text = payload["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        raise MuseRouteError(f"Invalid chat completion response for tier '{tier}'") from exc

    usage = payload.get("usage") or {}
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    weight = config.get("cost_weight")
    cost_units = None
    if weight is not None and prompt_tokens is not None and completion_tokens is not None:
        cost_units = (prompt_tokens + completion_tokens) * weight
    return text, {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "latency_ms": latency_ms,
        "tier": tier,
        "cost_units": cost_units,
    }
