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
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from museroute.checks import score  # noqa: E402

try:
    from museroute import client  # noqa: E402

    CLIENT_ERROR: str | None = None
except Exception as exc:
    client = None  # type: ignore[assignment]
    CLIENT_ERROR = f"{type(exc).__name__}: {exc}"

# ── paths ────────────────────────────────────────────────────────────────

TUNE_PATH = ROOT / "data" / "tune.jsonl"
HOLDOUT_PATH = ROOT / "data" / "holdout.jsonl"
CACHE_PATH = ROOT / "results" / "answer_cache.json"
SWEEP_PATH = ROOT / "results" / "sweep.json"
CHART_PATH = ROOT / "results" / "pareto.png"

# ── sweep grid ───────────────────────────────────────────────────────────

THRESHOLDS = [round(0.50 + i * 0.05, 2) for i in range(10)]  # 0.50 … 0.95
SC_K = 3  # samples per prompt in self-consistency mode
SC_TEMP = 0.7


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
    if key in cache:
        e = cache[key]
        return e["answer"], e["stats"]
    if CLIENT_ERROR:
        raise RuntimeError(CLIENT_ERROR)
    try:
        answer, stats = client.chat(tier, messages, temperature=temperature)
    except client.RejectedParameterError as exc:
        if "temperature" not in str(exc).casefold():
            raise
        answer, stats = client.chat(tier, messages)
    cache[key] = {"answer": answer, "stats": stats}
    _save_cache(cache)
    return answer, stats


def _tokens(stats: dict[str, Any]) -> int:
    return (stats.get("prompt_tokens") or 0) + (stats.get("completion_tokens") or 0)


def _total_cost(stats_items: list[dict[str, Any]]) -> float | int | None:
    values = [stats.get("cost_units") for stats in stats_items]
    if not values or any(value is None for value in values):
        return None
    return sum(values)


def _resource(row: dict[str, Any]) -> float | int:
    """Use configured cost units, or token count as a transparent fallback."""
    return row["total_cost_units"] if row.get("total_cost_units") is not None else row["total_tokens"]


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


# ═══════════════════════════════════════════════════════════════════════════
# Pre-cache every API call the sweep could need
# ═══════════════════════════════════════════════════════════════════════════


def _precache(cache: dict[str, Any], prompts: list[dict[str, Any]], label: str) -> None:
    n = len(prompts)
    for i, p in enumerate(prompts, 1):
        pid = p["id"]
        msgs: list[dict[str, str]] = [{"role": "user", "content": p["prompt"]}]

        # 1. cheap @ temp=0 (non-SC baseline & confidence probe source)
        answer_t0, _ = _cached_chat(cache, "cheap", pid, msgs, temperature=0)

        # 2. confidence probe for non-SC mode
        probe: list[dict[str, str]] = [
            {"role": "user", "content": p["prompt"]},
            {"role": "assistant", "content": answer_t0},
            {
                "role": "user",
                "content": (
                    "Rate your confidence in your previous answer from 0.0 "
                    "to 1.0.  Reply with only the number."
                ),
            },
        ]
        _cached_chat(cache, "cheap", pid, probe, temperature=0, tag="conf")

        # 3. cheap @ SC_TEMP x SC_K (self-consistency mode)
        for k in range(SC_K):
            _cached_chat(cache, "cheap", pid, msgs, temperature=SC_TEMP, attempt=k)

        # 4. strong @ temp=0 (escalation target + strong-only baseline)
        _cached_chat(cache, "strong", pid, msgs, temperature=0)

        sys.stdout.write(f"\r  {label} [{i}/{n}] {pid} cached")
        sys.stdout.flush()
    print()


# ═══════════════════════════════════════════════════════════════════════════
# Router – evaluate a single prompt under one setting
# ═══════════════════════════════════════════════════════════════════════════


def _parse_confidence(text: str) -> float:
    m = re.search(r"(0\.\d+|1\.0?|0(?!\.\d)|1(?!\.\d))", text.strip())
    return max(0.0, min(1.0, float(m.group(1)))) if m else 0.5


def _eval_no_sc(
    cache: dict[str, Any], p: dict[str, Any], threshold: float
) -> dict[str, Any]:
    pid = p["id"]
    msgs: list[dict[str, str]] = [{"role": "user", "content": p["prompt"]}]

    answer, stats0 = _cached_chat(cache, "cheap", pid, msgs, temperature=0)

    probe: list[dict[str, str]] = [
        {"role": "user", "content": p["prompt"]},
        {"role": "assistant", "content": answer},
        {
            "role": "user",
            "content": (
                "Rate your confidence in your previous answer from 0.0 "
                "to 1.0.  Reply with only the number."
            ),
        },
    ]
    conf_str, stats_c = _cached_chat(
        cache, "cheap", pid, probe, temperature=0, tag="conf"
    )
    confidence = _parse_confidence(conf_str)

    stats_used = [stats0, stats_c]
    tok = sum(_tokens(s) for s in stats_used)
    lat = (stats0.get("latency_ms") or 0) + (stats_c.get("latency_ms") or 0)
    escalated = False

    if confidence < threshold:
        escalated = True
        strong_ans, strong_s = _cached_chat(cache, "strong", pid, msgs, temperature=0)
        answer = strong_ans
        stats_used.append(strong_s)
        tok += _tokens(strong_s)
        lat += strong_s.get("latency_ms") or 0

    return {
        "answer": answer,
        "correct": score(answer, p["gold"], p["check"]),
        "escalated": escalated,
        "confidence": confidence,
        "tokens": tok,
        "cost_units": _total_cost(stats_used),
        "latency_ms": lat,
    }


def _eval_sc(
    cache: dict[str, Any], p: dict[str, Any], threshold: float
) -> dict[str, Any]:
    pid = p["id"]
    msgs: list[dict[str, str]] = [{"role": "user", "content": p["prompt"]}]

    answers: list[str] = []
    stats_list: list[dict[str, Any]] = []
    for k in range(SC_K):
        a, s = _cached_chat(
            cache, "cheap", pid, msgs, temperature=SC_TEMP, attempt=k
        )
        answers.append(a)
        stats_list.append(s)

    normed = [" ".join(a.casefold().split()) for a in answers]
    ctr = Counter(normed)
    majority_norm, majority_count = ctr.most_common(1)[0]
    agreement = majority_count / len(answers)
    majority_answer = answers[normed.index(majority_norm)]

    stats_used = list(stats_list)
    tok = sum(_tokens(s) for s in stats_used)
    lat = sum(s.get("latency_ms") or 0 for s in stats_list)
    escalated = False

    if agreement < threshold:
        escalated = True
        strong_ans, strong_s = _cached_chat(cache, "strong", pid, msgs, temperature=0)
        majority_answer = strong_ans
        stats_used.append(strong_s)
        tok += _tokens(strong_s)
        lat += strong_s.get("latency_ms") or 0

    return {
        "answer": majority_answer,
        "correct": score(majority_answer, p["gold"], p["check"]),
        "escalated": escalated,
        "confidence": agreement,
        "tokens": tok,
        "cost_units": _total_cost(stats_used),
        "latency_ms": lat,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Baselines
# ═══════════════════════════════════════════════════════════════════════════


def _baseline(
    cache: dict[str, Any], prompts: list[dict[str, Any]], tier: str
) -> dict[str, Any]:
    correct = 0
    total_tok = 0
    cost_values: list[float | int | None] = []
    total_lat = 0.0
    band_c: dict[str, int] = {}
    band_n: dict[str, int] = {}
    details: list[dict[str, Any]] = []

    for p in prompts:
        msgs: list[dict[str, str]] = [{"role": "user", "content": p["prompt"]}]
        ans, stats = _cached_chat(cache, tier, p["id"], msgs, temperature=0)
        ok = score(ans, p["gold"], p["check"])
        if ok:
            correct += 1
        total_tok += _tokens(stats)
        cost_values.append(stats.get("cost_units"))
        total_lat += stats.get("latency_ms") or 0
        b = p["band"]
        band_n[b] = band_n.get(b, 0) + 1
        band_c[b] = band_c.get(b, 0) + (1 if ok else 0)
        details.append({"id": p["id"], "band": b, "correct": ok, "answer": ans})

    n = len(prompts)
    return {
        "tier": tier,
        "accuracy": round(correct / n, 4) if n else 0,
        "total_cost_units": None if any(v is None for v in cost_values) else sum(cost_values),
        "total_tokens": total_tok,
        "avg_latency_ms": round(total_lat / n, 2) if n else 0,
        "band_accuracy": {
            b: round(band_c.get(b, 0) / band_n[b], 4) for b in sorted(band_n)
        },
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
            tok = 0
            cost_values: list[float | int | None] = []
            lat = 0.0
            band_c: dict[str, int] = {}
            band_n: dict[str, int] = {}

            for p in prompts:
                r = _eval_sc(cache, p, th) if sc else _eval_no_sc(cache, p, th)
                if r["correct"]:
                    correct += 1
                if r["escalated"]:
                    esc += 1
                tok += r["tokens"]
                cost_values.append(r["cost_units"])
                lat += r["latency_ms"]
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
                    "total_tokens": tok,
                    "avg_latency_ms": round(lat / n, 2),
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
    for r in results:
        dominated = any(
            o["accuracy"] >= r["accuracy"]
            and _resource(o) <= _resource(r)
            and (
                o["accuracy"] > r["accuracy"]
                or _resource(o) < _resource(r)
            )
            for o in results
            if o is not r
        )
        if not dominated:
            optimal.append(r)
    return sorted(optimal, key=_resource)


def _pick_op(results: list[dict[str, Any]], strong_acc: float) -> dict[str, Any]:
    cutoff = strong_acc - 0.02
    eligible = [r for r in results if r["accuracy"] >= cutoff]
    if not eligible:
        eligible = sorted(results, key=lambda r: -r["accuracy"])
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
    tok = 0
    cost_values: list[float | int | None] = []
    lat = 0.0
    band_c: dict[str, int] = {}
    band_n: dict[str, int] = {}
    details: list[dict[str, Any]] = []

    for p in prompts:
        r = _eval_sc(cache, p, threshold) if sc else _eval_no_sc(cache, p, threshold)
        if r["correct"]:
            correct += 1
        if r["escalated"]:
            esc += 1
        tok += r["tokens"]
        cost_values.append(r["cost_units"])
        lat += r["latency_ms"]
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
            }
        )

    n = len(prompts)
    return {
        "accuracy": round(correct / n, 4),
        "escalation_rate": round(esc / n, 4),
        "total_cost_units": None if any(v is None for v in cost_values) else sum(cost_values),
        "total_tokens": tok,
        "avg_latency_ms": round(lat / n, 2),
        "band_accuracy": {
            b: round(band_c.get(b, 0) / band_n[b], 4) for b in sorted(band_n)
        },
        "details": details,
    }


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
        pts = [r for r in sweep_results if r["self_consistency"] == sc_val]
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
    hdr = ("SC", "THRESH", "ACC", "ESC%", "COST_UNITS", "TOKENS", "LAT_MS", "P")
    rows: list[tuple[str, ...]] = [hdr]
    for r in results:
        star = "*" if (r["self_consistency"], r["threshold"]) in pset else ""
        rows.append(
            (
                str(r["self_consistency"]),
                f"{r['threshold']:.2f}",
                f"{r['accuracy']:.1%}",
                f"{r['escalation_rate']:.1%}",
                "unknown" if r["total_cost_units"] is None else str(r["total_cost_units"]),
                str(r["total_tokens"]),
                f"{r['avg_latency_ms']:.1f}",
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
    h = ("", "Router", "Cheap-only", "Strong-only")
    r1 = ("Accuracy", f"{ho['accuracy']:.1%}", f"{cheap['accuracy']:.1%}", f"{strong['accuracy']:.1%}")
    r2 = ("Escalation %", f"{ho['escalation_rate']:.1%}", "0.0%", "100.0%")
    r3 = ("Cost units", *("unknown" if x["total_cost_units"] is None else str(x["total_cost_units"]) for x in (ho, cheap, strong)))
    r4 = ("Tokens", *(str(x["total_tokens"]) for x in (ho, cheap, strong)))
    r5 = ("Avg latency ms", f"{ho['avg_latency_ms']:.1f}", f"{cheap['avg_latency_ms']:.1f}", f"{strong['avg_latency_ms']:.1f}")
    table = [h, r1, r2, r3, r4, r5]
    ws = [max(len(row[c]) for row in table) for c in range(4)]
    for i, row in enumerate(table):
        print("  " + " | ".join(f"{v:>{ws[c]}}" for c, v in enumerate(row)))
        if i == 0:
            print("  " + "-+-".join("-" * w for w in ws))

    # Per-band breakdown
    print("\n  Per-band accuracy (holdout):")
    print(f"  {'Band':<8} {'Router':>8} {'Cheap':>8} {'Strong':>8}")
    print(f"  {'-'*8} {'-'*8} {'-'*8} {'-'*8}")
    for band in sorted(ho["band_accuracy"]):
        ra = ho["band_accuracy"].get(band, 0)
        ca = cheap["band_accuracy"].get(band, 0)
        sa = strong["band_accuracy"].get(band, 0)
        print(f"  {band:<8} {ra:>7.1%} {ca:>7.1%} {sa:>7.1%}")

    # Worst mistakes
    mistakes = [d for d in ho.get("details", []) if not d["correct"]]
    if mistakes:
        print(f"\n  5 worst holdout mistakes:")
        for d in mistakes[:5]:
            ans_short = (d["answer"] or "")[:60].replace("\n", " ")
            print(
                f"    {d['id']} ({d['band']}) esc={d['escalated']} "
                f"conf={d['confidence']:.2f}  ans={ans_short!r}"
            )


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════


def main() -> None:
    for path, name in [(TUNE_PATH, "tune.jsonl"), (HOLDOUT_PATH, "holdout.jsonl")]:
        if not path.exists():
            print(f"{name} not found. Run:  python scripts/split_data.py")
            sys.exit(1)

    tune = _load_jsonl(TUNE_PATH)
    holdout = _load_jsonl(HOLDOUT_PATH)
    # Refuse to start if a tier is not configured. In particular, this avoids
    # creating partial result/cache artifacts that could look like measurements.
    if CLIENT_ERROR:
        print(f"Cannot run sweep: {CLIENT_ERROR}")
        sys.exit(2)
    missing = [
        f"{name}.{field}"
        for name, cfg in client._TIERS.items()
        for field in ("model_id", "base_url")
        if not cfg.get(field)
    ]
    if missing:
        print("Cannot run sweep: configure " + ", ".join(missing) + " in .env/config.yaml first.")
        sys.exit(2)
    cache = _load_cache()
    print(f"Tune: {len(tune)}  Holdout: {len(holdout)}  Cache: {len(cache)} entries\n")

    # ── Phase 1: pre-cache tune ──────────────────────────────────────────
    print("== Phase 1: pre-cache tune answers ==")
    _precache(cache, tune, "tune")

    # ── Phase 2: baselines on tune ───────────────────────────────────────
    print("\n== Phase 2: tune baselines ==")
    cheap_tune = _baseline(cache, tune, "cheap")
    strong_tune = _baseline(cache, tune, "strong")
    print(f"  cheap-only:  acc={cheap_tune['accuracy']:.1%}  cost_units={cheap_tune['total_cost_units']}  tokens={cheap_tune['total_tokens']}  lat={cheap_tune['avg_latency_ms']:.1f}ms")
    print(f"  strong-only: acc={strong_tune['accuracy']:.1%}  cost_units={strong_tune['total_cost_units']}  tokens={strong_tune['total_tokens']}  lat={strong_tune['avg_latency_ms']:.1f}ms")
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
        f"cutoff≥{strong_tune['accuracy'] - 0.02:.1%})"
    )

    # ── Phase 5: pre-cache holdout + evaluate ────────────────────────────
    print("\n== Phase 5: holdout evaluation ==")
    _precache(cache, holdout, "holdout")

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
