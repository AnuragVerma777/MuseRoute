"""Router sweep over confidence thresholds x self-consistency on the tune set.

Every cheap and strong API answer is cached to ``results/answer_cache.json`` so
repeat runs are free and rate limits are not wasted.  After sweeping, the script
picks the cheapest Pareto-optimal setting within 2 pp of strong-only accuracy,
evaluates it once on the holdout set, and saves everything to
``results/sweep.json``.

Usage (from repo root, venv active, endpoints configured in .env):

    python scripts/split_data.py   # once
    python scripts/sweep.py
"""

from __future__ import annotations

import json
import argparse
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from museroute.checks import score  # noqa: E402

try:
    from museroute import client, router as routing  # noqa: E402

    CLIENT_ERROR: str | None = None
except Exception as exc:
    client = None  # type: ignore[assignment]
    routing = None  # type: ignore[assignment]
    CLIENT_ERROR = f"{type(exc).__name__}: {exc}"

# ── paths ────────────────────────────────────────────────────────────────

TUNE_PATH = ROOT / "data" / "tune.jsonl"
HOLDOUT_PATH = ROOT / "data" / "holdout.jsonl"
CACHE_PATH = ROOT / "results" / "answer_cache.json"
SWEEP_PATH = ROOT / "results" / "sweep.json"
CHART_PATH = ROOT / "results" / "pareto.png"

# ── sweep grid ───────────────────────────────────────────────────────────

THRESHOLDS = [round(0.50 + i * 0.05, 2) for i in range(10)]  # 0.50 … 0.95
ROUTER_TEMP = 0.2
PRECACHE_WORKERS = 4
_CACHE_LOCK = threading.Lock()
if routing is not None:
    routing._CACHE_PATH = CACHE_PATH


# ═══════════════════════════════════════════════════════════════════════════
# Disk cache
# ═══════════════════════════════════════════════════════════════════════════


def _load_cache() -> dict[str, Any]:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_cache(cache: dict[str, Any]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(cache, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    tmp.replace(CACHE_PATH)


def _ckey(tier: str, pid: str, temp: float, attempt: int, tag: str = "") -> str:
    parts = [tier, pid, f"t{temp}", f"a{attempt}"]
    if tag:
        parts.append(tag)
    return "|".join(parts)


def _cached_chat(
    cache: dict[str, Any],
    tier: str,
    pid: str,
    messages: list[dict[str, str]],
    temperature: float = 0,
    attempt: int = 0,
    tag: str = "",
) -> tuple[str, dict[str, Any]]:
    key = _ckey(tier, pid, temperature, attempt, tag)
    with _CACHE_LOCK:
        e = cache.get(key)
    if isinstance(e, dict):
        if isinstance(e.get("answer"), str) and isinstance(e.get("stats"), dict):
            return e["answer"], e["stats"]
        if isinstance(e.get("error"), str):
            error_type = getattr(client, e.get("error_type", "MuseRouteError"), client.MuseRouteError)
            raise error_type(e["error"])
    if CLIENT_ERROR:
        raise RuntimeError(CLIENT_ERROR)
    try:
        answer, stats = client.chat(tier, messages, temperature=temperature)
    except client.RejectedParameterError as exc:
        if "temperature" not in str(exc).casefold():
            raise
        answer, stats = client.chat(tier, messages)
    except Exception as exc:
        with _CACHE_LOCK:
            cache[key] = {"error_type": type(exc).__name__, "error": client.redact_secrets(exc)}
            _save_cache(cache)
        raise
    with _CACHE_LOCK:
        cache[key] = {"answer": answer, "stats": stats}
        _save_cache(cache)
    return answer, stats


def _cached_router_chat(
    cache: dict[str, Any], tier: str, messages: list[dict[str, Any]], params: dict[str, Any], sample: int
) -> tuple[str, dict[str, Any]]:
    key = routing._cache_key(tier, messages, params, sample)
    with _CACHE_LOCK:
        saved = cache.get(key)
    if isinstance(saved, dict) and isinstance(saved.get("answer"), str) and isinstance(saved.get("stats"), dict):
        return saved["answer"], saved["stats"]
    if isinstance(saved, dict) and isinstance(saved.get("error"), str):
        error_type = getattr(client, saved.get("error_type", "MuseRouteError"), client.MuseRouteError)
        raise error_type(saved["error"])
    if CLIENT_ERROR:
        raise RuntimeError(CLIENT_ERROR)
    try:
        answer, stats = client.chat(tier, messages, **params)
    except client.RejectedParameterError as exc:
        if "temperature" not in str(exc).casefold():
            raise
        answer, stats = client.chat(tier, messages)
    except Exception as exc:
        with _CACHE_LOCK:
            cache[key] = {"error_type": type(exc).__name__, "error": client.redact_secrets(exc)}
            _save_cache(cache)
        raise
    with _CACHE_LOCK:
        cache[key] = {"answer": answer, "stats": stats}
        _save_cache(cache)
    return answer, stats


def _tokens(stats: dict[str, Any]) -> int | None:
    prompt_tokens = stats.get("prompt_tokens")
    completion_tokens = stats.get("completion_tokens")
    if not isinstance(prompt_tokens, int) or not isinstance(completion_tokens, int):
        return None
    return prompt_tokens + completion_tokens


def _total_cost(stats_items: list[dict[str, Any]]) -> float | int | None:
    values = [stats.get("cost_units") for stats in stats_items]
    if not values or any(value is None for value in values):
        return None
    return sum(values)


def _resource(row: dict[str, Any]) -> float | int | None:
    """Use measured cost units, or measured total tokens as an explicit fallback."""
    value = row.get("total_cost_units")
    if value is None:
        value = row.get("total_tokens")
    return value if isinstance(value, (int, float)) else None


def _display(value: Any, fmt: str | None = None) -> str:
    if value is None:
        return "NOT MEASURED"
    return format(value, fmt) if fmt else str(value)


# ═══════════════════════════════════════════════════════════════════════════
# Load prompts
# ═══════════════════════════════════════════════════════════════════════════


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                items.append(json.loads(stripped))
    return items


def _required_tune_cache_keys(prompts: list[dict[str, Any]]) -> set[str]:
    """All responses used by direct baselines and every tune-grid setting."""
    required: set[str] = set()
    for p in prompts:
        pid = p["id"]
        messages = [{"role": "user", "content": p["prompt"]}]
        output_format = "json" if p["check"] == "json_fields" else "text"
        prepared = routing._with_confidence_request(messages, output_format)
        required.add(_ckey("cheap", pid, ROUTER_TEMP, 0))
        required.add(_ckey("strong", pid, ROUTER_TEMP, 0))
        required.add(routing._cache_key("cheap", prepared, {"temperature": ROUTER_TEMP}, 0))
        required.add(routing._cache_key("cheap", prepared, {"temperature": ROUTER_TEMP}, 1))
        required.add(routing._cache_key("strong", prepared, {"temperature": ROUTER_TEMP}, 0))
    return required


def _required_holdout_cache_keys(
    cache: dict[str, Any], prompts: list[dict[str, Any]], threshold: float, self_consistency: bool
) -> set[str]:
    """Only direct baselines and route calls used by the chosen holdout setting."""
    required: set[str] = set()
    for prompt in prompts:
        pid = prompt["id"]
        messages = [{"role": "user", "content": prompt["prompt"]}]
        output_format = "json" if prompt["check"] == "json_fields" else "text"
        prepared = routing._with_confidence_request(messages, output_format)
        cheap_key = routing._cache_key("cheap", prepared, {"temperature": ROUTER_TEMP}, 0)
        second_key = routing._cache_key("cheap", prepared, {"temperature": ROUTER_TEMP}, 1)
        strong_key = routing._cache_key("strong", prepared, {"temperature": ROUTER_TEMP}, 0)
        required.update(
            {
                _ckey("cheap", pid, ROUTER_TEMP, 0),
                _ckey("strong", pid, ROUTER_TEMP, 0),
                cheap_key,
            }
        )

        saved_cheap = cache.get(cheap_key)
        if isinstance(saved_cheap, dict) and isinstance(saved_cheap.get("error"), str):
            same_host = (client._TIERS.get("cheap") or {}).get("base_url") == (client._TIERS.get("strong") or {}).get("base_url")
            if saved_cheap.get("error_type") != "FatalPaymentError" or not same_host:
                required.add(strong_key)
            continue
        if not isinstance(saved_cheap, dict) or not isinstance(saved_cheap.get("answer"), str):
            # The missing cheap response is already sufficient to fail preflight.
            continue
        answer, confidence, parse_error = routing._parse_envelope(saved_cheap["answer"])
        escalation = bool(parse_error) or bool(routing._format_error(answer, output_format))
        confidences = [confidence]

        if self_consistency and not escalation:
            required.add(second_key)
            saved_second = cache.get(second_key)
            if isinstance(saved_second, dict) and isinstance(saved_second.get("error"), str):
                required.add(strong_key)
                continue
            if not isinstance(saved_second, dict) or not isinstance(saved_second.get("answer"), str):
                continue
            answer2, confidence2, parse_error2 = routing._parse_envelope(saved_second["answer"])
            confidences.append(confidence2)
            escalation = (
                bool(parse_error2)
                or bool(routing._format_error(answer2, output_format))
                or routing._agreement_key(answer, output_format)
                != routing._agreement_key(answer2, output_format)
            )

        if not escalation and min(confidences, default=0.0) < threshold:
            escalation = True
        if escalation:
            required.add(strong_key)
    return required


def _cache_misses(cache: dict[str, Any], required: set[str]) -> list[str]:
    return sorted(
        key for key in required
        if not isinstance(cache.get(key), dict)
        or not (
            (isinstance(cache[key].get("answer"), str) and isinstance(cache[key].get("stats"), dict))
            or isinstance(cache[key].get("error"), str)
        )
    )


# ═══════════════════════════════════════════════════════════════════════════
# Pre-cache every API call the sweep could need
# ═══════════════════════════════════════════════════════════════════════════


def _precache(cache: dict[str, Any], prompts: list[dict[str, Any]], label: str) -> None:
    def best_effort(call: Any) -> None:
        try:
            call()
        except client.FatalPaymentError:
            raise
        except Exception:
            # Failures are cached and replayed by the router; they are useful
            # trace data and should not stop the rest of the prompt grid.
            return

    def precache_prompt(p: dict[str, Any]) -> str:
        pid = p["id"]
        msgs: list[dict[str, str]] = [{"role": "user", "content": p["prompt"]}]

        best_effort(lambda: _cached_chat(cache, "cheap", pid, msgs, temperature=ROUTER_TEMP))
        best_effort(lambda: _cached_chat(cache, "strong", pid, msgs, temperature=ROUTER_TEMP))

        output_format = "json" if p["check"] == "json_fields" else "text"
        routed_messages = routing._with_confidence_request(msgs, output_format)
        params = {"temperature": ROUTER_TEMP}
        best_effort(lambda: _cached_router_chat(cache, "cheap", routed_messages, params, sample=0))
        best_effort(lambda: _cached_router_chat(cache, "cheap", routed_messages, params, sample=1))
        best_effort(lambda: _cached_router_chat(cache, "strong", routed_messages, params, sample=0))
        return pid

    with ThreadPoolExecutor(max_workers=PRECACHE_WORKERS) as pool:
        futures = [pool.submit(precache_prompt, p) for p in prompts]
        for i, future in enumerate(as_completed(futures), 1):
            pid = future.result()
            sys.stdout.write(f"\r  {label} [{i}/{len(prompts)}] {pid} cached")
            sys.stdout.flush()
    print()


def _precache_baselines(cache: dict[str, Any], prompts: list[dict[str, Any]], label: str) -> None:
    """Cache the two direct baselines without pre-running router variants."""
    def precache_prompt(p: dict[str, Any]) -> str:
        messages = [{"role": "user", "content": p["prompt"]}]
        for tier in ("cheap", "strong"):
            try:
                _cached_chat(cache, tier, p["id"], messages, temperature=ROUTER_TEMP)
            except client.FatalPaymentError:
                raise
            except Exception:
                pass
        return p["id"]

    with ThreadPoolExecutor(max_workers=PRECACHE_WORKERS) as pool:
        futures = [pool.submit(precache_prompt, p) for p in prompts]
        for i, future in enumerate(as_completed(futures), 1):
            pid = future.result()
            sys.stdout.write(f"\r  {label} baselines [{i}/{len(prompts)}] {pid} cached")
            sys.stdout.flush()
    print()


def _precache_selected_route(
    cache: dict[str, Any], prompts: list[dict[str, Any]], threshold: float, self_consistency: bool
) -> None:
    """Fetch only the calls made by the chosen setting, for holdout evaluation."""
    def precache_prompt(prompt: dict[str, Any]) -> str:
        messages = [{"role": "user", "content": prompt["prompt"]}]
        output_format = "json" if prompt["check"] == "json_fields" else "text"
        prepared = routing._with_confidence_request(messages, output_format)
        params = {"temperature": ROUTER_TEMP}
        try:
            first_raw, _ = _cached_router_chat(cache, "cheap", prepared, params, sample=0)
        except Exception as exc:
            same_host = (client._TIERS.get("cheap") or {}).get("base_url") == (client._TIERS.get("strong") or {}).get("base_url")
            if isinstance(exc, client.FatalPaymentError) and same_host:
                return prompt["id"]
            try:
                _cached_router_chat(cache, "strong", prepared, params, sample=0)
            except Exception:
                pass
            return prompt["id"]
        first_answer, first_confidence, parse_error = routing._parse_envelope(first_raw)
        escalation = bool(parse_error) or bool(routing._format_error(first_answer, output_format))
        confidences = [first_confidence]

        if self_consistency and not escalation:
            try:
                second_raw, _ = _cached_router_chat(cache, "cheap", prepared, params, sample=1)
                second_answer, second_confidence, second_error = routing._parse_envelope(second_raw)
                confidences.append(second_confidence)
                escalation = (
                    bool(second_error)
                    or bool(routing._format_error(second_answer, output_format))
                    or routing._agreement_key(first_answer, output_format)
                    != routing._agreement_key(second_answer, output_format)
                )
            except Exception:
                escalation = True

        if not escalation and min(confidences, default=0.0) < threshold:
            escalation = True
        if escalation:
            try:
                _cached_router_chat(cache, "strong", prepared, params, sample=0)
            except Exception:
                pass
        return prompt["id"]

    with ThreadPoolExecutor(max_workers=PRECACHE_WORKERS) as pool:
        futures = [pool.submit(precache_prompt, prompt) for prompt in prompts]
        for i, future in enumerate(as_completed(futures), 1):
            pid = future.result()
            sys.stdout.write(f"\r  holdout route [{i}/{len(prompts)}] {pid} cached")
            sys.stdout.flush()
    print()


# ═══════════════════════════════════════════════════════════════════════════
# Router – evaluate a single prompt under one setting
# ═══════════════════════════════════════════════════════════════════════════


def _eval_route(
    cache: dict[str, Any], p: dict[str, Any], threshold: float, self_consistency: bool
) -> dict[str, Any]:
    # router.route and the direct baselines share one disk cache.
    routing._CACHE_PATH = CACHE_PATH
    answer, trace = routing.route(
        [{"role": "user", "content": p["prompt"]}],
        {
            "threshold": threshold,
            "format_check": "json" if p["check"] == "json_fields" else "text",
            "self_consistency": self_consistency,
            "low_temperature": ROUTER_TEMP,
        },
    )
    prompt_tokens = trace.get("total_prompt_tokens")
    completion_tokens = trace.get("total_completion_tokens")
    tokens = (
        prompt_tokens + completion_tokens
        if isinstance(prompt_tokens, int) and isinstance(completion_tokens, int)
        else None
    )
    return {
        "answer": answer,
        "correct": score(answer, p["gold"], p["check"]),
        "escalated": trace["escalated"],
        "confidence": trace["reason"],
        "tokens": tokens,
        "cost_units": trace["total_cost_units"],
        "latency_ms": trace["total_latency_ms"],
        "trace": trace,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Baselines
# ═══════════════════════════════════════════════════════════════════════════


def _baseline(
    cache: dict[str, Any], prompts: list[dict[str, Any]], tier: str
) -> dict[str, Any]:
    correct = 0
    token_values: list[int | None] = []
    cost_values: list[float | int | None] = []
    total_lat = 0.0
    latency_count = 0
    error_count = 0
    band_c: dict[str, int] = {}
    band_n: dict[str, int] = {}
    details: list[dict[str, Any]] = []

    for p in prompts:
        msgs: list[dict[str, str]] = [{"role": "user", "content": p["prompt"]}]
        error: str | None = None
        try:
            ans, stats = _cached_chat(cache, tier, p["id"], msgs, temperature=ROUTER_TEMP)
        except client.FatalPaymentError:
            raise
        except Exception as exc:
            ans, stats = "", {}
            error = f"{type(exc).__name__}: {client.redact_secrets(exc)}"
            error_count += 1
        ok = score(ans, p["gold"], p["check"])
        if ok:
            correct += 1
        measured_tokens = _tokens(stats) if stats else None
        token_values.append(measured_tokens)
        cost_values.append(stats.get("cost_units"))
        if isinstance(stats.get("request_time_ms"), (int, float)):
            total_lat += stats["request_time_ms"]
            latency_count += 1
        b = p["band"]
        band_n[b] = band_n.get(b, 0) + 1
        band_c[b] = band_c.get(b, 0) + (1 if ok else 0)
        details.append({
            "id": p["id"], "band": b, "correct": ok, "answer": ans,
            "tokens": measured_tokens, "latency_ms": stats.get("request_time_ms"),
            "retry_count": stats.get("retry_count"),
            "retry_sleep_ms": stats.get("retry_sleep_ms"),
            "cost_units": stats.get("cost_units"), "escalated": tier == "strong",
            "error": error,
            "prompt_tokens": stats.get("prompt_tokens"),
            "completion_tokens": stats.get("completion_tokens"),
        })

    n = len(prompts)
    return {
        "tier": tier,
        "accuracy": round(correct / n, 4) if n else 0,
        "total_cost_units": None if any(v is None for v in cost_values) else sum(cost_values),
        "total_tokens": None if any(value is None for value in token_values) else sum(token_values),
        "finished": n - error_count,
        "errors": error_count,
        "avg_total_tokens_per_request": round(sum(token_values) / n, 2) if n and all(value is not None for value in token_values) else None,
        "avg_latency_ms": round(total_lat / latency_count, 2) if latency_count == n and n else None,
        "retry_count": sum(row["retry_count"] for row in details)
        if n and all(isinstance(row.get("retry_count"), int) for row in details) else None,
        "retry_sleep_ms": round(sum(row["retry_sleep_ms"] for row in details), 2)
        if n and all(isinstance(row.get("retry_sleep_ms"), (int, float)) for row in details) else None,
        "band_accuracy": {b: round(band_c.get(b, 0) / band_n[b], 4) for b in sorted(band_n)},
        "bands": _group_metrics(details),
        "details": details,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Sweep
# ═══════════════════════════════════════════════════════════════════════════


def _sweep(
    cache: dict[str, Any], prompts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for sc in [False, True]:
        for th in THRESHOLDS:
            correct = 0
            esc = 0
            token_values: list[int | None] = []
            cost_values: list[float | int | None] = []
            latency_values: list[float | None] = []
            retry_values: list[int | None] = []
            retry_sleep_values: list[float | None] = []
            band_c: dict[str, int] = {}
            band_n: dict[str, int] = {}

            for p in prompts:
                r = _eval_route(cache, p, th, sc)
                if r["correct"]:
                    correct += 1
                if r["escalated"]:
                    esc += 1
                token_values.append(r["tokens"])
                cost_values.append(r["cost_units"])
                latency_values.append(r["latency_ms"])
                retry_values.append(r["trace"].get("retry_count"))
                retry_sleep_values.append(r["trace"].get("retry_sleep_ms"))
                b = p["band"]
                band_n[b] = band_n.get(b, 0) + 1
                band_c[b] = band_c.get(b, 0) + (1 if r["correct"] else 0)

            n = len(prompts)
            results.append(
                {
                    "self_consistency": sc,
                    "threshold": th,
                    "accuracy": round(correct / n, 4),
                    "escalation_rate": round(esc / n, 4),
                    "total_cost_units": None if any(v is None for v in cost_values) else sum(cost_values),
                    "total_tokens": None if any(value is None for value in token_values) else sum(token_values),
                    "avg_total_tokens_per_request": (
                        round(sum(token_values) / n, 2)
                        if n and all(value is not None for value in token_values)
                        else None
                    ),
                    "avg_latency_ms": (
                        round(sum(latency_values) / n, 2)
                        if n and all(isinstance(value, (int, float)) for value in latency_values)
                        else None
                    ),
                    "retry_count": sum(retry_values)
                    if n and all(isinstance(value, int) for value in retry_values) else None,
                    "retry_sleep_ms": round(sum(retry_sleep_values), 2)
                    if n and all(isinstance(value, (int, float)) for value in retry_sleep_values) else None,
                    "band_accuracy": {
                        b: round(band_c.get(b, 0) / band_n[b], 4)
                        for b in sorted(band_n)
                    },
                }
            )
    return results


# ═══════════════════════════════════════════════════════════════════════════
# Pareto frontier and operating-point selection
# ═══════════════════════════════════════════════════════════════════════════


def _pareto(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    optimal: list[dict[str, Any]] = []
    measured = [r for r in results if _resource(r) is not None]
    for r in measured:
        dominated = any(
            o["accuracy"] >= r["accuracy"]
            and _resource(o) <= _resource(r)
            and (
                o["accuracy"] > r["accuracy"]
                or _resource(o) < _resource(r)
            )
            for o in measured
            if o is not r
        )
        if not dominated:
            optimal.append(r)
    return sorted(optimal, key=_resource)


def _pick_op(results: list[dict[str, Any]], strong_acc: float) -> dict[str, Any]:
    cutoff = strong_acc - 0.02
    eligible = [r for r in results if r["accuracy"] >= cutoff and _resource(r) is not None]
    if not eligible:
        raise RuntimeError(
            f"No sweep setting has measured cost/tokens and accuracy within 2 percentage points of strong-only accuracy "
            f"({strong_acc:.1%}); cannot select the requested operating point."
        )
    return min(eligible, key=_resource)


# ═══════════════════════════════════════════════════════════════════════════
# Holdout evaluation (single setting, no tuning)
# ═══════════════════════════════════════════════════════════════════════════


def _eval_set(
    cache: dict[str, Any],
    prompts: list[dict[str, Any]],
    sc: bool,
    threshold: float,
) -> dict[str, Any]:
    correct = 0
    esc = 0
    token_values: list[int | None] = []
    cost_values: list[float | int | None] = []
    latency_values: list[float | None] = []
    band_c: dict[str, int] = {}
    band_n: dict[str, int] = {}
    details: list[dict[str, Any]] = []

    for p in prompts:
        r = _eval_route(cache, p, threshold, sc)
        if r["correct"]:
            correct += 1
        if r["escalated"]:
            esc += 1
        token_values.append(r["tokens"])
        cost_values.append(r["cost_units"])
        latency_values.append(r["latency_ms"])
        b = p["band"]
        band_n[b] = band_n.get(b, 0) + 1
        band_c[b] = band_c.get(b, 0) + (1 if r["correct"] else 0)
        details.append(
            {
                "id": p["id"],
                "band": b,
                "correct": r["correct"],
                "escalated": r["escalated"],
                "confidence": r["confidence"],
                "answer": r["answer"],
                "tokens": r["tokens"],
                "latency_ms": r["latency_ms"],
                "retry_count": r["trace"].get("retry_count"),
                "retry_sleep_ms": r["trace"].get("retry_sleep_ms"),
                "trace": r["trace"],
                "cost_units": r["cost_units"],
            }
        )

    n = len(prompts)
    return {
        "accuracy": round(correct / n, 4),
        "escalation_rate": round(esc / n, 4),
        "total_cost_units": None if any(v is None for v in cost_values) else sum(cost_values),
        "total_tokens": None if any(value is None for value in token_values) else sum(token_values),
        "avg_total_tokens_per_request": (
            round(sum(token_values) / n, 2)
            if n and all(value is not None for value in token_values)
            else None
        ),
        "avg_latency_ms": (
            round(sum(latency_values) / n, 2)
            if n and all(isinstance(value, (int, float)) for value in latency_values)
            else None
        ),
        "retry_count": sum(row["retry_count"] for row in details)
        if n and all(isinstance(row.get("retry_count"), int) for row in details) else None,
        "retry_sleep_ms": round(sum(row["retry_sleep_ms"] for row in details), 2)
        if n and all(isinstance(row.get("retry_sleep_ms"), (int, float)) for row in details) else None,
        "band_accuracy": {
            b: round(band_c.get(b, 0) / band_n[b], 4) for b in sorted(band_n)
        },
        "bands": _group_metrics(details),
        "details": details,
    }


def _group_metrics(details: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for detail in details:
        grouped.setdefault(detail["band"], []).append(detail)
    result: dict[str, dict[str, Any]] = {}
    for band, rows in sorted(grouped.items()):
        costs = [row.get("cost_units") for row in rows]
        result[band] = {
            "accuracy": round(sum(bool(row["correct"]) for row in rows) / len(rows), 4),
            "escalation_rate": round(sum(bool(row.get("escalated")) for row in rows) / len(rows), 4),
            "total_cost_units": None if any(value is None for value in costs) else sum(costs),
            "total_tokens": (
                sum(row["tokens"] for row in rows)
                if all(isinstance(row.get("tokens"), int) for row in rows)
                else None
            ),
            "avg_total_tokens_per_request": (
                round(sum(row["tokens"] for row in rows) / len(rows), 2)
                if all(isinstance(row.get("tokens"), int) for row in rows)
                else None
            ),
            "avg_latency_ms": (
                round(sum(row["latency_ms"] for row in rows if isinstance(row.get("latency_ms"), (int, float)))
                      / sum(isinstance(row.get("latency_ms"), (int, float)) for row in rows), 2)
                if all(isinstance(row.get("latency_ms"), (int, float)) for row in rows)
                else None
            ),
            "retry_count": sum(row["retry_count"] for row in rows)
            if rows and all(isinstance(row.get("retry_count"), int) for row in rows) else None,
            "retry_sleep_ms": round(sum(row["retry_sleep_ms"] for row in rows), 2)
            if rows and all(isinstance(row.get("retry_sleep_ms"), (int, float)) for row in rows) else None,
        }
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Chart
# ═══════════════════════════════════════════════════════════════════════════


def _save_chart(
    sweep_results: list[dict[str, Any]],
    pareto: list[dict[str, Any]],
    cheap_bl: dict[str, Any],
    strong_bl: dict[str, Any],
) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("  matplotlib not installed; skipping chart generation")
        return

    fig, ax = plt.subplots(figsize=(8, 5))

    # Sweep points
    for sc_val, marker, label in [(False, "o", "SC=False"), (True, "s", "SC=True")]:
        pts = [r for r in sweep_results if r["self_consistency"] == sc_val and _resource(r) is not None]
        ax.scatter(
            [_resource(r) for r in pts],
            [r["accuracy"] * 100 for r in pts],
            marker=marker,
            alpha=0.5,
            label=label,
            s=40,
        )

    # Pareto frontier line
    if pareto:
        ax.plot(
            [_resource(r) for r in pareto],
            [r["accuracy"] * 100 for r in pareto],
            "r--",
            linewidth=1.5,
            label="Pareto frontier",
            zorder=5,
        )

    # Baselines
    ax.axhline(
        cheap_bl["accuracy"] * 100,
        color="green",
        linestyle=":",
        linewidth=1,
        label=f"cheap-only ({cheap_bl['accuracy']:.0%})",
    )
    ax.axhline(
        strong_bl["accuracy"] * 100,
        color="purple",
        linestyle=":",
        linewidth=1,
        label=f"strong-only ({strong_bl['accuracy']:.0%})",
    )

    ax.set_xlabel("Cost units (token-count fallback when weights are null)")
    ax.set_ylabel("Accuracy (%)")
    ax.set_title("MuseRoute: Accuracy vs Cost (tune set)")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(str(CHART_PATH), dpi=150)
    plt.close(fig)
    print(f"  Saved chart to {CHART_PATH}")


# ═══════════════════════════════════════════════════════════════════════════
# Printing
# ═══════════════════════════════════════════════════════════════════════════


def _print_sweep(results: list[dict[str, Any]], pareto: list[dict[str, Any]]) -> None:
    pset = {(r["self_consistency"], r["threshold"]) for r in pareto}
    hdr = ("SC", "THRESH", "ACC", "ESC%", "COST_UNITS", "TOKENS", "AVG_TOK/REQ", "LAT_MS", "P")
    rows: list[tuple[str, ...]] = [hdr]
    for r in results:
        star = "*" if (r["self_consistency"], r["threshold"]) in pset else ""
        rows.append(
            (
                str(r["self_consistency"]),
                f"{r['threshold']:.2f}",
                f"{r['accuracy']:.1%}",
                f"{r['escalation_rate']:.1%}",
                _display(r["total_cost_units"]),
                _display(r["total_tokens"]),
                _display(r["avg_total_tokens_per_request"], ".1f"),
                _display(r["avg_latency_ms"], ".1f"),
                star,
            )
        )
    widths = [max(len(row[c]) for row in rows) for c in range(len(hdr))]
    for idx, row in enumerate(rows):
        print(" | ".join(f"{v:<{widths[c]}}" for c, v in enumerate(row)))
        if idx == 0:
            print("-+-".join("-" * w for w in widths))


def _print_holdout(
    ho: dict[str, Any],
    cheap: dict[str, Any],
    strong: dict[str, Any],
    op: dict[str, Any],
) -> None:
    print(f"\n  Operating point: SC={op['self_consistency']}, threshold={op['threshold']:.2f}")
    print(f"  Tune accuracy:  {op['accuracy']:.1%}\n")
    systems = [("Cheap-only", cheap, 0.0), ("Strong-only", strong, 1.0), ("MuseRoute", ho, ho["escalation_rate"])]
    headers = ("BAND", "SYSTEM", "ACCURACY", "ESCALATION", "COST_UNITS", "TOKENS")
    rows = [headers]
    for band in ["overall", "easy", "medium", "hard"]:
        for name, summary, fallback_esc in systems:
            metric = summary if band == "overall" else summary.get("bands", {}).get(band, {})
            if not metric:
                continue
            cost = metric.get("total_cost_units")
            rows.append((band, name, f"{metric['accuracy']:.1%}", f"{metric.get('escalation_rate', fallback_esc):.1%}", _display(cost), _display(metric["total_tokens"])))
    ws = [max(len(row[c]) for row in rows) for c in range(len(headers))]
    for i, row in enumerate(rows):
        print("  " + " | ".join(f"{v:>{ws[c]}}" for c, v in enumerate(row)))
        if i == 0:
            print("  " + "-+-".join("-" * w for w in ws))

    # Worst mistakes
    mistakes = [d for d in ho.get("details", []) if not d["correct"]]
    if mistakes:
        print(f"\n  5 worst holdout mistakes:")
        for d in mistakes[:5]:
            ans_short = (d["answer"] or "")[:60].replace("\n", " ")
            print(
                f"    {d['id']} ({d['band']}) esc={d['escalated']} "
                f"reason={d['confidence']}  ans={ans_short!r}"
            )


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════


def main() -> None:
    parser = argparse.ArgumentParser(description="Sweep routing settings on tune data.")
    parser.add_argument(
        "--reproduce", action="store_true",
        help="use the existing answer cache only; never make network requests",
    )
    args = parser.parse_args()
    for path, name in [(TUNE_PATH, "tune.jsonl"), (HOLDOUT_PATH, "holdout.jsonl")]:
        if not path.exists():
            print(f"{name} not found. Run:  python scripts/split_data.py")
            sys.exit(1)

    tune = _load_jsonl(TUNE_PATH)
    holdout = _load_jsonl(HOLDOUT_PATH)
    cache = _load_cache()
    if not args.reproduce:
        # A new online run retries prior error sentinels but keeps all answers.
        stale_errors = [key for key, value in cache.items() if isinstance(value, dict) and isinstance(value.get("error"), str)]
        for key in stale_errors:
            del cache[key]
        if stale_errors:
            _save_cache(cache)
    if args.reproduce:
        missing_keys = _cache_misses(cache, _required_tune_cache_keys(tune))
        if missing_keys:
            print(
                f"Cannot reproduce offline: answer cache is missing {len(missing_keys)} "
                "of the required response entries; no network requests were made. "
                f"Example missing key: {missing_keys[0]}"
            )
            sys.exit(3)
        print("Reproducing from complete answer cache; network access is disabled.")
    # Refuse to start if a tier is not configured. In particular, this avoids
    # creating partial result/cache artifacts that could look like measurements.
    if CLIENT_ERROR and not args.reproduce:
        print(f"Cannot run sweep: {CLIENT_ERROR}")
        sys.exit(2)
    missing = [
        f"{name}.{field}"
        for name, cfg in client._TIERS.items()
        for field in ("model_id", "base_url")
        if not cfg.get(field)
    ]
    if missing and not args.reproduce:
        print("Cannot run sweep: configure " + ", ".join(missing) + " in .env/config.yaml first.")
        sys.exit(2)
    print(f"Tune: {len(tune)}  Holdout: {len(holdout)}  Cache: {len(cache)} entries\n")

    # ── Phase 1: pre-cache tune ──────────────────────────────────────────
    print("== Phase 1: pre-cache tune answers ==")
    _precache(cache, tune, "tune")

    # ── Phase 2: baselines on tune ───────────────────────────────────────
    print("\n== Phase 2: tune baselines ==")
    cheap_tune = _baseline(cache, tune, "cheap")
    strong_tune = _baseline(cache, tune, "strong")
    print(f"  cheap-only:  acc={cheap_tune['accuracy']:.1%}  cost_units={_display(cheap_tune['total_cost_units'])}  tokens={_display(cheap_tune['total_tokens'])}  lat={_display(cheap_tune['avg_latency_ms'], '.1f')}ms")
    print(f"  strong-only: acc={strong_tune['accuracy']:.1%}  cost_units={_display(strong_tune['total_cost_units'])}  tokens={_display(strong_tune['total_tokens'])}  lat={_display(strong_tune['avg_latency_ms'], '.1f')}ms")
    for label, bl in [("cheap", cheap_tune), ("strong", strong_tune)]:
        print(f"    {label} by band: {bl['band_accuracy']}")

    # ── Phase 3: sweep ───────────────────────────────────────────────────
    print("\n== Phase 3: sweep (tune) ==")
    sweep_results = _sweep(cache, tune)
    pareto = _pareto(sweep_results)
    _print_sweep(sweep_results, pareto)
    print(f"\n  Pareto-optimal ({len(pareto)}):")
    for r in pareto:
        print(
            f"    SC={r['self_consistency']:5}  th={r['threshold']:.2f}  "
            f"acc={r['accuracy']:.1%}  esc={r['escalation_rate']:.1%}  "
            f"cost_units={r['total_cost_units']}  tokens={r['total_tokens']}"
        )

    # ── Phase 4: operating point ─────────────────────────────────────────
    op = _pick_op(sweep_results, strong_tune["accuracy"])
    print(f"\n== Phase 4: operating point ==")
    print(
        f"  SC={op['self_consistency']}, threshold={op['threshold']:.2f}, "
        f"tune_acc={op['accuracy']:.1%} (strong={strong_tune['accuracy']:.1%}, "
        f"cutoff>={strong_tune['accuracy'] - 0.02:.1%})"
    )

    # ── Phase 5: pre-cache holdout + evaluate ────────────────────────────
    print("\n== Phase 5: holdout evaluation ==")
    if args.reproduce:
        holdout_keys = _required_holdout_cache_keys(
            cache, holdout, op["threshold"], op["self_consistency"]
        )
        missing_keys = _cache_misses(cache, holdout_keys)
        if missing_keys:
            print(
                f"Cannot reproduce holdout offline: answer cache is missing {len(missing_keys)} "
                "required response entries; no network requests were made. "
                f"Example missing key: {missing_keys[0]}"
            )
            sys.exit(3)
    _precache_selected_route(cache, holdout, op["threshold"], op["self_consistency"])
    _precache_baselines(cache, holdout, "holdout")

    cheap_ho = _baseline(cache, holdout, "cheap")
    strong_ho = _baseline(cache, holdout, "strong")
    router_ho = _eval_set(cache, holdout, op["self_consistency"], op["threshold"])
    _print_holdout(router_ho, cheap_ho, strong_ho, op)

    # ── Phase 6: chart ───────────────────────────────────────────────────
    print("\n== Phase 6: chart ==")
    _save_chart(sweep_results, pareto, cheap_tune, strong_tune)

    # ── Save ─────────────────────────────────────────────────────────────
    output = {
        "tune_baselines": {
            "cheap": {k: v for k, v in cheap_tune.items() if k != "details"},
            "strong": {k: v for k, v in strong_tune.items() if k != "details"},
        },
        "holdout_baselines": {
            "cheap": {k: v for k, v in cheap_ho.items() if k != "details"},
            "strong": {k: v for k, v in strong_ho.items() if k != "details"},
        },
        "sweep": sweep_results,
        "pareto": pareto,
        "operating_point": {
            "self_consistency": op["self_consistency"],
            "threshold": op["threshold"],
            "selection_basis": "cost_units" if op["total_cost_units"] is not None else "total_tokens_fallback",
            "tune": {k: v for k, v in op.items() if k != "details"},
            "holdout": router_ho,
        },
    }
    SWEEP_PATH.parent.mkdir(parents=True, exist_ok=True)
    SWEEP_PATH.write_text(
        json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"\n  Saved results to {SWEEP_PATH}")


if __name__ == "__main__":
    main()
