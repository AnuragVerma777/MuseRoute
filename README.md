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

All results below come from [results/facts.json](results/facts.json). The set contains 45 held-out prompts (15 per band). A JSON `null` means **NOT MEASURED**.

| System | Accuracy | Escalation | Average total tokens/request | Average active request time (ms) | Isolated model time (ms) | Retry count / sleep | Cost units |
|---|---:|---:|---:|---:|---:|---:|---:|
| Cheap-only | 66.67% | 0% | 249.93 | NOT MEASURED | NOT MEASURED | NOT MEASURED | NOT MEASURED |
| Strong-only | 66.67% | 100% | 537.33 | NOT MEASURED | NOT MEASURED | NOT MEASURED | NOT MEASURED |
| MuseRoute | 75.56% | 20% | NOT MEASURED | NOT MEASURED | NOT MEASURED | NOT MEASURED | NOT MEASURED |

Per-band results:

| Band (15 prompts each) | System | Accuracy | Escalation | Average total tokens/request | Active request time (ms) |
|---|---|---:|---:|---:|---:|
| Easy | Cheap-only | 100% | 0% | 177.53 | NOT MEASURED |
| Easy | Strong-only | 100% | 100% | 340.60 | NOT MEASURED |
| Easy | MuseRoute | 60% | 60% | NOT MEASURED | NOT MEASURED |
| Medium | Cheap-only | 80% | 0% | 372.00 | NOT MEASURED |
| Medium | Strong-only | 73.33% | 100% | 931.87 | NOT MEASURED |
| Medium | MuseRoute | 73.33% | 0% | 392.80 | NOT MEASURED |
| Hard | Cheap-only | 20% | 0% | 200.27 | NOT MEASURED |
| Hard | Strong-only | 26.67% | 100% | 339.53 | NOT MEASURED |
| Hard | MuseRoute | 93.33% | 0% | 202.67 | NOT MEASURED |

**Exploratory, post-hoc holdout rescoring:** the numeric checker previously rejected worked answers containing intermediate numbers even when their final equation or explicit remainder equaled the gold answer. The checker was corrected and the saved 45 holdout responses were rescored offline. This post-hoc holdout accuracy is exploratory and must not be treated as an untouched estimate of generalization. The threshold (`0.5`, self-consistency off) was selected using tune data only; no holdout result was used to choose the policy.

The prior `452,626.58 ms` mean mixed active HTTP time with retry waits and was not a valid isolated model-time figure. New client telemetry measures active HTTP request time across attempts and records retry count and sleep time separately. The host does not expose isolated model-compute time, so `model_time_ms` remains **NOT MEASURED**. The saved answer cache predates this split; replaying it cannot recover active time, retry counts, or sleeps, so those averages are now null rather than guessed. Per-call token usage is retained in the route trace. Since failed calls have unknown usage, the complete MuseRoute token total is **NOT MEASURED**; known successful-call usage is 10,962 tokens across the cached trace calls. Cost units are a relative estimate, not dollars; no cost weights are configured, so cost is **NOT MEASURED**.

The operating point remains a tune-only choice. Cheap and strong currently use the same model ID and host, with different reasoning modes.

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
