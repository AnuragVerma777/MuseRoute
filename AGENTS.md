# Rules for every future task

1. Only Meta Muse models (Muse Spark Open, Muse Glimmer) may be called. No other vendor SDK, model or name in code. Groq or OpenRouter are allowed only as hosts. Block `api.meta.ai`.
2. No Streamlit, Gradio, or a dashboard as the main feature. The demo UI is plain HTML/CSS/JS served by FastAPI.
3. Never invent numbers. Any value not measured is `null` and shows as `NOT MEASURED`.
4. Run `pytest -q` before finishing every task and show the result. Commit small, clear commits.
5. Never print, log or commit API keys. `.env` stays ignored.
6. Before adding code, inspect the repo and reuse what exists. Do not add dependencies without saying so.
