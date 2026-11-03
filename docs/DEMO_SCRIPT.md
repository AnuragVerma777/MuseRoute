# MuseRoute demo script — 3 minutes

Start the server with `uvicorn museroute.server:app --host 127.0.0.1 --port 8000`, then open `http://127.0.0.1:8000/`. For a cache-only presentation, open `http://127.0.0.1:8000/?demo=1`. Demo mode uses saved tier answers and holdout decisions only; it does not call models. The saved cache did not retain confidence values, so the UI displays **NOT MEASURED** for confidence.

| Time | Scene | Narration | On-screen action | UI step |
|---|---|---|---|---|
| 0:00–0:30 | Problem | “Hackathon teams evaluating Meta Muse need to know when a cheaper first answer is enough and when a request should escalate.” | Show the one-sentence problem at the top of the page and the `cheap` → `strong` timeline structure. | Open `/`; point to the problem statement and the three difficulty groups of examples. |
| 0:30–1:00 | Evidence | “The saved workload has 90 prompts: 45 for tuning and 45 held out, with 15 holdout prompts in each band. Both tiers used `meta/muse-glimmer-30b` at `integrate.api.nvidia.com`.” | Point to the prompt controls and the saved source facts in `results/facts.json`. | Choose an easy preset and press **Route prompt**; show the cached badge and saved decision. |
| 1:00–1:25 | Cost | “The holdout escalation rate is 20%. That is not a cost measurement: cost units and MuseRoute total tokens are null, so they are shown as NOT MEASURED.” | Point to the cost banner and the comparison metric cards. | Press **Compare strong-only vs MuseRoute** and show the unavailable cost fields. |
| 1:25–2:00 | Available solutions | “The alternatives considered are OpenRouter Auto Router, Not Diamond, LiteLLM Router, and RouteLLM. I don’t have the source descriptions for them here, so I’m making no feature or performance claims.” | Show the alternatives table in README and its source status. | Keep the demo on the page; do not imply these systems were benchmarked. |
| 2:00–2:35 | Before and after | “The saved strong-only holdout accuracy is 57.78%, while the selected MuseRoute setting scored 48.89%. Routing did not improve accuracy on this holdout.” | Show the comparison cards, then refer to the overall accuracy table in README. | Pick a different preset and compare again; in demo mode all values come from saved data. |
| 2:35–3:00 | Impact and limits | “The evaluation shows a working Muse-only routing experiment, but MuseRoute underperformed both single tiers overall. Six routed prompts had no returned answer, and this data does not establish savings or broader generalization.” | Show the saved route decision, final answer and the null cost/token fields. | Point to the banner and state that live mode may fail if network access is blocked. |

## 30-second fallback

Open `http://127.0.0.1:8000/?demo=1`, choose any of the six cache-backed examples and press **Route prompt**, then press **Compare strong-only vs MuseRoute**. Narrate: “This replays cached tier answers and a saved holdout decision without a network call. Accuracy on the 45-prompt holdout was 60.00% cheap-only, 57.78% strong-only and 48.89% MuseRoute; cost units are NOT MEASURED.” If the requested text is not one of the cached prompts, demo mode shows a cache-miss message and never falls through to live calls.

Demo-mode latency values for tier cards are the recorded cached tier-answer latencies; route latency is from the saved holdout result. They are clearly marked cached and should not be described as fresh live timings. Confidence was not saved and is displayed as **NOT MEASURED**.
