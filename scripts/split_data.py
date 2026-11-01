"""Split workload.jsonl into stratified tune (45) and holdout (45) sets."""

from __future__ import annotations

import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED = 42


def split() -> None:
    with (ROOT / "data" / "workload.jsonl").open(encoding="utf-8") as f:
        prompts = [json.loads(line) for line in f if line.strip()]

    by_band: dict[str, list[dict]] = {}
    for p in prompts:
        by_band.setdefault(p["band"], []).append(p)

    rng = random.Random(SEED)
    tune: list[dict] = []
    holdout: list[dict] = []
    for band in sorted(by_band):
        items = list(by_band[band])
        rng.shuffle(items)
        half = len(items) // 2
        tune.extend(items[:half])
        holdout.extend(items[half:])

    for path, data in [
        (ROOT / "data" / "tune.jsonl", tune),
        (ROOT / "data" / "holdout.jsonl", holdout),
    ]:
        with path.open("w", encoding="utf-8") as f:
            for item in data:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        bands = {}
        for p in data:
            bands[p["band"]] = bands.get(p["band"], 0) + 1
        print(f"Wrote {len(data)} prompts to {path.name}  {bands}")


if __name__ == "__main__":
    split()
