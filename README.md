# MuseRoute

MuseRoute is a small Python 3.11 service and evaluation harness that routes chat requests through a cheaper Muse tier first and escalates to a stronger tier when the answer is malformed, refused, or below a confidence threshold. It also provides a standard chat-completions HTTP endpoint, a direct route trace, and a comparison endpoint for strong-only versus routed calls.

## Muse models and tier roles

Both configured tiers currently use the exact model ID `meta/muse-glimmer-30b` on NVIDIA's OpenAI-compatible Integrate endpoint (`https://integrate.api.nvidia.com/v1`). They are two modes of the same model, not two different models:

- **cheap** sends `reasoning_effort: low` and is tried first for each routed request.
- **strong** sends `reasoning_effort: high` when MuseRoute escalates; it is also the strong-only comparison tier.

The model ID, URL, and API key are read separately from `CHEAP_*` and `STRONG_*` variables in `.env`. The client rejects configured model IDs without `muse` (case-insensitive) and blocks the paid Meta API host `api.meta.ai`. Both `cost_weight` values are currently null.

## Setup

Use Python 3.11 from the project root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Fill in the tier-specific base URLs, model IDs, and keys in `.env`. `.env` is ignored by Git. Do not commit it. Set `cost_weight` only from a trusted model card or measured relative compute data; do not treat it as a dollar price.

## Commands

```powershell
# Run request checks and the independent tier baseline
python scripts/smoke.py
python scripts/baseline.py

# Recreate the fixed-seed (42) stratified split, then tune on tune.jsonl
python scripts/split_data.py
python scripts/sweep.py

# After a complete sweep cache exists, replay without network calls
python scripts/sweep.py --reproduce

# Run the local API
uvicorn museroute.server:app --host 127.0.0.1 --port 8000

# Run tests
pytest -q
```

The split has 45 tune prompts and 45 held-out prompts (15 easy, 15 medium, 15 hard). `sweep.py` selects its operating point from tune data only, then evaluates that single setting on holdout. The API reads the selected point from `results/sweep.json` when present; `MUSEROUTE_THRESHOLD` and `MUSEROUTE_SELF_CONSISTENCY` can override it.

## Local API

- `GET /health` checks configured model policy.
- `POST /v1/chat/completions` returns chat-completions JSON, usage counts, and a compact `route_trace`.
- `POST /api/route` returns the answer and full trace.
- `POST /api/compare` runs strong-only and MuseRoute on the same messages.
- `GET /api/facts` serves the generated `results/facts.json` measurement summary.

Example:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"museroute","messages":[{"role":"user","content":"Which number is larger, 9.11 or 9.8?"}]}'
```

Fetch the measured summary:

```bash
curl http://127.0.0.1:8000/api/facts
```

Generate or refresh it from the saved evaluation artifacts with `python scripts/make_facts.py`.

## Results — 45 held-out prompts

`results/facts.json` is generated from the saved baseline, sweep and split artifacts. It reports measured accuracy and per-band results for cheap-only, strong-only and MuseRoute, prompt counts, token/latency measurements, the selected tune threshold, and routed holdout mistakes. Run `python scripts/make_facts.py` to refresh the summary; `GET /api/facts` serves the same JSON.

There are 45 holdout prompts, stratified across easy, medium and hard. The recorded overall accuracies are 60.0% cheap-only, 57.8% strong-only, and 48.9% MuseRoute. Six routed holdout prompts have no returned answer after request failures; those failures are listed separately from the answered mistakes. Per-band accuracy, escalation, token, latency and cost fields are in `results/facts.json`; values absent from the source records remain JSON `null` and should be displayed as **NOT MEASURED**. Both tier cost weights are null, so cost units are not measured. **Cost units are a relative estimate, not dollars.**

The facts artifact contains up to five deterministic examples of incorrect answered holdout prompts, ordered hard then medium then easy, with the check failure explained. It does not mislabel failed requests as model mistakes. The accuracy-versus-cost chart is at `results/pareto.png`.

```bash
python scripts/make_facts.py
```

## Serve Glimmer from a free GPU notebook

`notebooks/serve_glimmer.py` can load `meta-models/Muse-Glimmer-30B` on a Kaggle or Colab GPU session, start a compatible server, and print a temporary Cloudflare Quick Tunnel URL. Set the Hugging Face token as `HF_TOKEN` in the notebook environment and enable Internet/GPU access. Run:

```bash
python notebooks/serve_glimmer.py
```

The tunnel URL changes each session. Use it as the relevant `CHEAP_BASE_URL` or `STRONG_BASE_URL` in `.env`; the model ID remains `meta/muse-glimmer-30b` for the NVIDIA hosted endpoint or the appropriate Muse model ID for a local server.
