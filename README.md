# MuseRoute

MuseRoute is a Python 3.11 project for making direct chat-completions requests through selectable Muse tiers, checking replies against a small workload, and comparing each tier's quality, latency, token use, and available cost data. Its policy restricts model IDs to case-insensitive substring matches for `muse-spark-open` or `muse-glimmer`, blocks `api.meta.ai`, and requires free use.

## Muse models and tiers

The policy's allowed model-name fragments are exactly `muse-spark-open` and `muse-glimmer`. The free path uses the verified Hugging Face model ID `meta-models/Muse-Glimmer-30B`. Both tiers point to the same locally hosted model: `cheap` requests low reasoning strength, while `strong` requests high reasoning strength. This compares effort settings without paying for another hosted model.

- **cheap** is the intended lower-cost tier for routine or simpler prompts. Its `model_id`, `base_url`, and `cost_weight` are TODOs.
- **strong** is the intended higher-capability tier for harder prompts. Its `model_id`, `base_url`, and `cost_weight` are TODOs.

Muse Spark Open through OpenRouter is paid, so it is blocked by the free-only policy. `require_free` permits only local addresses and Cloudflare Quick Tunnel URLs; paid provider hosts such as OpenRouter and Meta's paid API are rejected at startup. The baseline runner calls each tier independently with the same workload; it does not route prompts between tiers.

## Setup

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Fill in the endpoint and model ID for each tier in `museroute/config.yaml`. Put any required key values in `.env`, and set each tier's `api_key_env` to its variable name. Leave it empty for a self-hosted endpoint that requires no key. The `.env` file is ignored by Git. Populate `cost_weight` from the relevant model card; no USD prices are currently configured.

## Run the checks

From the repository root, with the virtual environment active and tier settings/keys configured:

```powershell
python scripts/smoke.py
python scripts/baseline.py
```

`smoke.py` checks plain text, JSON output, token usage, payment errors, and configured `extra_params`. For the strong tier, it only tries reasoning parameter names that it can identify from verified host documentation.

`baseline.py` runs every prompt in `data/workload.jsonl` against each tier with temperature 0 where supported. It writes resumable per-prompt results to `results/baseline.json`, saves after each item, and skips completed entries on a later run. Press Ctrl-C to stop safely; rerun the command to resume.

## Baseline results — 2026-10-31

The baseline command produced 180 records, but no model requests reached an endpoint. The available Python environment does not have `requests`, and both configured tier URLs and model IDs are still null. Accuracy and latency are therefore unavailable; USD cost is unknown because no prices are configured.

| Tier | Band | Accuracy | Average latency | Total tokens | Total cost (USD) |
|---|---|---:|---:|---:|---:|
| cheap | all | N/A (0/90 complete) | N/A | 0 | unknown |
| cheap | easy | N/A (0/30 complete) | N/A | 0 | unknown |
| cheap | medium | N/A (0/30 complete) | N/A | 0 | unknown |
| cheap | hard | N/A (0/30 complete) | N/A | 0 | unknown |
| strong | all | N/A (0/90 complete) | N/A | 0 | unknown |
| strong | easy | N/A (0/30 complete) | N/A | 0 | unknown |
| strong | medium | N/A (0/30 complete) | N/A | 0 | unknown |
| strong | hard | N/A (0/30 complete) | N/A | 0 | unknown |

## Holdout routing results

**No valid holdout evaluation has been recorded yet.** The saved baseline file contains 180 error records, and the free endpoint URL/model IDs are not configured in this checkout. The table therefore reports unavailable values rather than treating failed requests as model answers. In particular, there is not enough evidence to say where MuseRoute saves cost or to identify actual holdout mistakes.

| System | Accuracy | Escalation | Cost units | Average latency |
|---|---:|---:|---:|---:|
| cheap-only | N/A | 0% (reference) | N/A | N/A |
| strong-only | N/A | 100% (reference) | N/A | N/A |
| MuseRoute (selected tune setting) | N/A | N/A | N/A | N/A |

Per-band holdout accuracy is also unavailable until successful calls complete:

| Band | cheap-only | strong-only | MuseRoute |
|---|---:|---:|---:|
| Easy | N/A | N/A | N/A |
| Medium | N/A | N/A | N/A |
| Hard | N/A | N/A | N/A |

**Cost units are a relative estimate, not dollars.** They are token counts multiplied by the configured per-tier `cost_weight`. Both weights are currently `null`, so reported cost units and dollar costs are unknown; sweep selection then uses total token count as an explicitly labeled fallback. No measured cost savings or failure patterns can be claimed yet.

### Worst holdout mistakes

There are no valid holdout answers to rank. The current 180 baseline rows all contain request errors, so they are not counted as mistakes and are excluded from accuracy. After a successful run, this section should be replaced with the five incorrect MuseRoute holdout answers, their prompt IDs/bands, and a short explanation based on each gold answer and model output.

### Reproduce the split, sweep, holdout comparison, and chart

Configure the free local or Quick Tunnel endpoint and the two model IDs in `.env`/`museroute/config.yaml` first. From the repository root, with Python 3.11 and the virtual environment active, run these exact commands:

```powershell
python scripts/split_data.py
python scripts/sweep.py
```

The split uses fixed seed 42. `sweep.py` tunes thresholds and self-consistency on `data/tune.jsonl` only, chooses one operating point, evaluates it and the cheap-only/strong-only references on `data/holdout.jsonl`, then writes `results/sweep.json`. It also generates `results/pareto.png` from the measured tune results. **Current chart status:** no measured points; the image is intentionally annotated until a successful evaluation exists.

![Accuracy versus relative cost units; no evaluation points recorded yet](results/pareto.png)

## Serve Muse Glimmer from a free GPU notebook

In a Kaggle or Colab session, enable a GPU and set `HF_TOKEN` to a Hugging Face read token using the platform's secrets/environment UI. The verified repo is `meta-models/Muse-Glimmer-30B`; `.env.example` already sets `GLIMMER_REPO` to that ID. The model is Apache-2.0 open weights. Its model card describes a 4-bit size under 20 GB and a 24–32 GB deployment envelope, making a dual-T4 session a plausible target; the actual free runtime must still be checked for memory fit. The script installs the model's current Transformers implementation, logs in without saving the token as a Git credential, and loads across the visible GPUs with a balanced device map.

Run from the project root in the notebook terminal/cell:

```bash
python notebooks/serve_glimmer.py
```

The script prints GPU memory before and after model loading. If loading or generation runs out of memory, it prints the exact exception and per-GPU memory figures. On success it starts `POST /v1/chat/completions`, downloads `cloudflared` if needed, and opens a temporary free Quick Tunnel. Copy the printed `GLIMMER_BASE_URL` and generated `GLIMMER_API_KEY` lines into your local, Git-ignored `.env`. The base URL includes `/v1`; the key protects the public tunnel. Set the same repo ID in the notebook session and local `.env`, then MuseRoute uses low reasoning for `cheap` and high reasoning for `strong` against this self-hosted API. This is a self-hosted API on a free GPU session, not a guaranteed free third-party inference service; free GPU availability and session quotas depend on Kaggle/Colab.

Quick Tunnel URLs are temporary and stop working when the notebook process stops. The model's exact repository and access requirements must be checked on its [Hugging Face model page](https://huggingface.co/). The loader uses Hugging Face's [4-bit bitsandbytes and balanced device map](https://huggingface.co/docs/transformers/quantization/bitsandbytes); Cloudflare documents the temporary [Quick Tunnel](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/).

## Project layout

- `museroute/`: application package and model-tier configuration
- `scripts/`: smoke and baseline runners
- `data/`: workload prompts
- `results/`: generated JSON results
- `tests/`: checker and client tests
- `notebooks/`: exploratory notebooks
