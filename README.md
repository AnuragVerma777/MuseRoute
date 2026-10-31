# museroute

A Python 3.11 project for routing requests between model tiers.

## Setup

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Fill in the endpoint and model settings in `.env`, then configure `museroute/config.yaml`.

## Layout

- `museroute/`: application package
- `scripts/`: helper scripts
- `data/`: input data
- `results/`: generated outputs
- `tests/`: tests
