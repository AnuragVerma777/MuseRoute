"""Build a measured project facts document from the saved evaluation artifacts."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASELINE_PATH = ROOT / "results" / "baseline.json"
SWEEP_PATH = ROOT / "results" / "sweep.json"
FACTS_PATH = ROOT / "results" / "facts.json"
WORKLOAD_PATH = ROOT / "data" / "workload.jsonl"
TUNE_PATH = ROOT / "data" / "tune.jsonl"
HOLDOUT_PATH = ROOT / "data" / "holdout.jsonl"


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def _mean(values: list[Any], expected: int | None = None) -> float | None:
    if not values or any(not isinstance(value, (int, float)) for value in values):
        return None
    if expected is not None and len(values) != expected:
        return None
    return round(sum(values) / len(values), 2)


def _accuracy(rows: list[dict[str, Any]], expected: int) -> float | None:
    if expected <= 0 or not rows:
        return None
    if len(rows) != expected:
        return None
    return round(sum(bool(row.get("correct")) for row in rows) / expected, 4)


def _host(base_url: Any) -> str | None:
    if not isinstance(base_url, str) or not base_url.strip():
        return None
    return urlparse(base_url).hostname


def _baseline_facts(
    rows: list[dict[str, Any]], workload: list[dict[str, Any]], tiers: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    expected = len(workload)
    by_band_rows: dict[str, list[dict[str, Any]]] = {}
    workload_bands: dict[str, int] = {}
    for prompt in workload:
        workload_bands[prompt["band"]] = workload_bands.get(prompt["band"], 0) + 1
    for row in rows:
        by_band_rows.setdefault(row.get("tier", ""), []).append(row)

    counts: dict[str, Any] = {}
    averages: dict[str, Any] = {}
    for tier_name, config in tiers.items():
        tier_rows = by_band_rows.get(tier_name, [])
        errors = sum(bool(row.get("error")) for row in tier_rows)
        finished_rows = [row for row in tier_rows if not row.get("error") and isinstance(row.get("correct"), bool)]
        counts[tier_name] = {
            "expected": expected,
            "finished": len(finished_rows),
            "errors": errors,
        }
        prompt_token_values = [(row.get("tokens") or {}).get("prompt_tokens") for row in finished_rows]
        completion_token_values = [(row.get("tokens") or {}).get("completion_tokens") for row in finished_rows]
        total_token_values = [(row.get("tokens") or {}).get("total_tokens") for row in finished_rows]
        latency_values = [row.get("latency_ms") for row in finished_rows]
        averages[tier_name] = {
            "average_prompt_tokens": _mean(prompt_token_values, expected),
            "average_completion_tokens": _mean(completion_token_values, expected),
            "average_total_tokens": _mean(total_token_values, expected),
            "average_latency_ms": _mean(latency_values, expected),
            "cost_units": None,
        }

    baseline_accuracy: dict[str, Any] = {}
    for tier_name in tiers:
        tier_rows = by_band_rows.get(tier_name, [])
        baseline_accuracy[tier_name] = {
            "overall": _accuracy(tier_rows, expected),
            "by_band": {
                band: _accuracy([row for row in tier_rows if row.get("band") == band], count)
                for band, count in sorted(workload_bands.items())
            },
        }
    return counts, {"averages": averages, "accuracy": baseline_accuracy}


def _system_metrics(
    source: dict[str, Any] | None,
    prompt_count: int,
    *,
    default_escalation: float | None = None,
    failed_prompts: int | None = None,
    call_errors: int | None = None,
) -> dict[str, Any]:
    source = source or {}
    errors = source.get("errors")
    finished = source.get("finished")
    if failed_prompts is not None:
        finished = prompt_count - failed_prompts
        errors = failed_prompts
    if finished is None and isinstance(errors, int):
        finished = prompt_count - errors
    if errors is None and finished == prompt_count:
        errors = 0
    total_tokens = source.get("total_tokens")
    return {
        "accuracy": source.get("accuracy"),
        "escalation_rate": source.get("escalation_rate", default_escalation),
        "finished_prompts": finished,
        "errors": errors,
        "tier_call_errors": call_errors,
        "average_total_tokens": source.get("avg_total_tokens_per_request"),
        "total_tokens": total_tokens,
        "latency_ms": source.get("avg_latency_ms"),
        "cost_units": source.get("total_cost_units"),
    }


def _mistake_reason(check: str) -> str:
    return {
        "exact": "The answer did not match the expected value exactly.",
        "numeric": "The numeric answer did not match the expected value.",
        "json_fields": "One or more expected JSON fields were missing or different.",
        "contains": "The answer omitted text required by the check.",
    }.get(check, "The answer failed the configured check.")


def build_facts(
    baseline_rows: list[dict[str, Any]],
    sweep: dict[str, Any],
    workload: list[dict[str, Any]],
    tune: list[dict[str, Any]],
    holdout: list[dict[str, Any]],
    tiers: dict[str, Any],
) -> dict[str, Any]:
    baseline_counts, baseline = _baseline_facts(baseline_rows, workload, tiers)
    operating_point = sweep.get("operating_point") or {}
    holdout_router = operating_point.get("holdout") or {}
    holdout_baselines = sweep.get("holdout_baselines") or {}
    tier_order = list(tiers)
    cheap_name = "cheap" if "cheap" in tiers else (tier_order[0] if tier_order else None)
    strong_name = "strong" if "strong" in tiers else (tier_order[1] if len(tier_order) > 1 else None)
    workload_by_id = {prompt.get("id"): prompt for prompt in workload}
    holdout_counts: dict[str, int] = {}
    for prompt in holdout:
        holdout_counts[prompt["band"]] = holdout_counts.get(prompt["band"], 0) + 1

    def route_failures(details: list[dict[str, Any]]) -> tuple[int, int]:
        failed = 0
        errors = 0
        for detail in details:
            reason = str(detail.get("confidence") or "")
            errors += reason.count("call_error")
            if not detail.get("answer"):
                failed += 1
        return failed, errors

    router_details = holdout_router.get("details") or []
    overall_failed, overall_call_errors = route_failures(router_details)
    systems = {
        "cheap_only": holdout_baselines.get(cheap_name, {}) if cheap_name else {},
        "strong_only": holdout_baselines.get(strong_name, {}) if strong_name else {},
        "museroute": holdout_router,
    }
    overall: dict[str, Any] = {"prompt_count": len(holdout)}
    for system_name, source in systems.items():
        if system_name == "cheap_only":
            metrics = _system_metrics(source, len(holdout), default_escalation=0.0)
        elif system_name == "strong_only":
            metrics = _system_metrics(source, len(holdout), default_escalation=1.0)
        else:
            metrics = _system_metrics(
                source, len(holdout), failed_prompts=overall_failed, call_errors=overall_call_errors
            )
        overall[system_name] = metrics

    by_band: dict[str, Any] = {}
    for band, prompt_count in sorted(holdout_counts.items()):
        router_band_details = [detail for detail in router_details if detail.get("band") == band]
        band_failed, band_call_errors = route_failures(router_band_details)
        band_metrics: dict[str, Any] = {"prompt_count": prompt_count}
        for system_name, source in systems.items():
            band_source = (source.get("bands") or {}).get(band, {})
            if system_name == "cheap_only":
                metrics = _system_metrics(band_source, prompt_count, default_escalation=0.0)
            elif system_name == "strong_only":
                metrics = _system_metrics(band_source, prompt_count, default_escalation=1.0)
            else:
                metrics = _system_metrics(
                    band_source, prompt_count, failed_prompts=band_failed, call_errors=band_call_errors
                )
            band_metrics[system_name] = metrics
        by_band[band] = band_metrics

    # Rank only incorrect answers; failed requests are recorded separately and
    # never mislabeled as model mistakes.
    mistakes = []
    for detail in router_details:
        if detail.get("correct") or not detail.get("answer"):
            continue
        prompt = workload_by_id.get(detail.get("id"), {})
        check = prompt.get("check")
        mistakes.append(
            {
                "id": detail.get("id"),
                "band": detail.get("band"),
                "check": check,
                "answer": detail.get("answer"),
                "gold": prompt.get("gold"),
                "reason": _mistake_reason(str(check)),
            }
        )
    band_priority = {"hard": 0, "medium": 1, "easy": 2}
    mistakes.sort(key=lambda row: (band_priority.get(row.get("band"), 3), str(row.get("id"))))

    models = {}
    for tier_name, config in tiers.items():
        models[tier_name] = {
            "model_id": config.get("model_id") if isinstance(config, dict) else None,
            "host": _host(config.get("base_url")) if isinstance(config, dict) else None,
        }

    return {
        "prompt_counts": {
            "workload": len(workload),
            "tune": len(tune),
            "holdout": len(holdout),
            "baseline": baseline_counts,
        },
        "models_and_hosts": models,
        "baseline": baseline,
        "holdout": {"overall": overall, "by_band": by_band},
        "router": {
            "chosen_threshold": operating_point.get("threshold"),
            "self_consistency": operating_point.get("self_consistency"),
            "selection_basis": operating_point.get("selection_basis"),
            "tune_accuracy": (operating_point.get("tune") or {}).get("accuracy"),
            "strong_only_tune_accuracy": (sweep.get("tune_baselines", {}).get(strong_name) or {}).get("accuracy") if strong_name else None,
            # Sweep artifacts contain only total tokens per routed prompt; they
            # do not retain a prompt/completion split for each call.
            "average_prompt_tokens": None,
            "average_completion_tokens": None,
            "average_total_tokens": holdout_router.get("avg_total_tokens_per_request"),
            "latency_ms": holdout_router.get("avg_latency_ms"),
            "cost_units": holdout_router.get("total_cost_units"),
        },
        "worst_holdout_mistakes": mistakes[:5],
        "holdout_request_failures": [
            {
                "id": detail.get("id"),
                "band": detail.get("band"),
                "reason": detail.get("confidence"),
            }
            for detail in router_details
            if not detail.get("answer")
        ],
    }


def main() -> None:
    from museroute import client

    required = [BASELINE_PATH, SWEEP_PATH, WORKLOAD_PATH, TUNE_PATH, HOLDOUT_PATH]
    missing = [str(path.relative_to(ROOT)) for path in required if not path.exists()]
    if missing:
        raise SystemExit("Cannot build results/facts.json; missing: " + ", ".join(missing))
    facts = build_facts(
        _read_json(BASELINE_PATH),
        _read_json(SWEEP_PATH),
        _read_jsonl(WORKLOAD_PATH),
        _read_jsonl(TUNE_PATH),
        _read_jsonl(HOLDOUT_PATH),
        client._TIERS,
    )
    FACTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    FACTS_PATH.write_text(json.dumps(facts, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote measured facts to {FACTS_PATH}")


if __name__ == "__main__":
    main()
