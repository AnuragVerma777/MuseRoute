"""Run the workload independently against each configured tier."""

from __future__ import annotations

import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from museroute.checks import score  # noqa: E402

try:
    from museroute import client  # noqa: E402
    CLIENT_IMPORT_ERROR: str | None = None
except Exception as exc:  # keep a resumable diagnostic output if dependencies are absent
    client = None
    CLIENT_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

WORKLOAD_PATH = ROOT / "data" / "workload.jsonl"
RESULT_PATH = ROOT / "results" / "baseline.json"
TEMP_PATH = ROOT / "results" / "baseline.json.tmp"
MAX_RATE_LIMIT_RETRIES = 3


def _load_workload() -> list[dict[str, Any]]:
    prompts = []
    with WORKLOAD_PATH.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                prompts.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid workload JSON on line {line_number}: {exc}") from exc
    return prompts


def _load_existing() -> dict[tuple[str, str], dict[str, Any]]:
    if not RESULT_PATH.exists():
        return {}
    try:
        existing = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(existing, list):
        return {}
    saved = {}
    for item in existing:
        if isinstance(item, dict) and isinstance(item.get("tier"), str) and isinstance(item.get("id"), str):
            saved[(item["tier"], item["id"])] = item
    return saved


def _save(items: dict[tuple[str, str], dict[str, Any]]) -> None:
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(items.values(), key=lambda row: (row["tier"], row["id"]))
    TEMP_PATH.write_text(json.dumps(ordered, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    TEMP_PATH.replace(RESULT_PATH)


def _finished(item: dict[str, Any] | None) -> bool:
    return bool(item is not None and item.get("error") is None and isinstance(item.get("correct"), bool))


def _new_row(tier: str, prompt: dict[str, Any], *, answer: str | None = None,
             correct: bool = False, tokens: Any = None, latency_ms: float | None = None,
             cost_usd: float | None = None, error: str | None = None) -> dict[str, Any]:
    return {
        "id": prompt["id"],
        "tier": tier,
        "band": prompt["band"],
        "correct": correct,
        "answer": answer,
        "tokens": tokens,
        "latency_ms": latency_ms,
        "cost_usd": cost_usd,
        "error": error,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


def _chat_once(tier: str, prompt: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    messages = [{"role": "user", "content": prompt["prompt"]}]
    try:
        return client.chat(tier, messages, temperature=0)
    except client.RejectedParameterError as exc:
        if "temperature" not in str(exc).casefold():
            raise
        # Some compatible hosts reject sampling controls. Retry this prompt once
        # without temperature, and only after the endpoint explicitly rejects it.
        return client.chat(tier, messages)


def _call_with_rate_limit_retry(tier: str, prompt: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    retry_index = 0
    while True:
        try:
            return _chat_once(tier, prompt)
        except Exception as exc:
            message = str(exc)
            if "429" not in message or retry_index >= MAX_RATE_LIMIT_RETRIES:
                raise
            delay = min(2 ** retry_index, 60) + random.uniform(0, 0.25)
            print(f"Rate limited on tier {tier}; retrying {prompt['id']} in {delay:.2f}s")
            time.sleep(delay)
            retry_index += 1


def _run_item(tier: str, prompt: dict[str, Any]) -> dict[str, Any]:
    if CLIENT_IMPORT_ERROR:
        return _new_row(tier, prompt, error=CLIENT_IMPORT_ERROR)
    try:
        answer, stats = _call_with_rate_limit_retry(tier, prompt)
        token_usage = {
            "prompt_tokens": stats.get("prompt_tokens"),
            "completion_tokens": stats.get("completion_tokens"),
            "total_tokens": None,
        }
        p_tokens = token_usage["prompt_tokens"]
        c_tokens = token_usage["completion_tokens"]
        if isinstance(p_tokens, int) and isinstance(c_tokens, int):
            token_usage["total_tokens"] = p_tokens + c_tokens
        return _new_row(
            tier,
            prompt,
            answer=answer,
            correct=score(answer, prompt["gold"], prompt["check"]),
            tokens=token_usage,
            latency_ms=stats.get("latency_ms"),
            # The current project config has relative cost weights but no verified
            # price fields, so they cannot be converted to USD.
            cost_usd=stats.get("cost_usd"),
        )
    except KeyboardInterrupt:
        raise
    except client.FatalPaymentError:
        # Payment errors are a hard stop for the entire baseline, not a row-level
        # model failure to be retried across the remaining prompts.
        raise
    except Exception as exc:
        return _new_row(tier, prompt, error=f"{type(exc).__name__}: {exc}")


def _print_summary(items: dict[tuple[str, str], dict[str, Any]], tiers: list[str], prompts: list[dict[str, Any]]) -> None:
    print("\nBaseline summary")
    headers = ("TIER", "BAND", "ACCURACY", "AVG LATENCY MS", "TOTAL TOKENS", "TOTAL COST USD")
    rows: list[tuple[str, ...]] = []
    bands = ("all", "easy", "medium", "hard")
    for tier in tiers:
        for band in bands:
            expected = prompts if band == "all" else [p for p in prompts if p["band"] == band]
            completed = [items[(tier, p["id"])] for p in expected if _finished(items.get((tier, p["id"])))]
            denominator = len(expected)
            if completed:
                accurate = sum(bool(row["correct"]) for row in completed)
                accuracy = f"{accurate / len(completed):.1%} ({accurate}/{len(completed)} complete)"
                latencies = [row["latency_ms"] for row in completed if isinstance(row.get("latency_ms"), (int, float))]
                latency = f"{sum(latencies) / len(latencies):.1f}" if latencies else "n/a"
                token_total = sum((row.get("tokens") or {}).get("total_tokens") or 0 for row in completed)
                costs = [row["cost_usd"] for row in completed if isinstance(row.get("cost_usd"), (int, float))]
                cost = f"${sum(costs):.6f}" if costs else "unknown"
            else:
                accuracy, latency, token_total, cost = f"n/a (0/{denominator} complete)", "n/a", "0", "unknown"
            rows.append((tier, band, accuracy, latency, str(token_total), cost))
    matrix = [headers, *rows]
    widths = [max(len(row[index]) for row in matrix) for index in range(len(headers))]
    for index, row in enumerate(matrix):
        print(" | ".join(f"{value:<{widths[i]}}" for i, value in enumerate(row)))
        if index == 0:
            print("-+-".join("-" * width for width in widths))
    failures = sum(1 for item in items.values() if item.get("error"))
    print(f"\nSaved {len(items)} tier/prompt records ({failures} errors) to {RESULT_PATH}")


def run() -> dict[tuple[str, str], dict[str, Any]]:
    if CLIENT_IMPORT_ERROR:
        print(f"Cannot run baseline: MuseRoute client could not load: {CLIENT_IMPORT_ERROR}")
        raise SystemExit(1)
    missing_by_tier = {
        tier: [field for field in ("base_url", "model_id") if not config.get(field)]
        for tier, config in client._TIERS.items()
    }
    missing_by_tier = {tier: fields for tier, fields in missing_by_tier.items() if fields}
    if missing_by_tier:
        details = "; ".join(
            f"{tier} missing {', '.join(fields)}" for tier, fields in missing_by_tier.items()
        )
        print(f"Cannot run baseline: configure each tier's base_url and model_id first ({details}).")
        raise SystemExit(1)

    prompts = _load_workload()
    tiers = list(client._TIERS)
    items = _load_existing()
    try:
        for tier in tiers:
            for prompt in prompts:
                key = (tier, prompt["id"])
                if _finished(items.get(key)):
                    continue
                items[key] = _run_item(tier, prompt)
                _save(items)
    except client.FatalPaymentError as exc:
        _save(items)
        print(f"\nBaseline stopped: {exc}")
        raise SystemExit(2)
    except KeyboardInterrupt:
        _save(items)
        print("\nInterrupted; completed results have been saved. Re-run to resume.")
    finally:
        _save(items)
    _print_summary(items, tiers, prompts)
    return items


if __name__ == "__main__":
    run()
