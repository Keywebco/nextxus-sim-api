# NextXus Sim API

The Phase 2 backend for the NextXus SIM — a FastAPI service that powers AI simulation conversations. It serves the `/messages` and `/braid` endpoints, routing through the Emergent universal LLM key over an OpenAI-compatible endpoint. It deploys to Render via Docker.

**Live:** https://keywebco.github.io/nextxus-sim-api/ (the service itself deploys to Render — see `render.yaml`; the SIM front end talks to it)

## What's here

- `main.py` — The FastAPI service: `/messages` and `/braid` endpoints, CORS enabled.
- `requirements.txt` — fastapi, uvicorn, httpx, pydantic.
- `Dockerfile` / `render.yaml` — container build and Render deployment config (free plan, `EMERGENT_LLM_KEY` environment variable).

Part of the [NextXus Federation](https://keywebco.github.io).
