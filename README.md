# MuseRoute

## Problem and target user

Teams evaluating Meta Muse models need to understand whether a cheaper first attempt can answer a request well enough, and when a stronger attempt should be used. MuseRoute is for hackathon teams and developers testing that question with measurable, repeatable prompts.

## Unique value

MuseRoute is a Muse-only, confidence-gated routing experiment that records its decisions and evaluates them on prompts held out from tuning.

## Key features

- **Muse-only model policy and two configured tiers:** both tiers currently use `meta/muse-glimmer-30b` on `integrate.api.nvidia.com`; [measured model and host facts](results/facts.json).
- **Cheap-first routing with a format and confidence guard:** see the [router implementation](museroute/router.py) and measured [holdout route results](results/facts.json).
- **Tune/holdout evaluation:** 45 prompts were held out from the 45-prompt tune set; inspect [all recorded facts](results/facts.json).
- **API endpoints for routing, comparison, health and facts:** see [the FastAPI service](museroute/server.py).
- **Presentation material:** [3-minute demo script](docs/DEMO_SCRIPT.md) and [judge Q&A](docs/JUDGE_QA.md).

## Existing alternatives

The four source lines referenced for this table were not included in the request (the message contains “paste the four lines from my earlier message”). To avoid unsourced descriptions or comparisons, the factual descriptions are pending those lines.

| Alternative | Factual line from supplied source |
|---|---|
| OpenRouter Auto Router | Source line not supplied. |
| Not Diamond | Source line not supplied. |
| LiteLLM Router | Source line not supplied. |
| RouteLLM | Source line not supplied. |

MuseRoute makes no claim that it beats these alternatives.

## Why MuseRoute is different

- **Muse-only:** the saved facts identify `meta/muse-glimmer-30b` for both tiers, and the project enforces a Muse model policy.
- **Accuracy guard:** the router checks answer format and self-reported confidence before deciding whether to escalate. Its measured accuracy is reported below; this guard did not improve the recorded holdout accuracy over either single tier.
- **Held-out evaluation:** the reported comparison uses 45 holdout prompts separate from the 45 tune prompts.
- **No card:** payment-card requirements are not recorded in `results/facts.json`; **SAY: not measured**.

## Results

All results below come from [results/facts.json](results/facts.json). Accuracy is measured on 45 holdout prompts. A JSON `null` means **NOT MEASURED**.

| System | Accuracy | Escalation | Average total tokens/request | Average latency (ms) | Cost units |
|---|---:|---:|---:|---:|---:|
| Cheap-only | 60.00% | 0% | 249.93 | 12,947.69 | NOT MEASURED |
| Strong-only | 57.78% | 100% | 537.33 | 26,021.18 | NOT MEASURED |
| MuseRoute | 48.89% | 20% | NOT MEASURED | 452,626.58 | NOT MEASURED |

Per-band accuracy:

| Band (15 prompts each) | Cheap-only | Strong-only | MuseRoute |
|---|---:|---:|---:|
| Easy | 100% | 100% | 60% |
| Medium | 80% | 73.33% | 73.33% |
| Hard | 0% | 0% | 13.33% |

The saved operating point uses threshold `0.5` with self-consistency off. The configured tiers currently use the same model ID and host. The MuseRoute holdout total-token count and cost units are null in the source facts, so this evaluation cannot establish a cost saving. Six routed prompts had no returned answer; the source facts record 15 tier-call errors across those requests and fallbacks.

## What was built during the hackathon

Commit dates and subjects from Git history:

| Date | Recorded work |
|---|---|
| 2026-10-31 | Project scaffold, Muse client/policy, invented workload and checks, resumable baseline runner. |
| 2026-11-01 | Muse endpoint configuration, host-agnostic tier setup, payment-error handling. |
| 2026-11-02 | Cached router, baseline and sweep work, held-out evaluation, FastAPI server. |
| 2026-11-03 | Repository rules, completed baseline and holdout results, facts generator and facts API. |

## Setup

Use Python 3.11 from the repository root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set `CHEAP_BASE_URL`, `CHEAP_MODEL_ID`, `CHEAP_API_KEY`, `STRONG_BASE_URL`, `STRONG_MODEL_ID`, and `STRONG_API_KEY` in the local `.env`. The checked-in facts show the host and model ID used for the recorded measurements; credentials are not part of the facts artifact.

```powershell
pytest -q
python scripts/smoke.py
python scripts/baseline.py
python scripts/split_data.py
python scripts/sweep.py
python scripts/make_facts.py
uvicorn museroute.server:app --host 127.0.0.1 --port 8000
```

## Architecture

`museroute/client.py` sends chat-completions HTTP requests. `museroute/router.py` tries the cheap tier, applies its format/confidence checks, and may escalate. `museroute/server.py` exposes `/health`, `/v1/chat/completions`, `/api/route`, `/api/compare`, and `/api/facts`. Workload, tune and holdout inputs are under `data/`; measured outputs and caches are under `results/`.

The plain HTML/CSS/JavaScript demo is served at `/`. Open `http://127.0.0.1:8000/?demo=1` for cache-only examples; that mode does not make model calls.

## API example

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"museroute","messages":[{"role":"user","content":"Which number is larger, 9.11 or 9.8?"}]}'
```

Retrieve the saved evaluation facts with `curl http://127.0.0.1:8000/api/facts`.
