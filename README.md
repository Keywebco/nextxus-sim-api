# NextXus Sim API

The Phase 2 backend for the NextXus SIM — a FastAPI service that powers AI simulation conversations. It serves the `/messages` and `/braid` endpoints, calling MiMo first and DeepSeek as the fallback, both over OpenAI-compatible endpoints. The Next-Xus Federation stack is GitHub, Google Drive, the Federation repositories, and the Immutable HumanCodex. Catalyst and Pontus load the HumanCodex core rulebook (`knowledge/HUMANCODEX-CORE.md`, refreshed at startup from `Keywebco/sovereign-knowledge-os`) into their system prompts. It deploys to Render via Docker.

**Live:** https://keywebco.github.io/nextxus-sim-api/ (the service itself deploys to Render — see `render.yaml`; the SIM front end talks to it)

## What's here

- `main.py` — The FastAPI service: `/messages` and `/braid` endpoints, CORS enabled.
- `requirements.txt` — fastapi, uvicorn, httpx, pydantic.
- `Dockerfile` / `render.yaml` — container build and Render deployment config (free plan, `MIMO_API_KEY` and `DEEPSEEK_API_KEY` environment variables).

Part of the [NextXus Federation](https://keywebco.github.io).
