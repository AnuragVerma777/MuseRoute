# museroute

A Python 3.11 project for routing requests across Muse model tiers.

## Setup

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set each tier's endpoint and model ID in `museroute/config.yaml` from verified deployment details. Set `api_key_env` to the corresponding variable name in `.env`, or leave it empty for a self-hosted endpoint that needs no key. Fill `cost_weight` from the model card before using cost-aware routing.

## Policy

The policy allows model IDs containing `muse-spark-open` or `muse-glimmer`, using case-insensitive substring matching. Requests to `api.meta.ai` are blocked, and `require_free` is enabled.

## Project layout

- `museroute/`: application package and configuration
- `scripts/`: helper scripts
- `data/`: input data
- `results/`: generated outputs
- `tests/`: tests
- `notebooks/`: exploratory notebooks
