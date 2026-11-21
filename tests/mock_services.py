"""
Local stand-ins for the two external services, for tests only:
  * a GitHub Contents API (GET/PUT /repos/{owner}/{repo}/contents/{path})
  * an OpenAI-compatible chat endpoint (POST /v1/chat/completions)

Run:  python tests/mock_services.py github 9101 <expected-token>
      python tests/mock_services.py llm 9102
No real credentials are involved.
"""
import base64
import hashlib
import json
import sys

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
import uvicorn


def make_github(expected_token: str) -> FastAPI:
    app = FastAPI()
    files: dict[str, str] = {}      # "owner/repo:branch:path" -> text
    stats = {"get": 0, "put": 0, "fail": False}

    def sha(text: str) -> str:
        return hashlib.sha1(text.encode()).hexdigest()

    def auth_ok(req: Request) -> bool:
        return req.headers.get("authorization") == f"Bearer {expected_token}"

    @app.get("/repos/{owner}/{repo}/contents/{path:path}")
    async def get(owner: str, repo: str, path: str, request: Request, ref: str = "main"):
        stats["get"] += 1
        if stats["fail"]:
            return JSONResponse({"message": "simulated outage"}, status_code=503)
        if not auth_ok(request):
            return JSONResponse({"message": "Bad credentials"}, status_code=401)
        key = f"{owner}/{repo}:{ref}:{path}"
        if key not in files:
            return JSONResponse({"message": "Not Found"}, status_code=404)
        text = files[key]
        if "raw" in request.headers.get("accept", ""):
            return PlainTextResponse(text)
        return {"sha": sha(text), "encoding": "base64", "path": path,
                "content": base64.b64encode(text.encode()).decode()}

    @app.put("/repos/{owner}/{repo}/contents/{path:path}")
    async def put(owner: str, repo: str, path: str, request: Request):
        stats["put"] += 1
        if stats["fail"]:
            return JSONResponse({"message": "simulated outage"}, status_code=503)
        if not auth_ok(request):
            return JSONResponse({"message": "Bad credentials"}, status_code=401)
        body = await request.json()
        key = f"{owner}/{repo}:{body.get('branch','main')}:{path}"
        if key in files:
            if not body.get("sha"):
                return JSONResponse({"message": "sha wasn't supplied"}, status_code=422)
            if body["sha"] != sha(files[key]):
                return JSONResponse({"message": "conflict"}, status_code=409)
        elif body.get("sha"):
            return JSONResponse({"message": "conflict"}, status_code=409)
        text = base64.b64decode(body["content"]).decode()
        files[key] = text
        return {"content": {"sha": sha(text), "path": path}, "commit": {"message": body.get("message")}}

    @app.get("/_debug/files")
    async def dbg_files():
        return {"files": {k: v for k, v in files.items()}, "stats": stats}

    @app.post("/_debug/fail/{flag}")
    async def dbg_fail(flag: int):
        stats["fail"] = bool(flag)
        return stats

    return app


def make_llm() -> FastAPI:
    app = FastAPI()
    requests: list[dict] = []

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        requests.append(body)
        system = body["messages"][0]["content"]
        user = body["messages"][-1]["content"]
        if "memory scribe" in system:
            n = user.count("\n[")
            text = f"FACTS:\n- MOCK DIGEST covering {n + 1} lines. Keywords: " + \
                   ", ".join(sorted({w for w in user.split() if w.isupper() and len(w) > 4}))[:300]
        elif "LONG-TERM MEMORY" in system:
            text = "STANDING CONTEXT:\n- MOCK LONG-TERM MEMORY merged from: " + user[-400:].replace("\n", " ")
        else:
            last = user.strip().splitlines()[-1]
            text = f"MOCK CATALYST REPLY to: {last[:120]}"
        return {"choices": [{"message": {"role": "assistant", "content": text}}]}

    @app.get("/_debug/requests")
    async def dbg():
        return {"requests": requests}

    return app


if __name__ == "__main__":
    kind, port = sys.argv[1], int(sys.argv[2])
    app = make_github(sys.argv[3]) if kind == "github" else make_llm()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
