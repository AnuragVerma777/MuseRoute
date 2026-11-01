"""Live tier smoke checks; requires configured endpoints and installed requirements."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from museroute import client
    CLIENT_IMPORT_ERROR: str | None = None
except Exception as exc:  # retain a report when the runtime dependencies are unavailable
    client = None
    CLIENT_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

RESULT_PATH = ROOT / "results" / "smoke.json"
PLAIN_MESSAGES = [{"role": "user", "content": "Reply with exactly: museroute smoke check passed."}]
JSON_MESSAGES = [{
    "role": "user",
    "content": 'Return only a valid JSON object with exactly these fields: {"ok": true, "count": 3}.',
}]
PAYMENT_MESSAGES = [{"role": "user", "content": "Reply with the word ok."}]

# Documentation-backed names only. The Meta entry is informational and the
# policy blocks that host, so client startup rejects it before any request.
# Add a host only after confirming its official Chat Completions docs.
REASONING_DOCS: dict[str, tuple[str, dict[str, Any], dict[str, Any], str]] = {
    "integrate.api.nvidia.com": (
        "reasoning_effort",
        {"reasoning_effort": "high"},
        {"reasoning_effort": "none"},
        "https://docs.api.nvidia.com/nim/reference/meta-muse-glimmer-30b-infer",
    ),
    "api.meta.ai": (
        "reasoning_effort",
        {"reasoning_effort": "high"},
        {"reasoning_effort": "none"},
        "https://dev.meta.ai/docs/protocols/chat-completions",
    ),
}
PAYMENT_MARKERS = ("this host requires payment", "payment required", "payment_required", "billing", "http 402")


def result(tier: str, check: str, status: str, detail: str = "") -> dict[str, str]:
    return {"tier": tier, "check": check, "status": status, "detail": detail}


def payment_error(detail: str) -> bool:
    lowered = detail.casefold()
    return any(marker in lowered for marker in PAYMENT_MARKERS)


def run_check(
    tier: str,
    check: str,
    messages: list[dict[str, str]],
    validator: Callable[[str, dict[str, Any]], str | None] | None = None,
    **overrides: Any,
) -> dict[str, str]:
    if CLIENT_IMPORT_ERROR:
        return result(tier, check, "FAIL", CLIENT_IMPORT_ERROR)
    config = client._TIERS.get(tier)
    if not isinstance(config, dict):
        return result(tier, check, "FAIL", f"Unknown tier {tier!r}; no tier configuration found")
    missing = [field for field in ("base_url", "model_id") if not config.get(field)]
    if missing:
        return result(
            tier,
            check,
            "FAIL",
            f"Tier '{tier}' is not configured: missing {', '.join(missing)}",
        )
    try:
        text, stats = client.chat(tier, messages, **overrides)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        if payment_error(error):
            return result(tier, check, "NOT FREE", error)
        return result(tier, check, "FAIL", error)
    if validator:
        error = validator(text, stats)
        if error:
            return result(tier, check, "FAIL", error)
    return result(tier, check, "PASS", f"reply={text!r}; stats={stats}")


def validate_plain(text: str, _stats: dict[str, Any]) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return f"Expected non-empty plain text reply; received {text!r}"
    return None


def validate_json(text: str, _stats: dict[str, Any]) -> str | None:
    try:
        json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        return f"Reply is not valid JSON: {type(exc).__name__}: {exc}; reply={text!r}"
    return None


def validate_usage(_text: str, stats: dict[str, Any]) -> str | None:
    prompt = stats.get("prompt_tokens")
    completion = stats.get("completion_tokens")
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return f"Missing or invalid token usage: prompt_tokens={prompt!r}, completion_tokens={completion!r}"
    return None


def _reasoning_docs(config: dict[str, Any]) -> tuple[list[tuple[str, dict[str, Any], dict[str, Any], str]], str | None]:
    base_url = config.get("base_url")
    if not base_url:
        return [], "base_url is not configured; host documentation cannot be identified"
    hostname = (urlparse(str(base_url)).hostname or "").casefold()
    documented = REASONING_DOCS.get(hostname)
    if not documented:
        return [], f"No verified reasoning parameter names found for host {hostname or str(base_url)!r}; no guesses made"
    return [documented], None


def _known_tiers_without_client() -> list[str]:
    # The project's config has the two named tiers; this fallback is only used
    # to report a missing client dependency and never sends network requests.
    return ["cheap", "strong"]


def run() -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    if CLIENT_IMPORT_ERROR:
        for tier in _known_tiers_without_client():
            for check in ("plain text reply", "valid JSON reply", "token usage returned", "payment/free check", "extra_params accepted"):
                results.append(result(tier, check, "FAIL", CLIENT_IMPORT_ERROR))
        results.append(result("strong", "reasoning parameters", "FAIL", "Cannot identify host docs because client dependencies could not load."))
        return _save_and_print(results)

    for tier, config in client._TIERS.items():
        missing = [field for field in ("base_url", "model_id") if not config.get(field)]
        if missing:
            reason = f"Tier '{tier}' is not configured: missing {', '.join(missing)}"
            for check in (
                "plain text reply",
                "valid JSON reply",
                "token usage returned",
                "payment/free check",
                "extra_params accepted",
            ):
                results.append(result(tier, check, "FAIL", reason))
            if tier == "strong":
                results.append(result(tier, "reasoning parameter discovery", "SKIP", reason))
                results.append(result(tier, "accepted reasoning parameter", "SKIP", reason))
            continue

        results.append(run_check(tier, "plain text reply", PLAIN_MESSAGES, validate_plain))
        results.append(run_check(tier, "valid JSON reply", JSON_MESSAGES, validate_json))
        results.append(run_check(tier, "token usage returned", PLAIN_MESSAGES, validate_usage))

        # A separate successful probe means no payment error was observed. A
        # 402/billing response is recorded as NOT FREE and is never retried by client.chat.
        results.append(run_check(tier, "payment/free check", PAYMENT_MESSAGES, validate_plain))

        extra_params = config.get("extra_params") or {}
        if extra_params:
            # client.chat includes these configured parameters in the request body.
            results.append(run_check(tier, "extra_params accepted", PLAIN_MESSAGES, validate_plain))
        else:
            results.append(result(tier, "extra_params accepted", "SKIP", "No extra_params configured; nothing to test."))

        if tier == "strong":
            candidates, lookup_error = _reasoning_docs(config)
            if lookup_error:
                results.append(result(tier, "reasoning parameter discovery", "SKIP", lookup_error))
                results.append(result(tier, "accepted reasoning parameter", "SKIP", "No documented candidate could be tested."))
                continue
            accepted: list[str] = []
            for key, on_params, off_params, docs_url in candidates:
                on = run_check(tier, f"reasoning on ({key})", PLAIN_MESSAGES, validate_plain, **on_params)
                off = run_check(tier, f"reasoning off ({key})", PLAIN_MESSAGES, validate_plain, **off_params)
                on["detail"] = f"{on['detail']} [docs: {docs_url}]"
                off["detail"] = f"{off['detail']} [docs: {docs_url}]"
                results.extend((on, off))
                if on["status"] == "PASS" or off["status"] == "PASS":
                    accepted.append(key)
            if accepted:
                results.append(result(tier, "accepted reasoning parameter", "PASS", ", ".join(accepted)))
            else:
                results.append(result(tier, "accepted reasoning parameter", "FAIL", "No documented reasoning parameter was accepted."))

    return _save_and_print(results)


def _save_and_print(results: list[dict[str, str]]) -> list[dict[str, str]]:
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    headers = ("TIER", "CHECK", "STATUS", "EXACT ERROR / DETAIL")
    rows = [headers] + [tuple(item.get(key, "") for key in ("tier", "check", "status", "detail")) for item in results]
    widths = [max(len(row[col]) for row in rows) for col in range(len(headers))]
    print(" | ".join(f"{value:<{widths[index]}}" for index, value in enumerate(headers)))
    print("-+-".join("-" * width for width in widths))
    for row in rows[1:]:
        print(" | ".join(f"{value:<{widths[index]}}" for index, value in enumerate(row)))
    failed = sum(item["status"] in {"FAIL", "NOT FREE"} for item in results)
    skipped = sum(item["status"] == "SKIP" for item in results)
    passed = sum(item["status"] == "PASS" for item in results)
    print(f"\nSummary: {passed} PASS, {failed} FAIL/NOT FREE, {skipped} SKIP")
    print(f"\nSaved results to {RESULT_PATH}")
    return results


if __name__ == "__main__":
    final_results = run()
    if any(item["status"] in {"FAIL", "NOT FREE"} for item in final_results):
        raise SystemExit(1)
