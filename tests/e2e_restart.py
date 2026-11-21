"""
End-to-end restart test for Catalyst Memory, fully local.

Starts a mock GitHub Contents API and a mock LLM, then runs the real app with
uvicorn, sends messages, kills/restarts it, and checks the memory comes back.
Uses throwaway random tokens (never real credentials; never printed).

Run from the repo root:  python tests/e2e_restart.py
"""
import json
import os
import secrets
import signal
import subprocess
import sys
import time

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
GH_PORT, LLM_PORT, APP_PORT = 9101, 9102, 9100
APP = f"http://127.0.0.1:{APP_PORT}"
GH = f"http://127.0.0.1:{GH_PORT}"
LLM = f"http://127.0.0.1:{LLM_PORT}"

GH_TOKEN = "test-" + secrets.token_hex(16)        # dummy, mock only
CHECKPOINT_TOKEN = "test-" + secrets.token_hex(16)

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)


def wait_http(url: str, timeout: float = 20) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"timeout waiting for {url}")


def start_app(extra_env: dict | None = None, log_name: str = "app") -> subprocess.Popen:
    env = dict(os.environ)
    env.update({
        "MEMORY_STORE": "auto",
        "MEMORY_GITHUB_TOKEN": GH_TOKEN,
        "MEMORY_GITHUB_API": GH,
        "MEMORY_GITHUB_REPO": "Keywebco/catalyst-memory",
        "MEMORY_CHECKPOINT_TOKEN": CHECKPOINT_TOKEN,
        "MEMORY_DIGEST_EVERY": "100",
        "MEMORY_CHECKPOINT_MINUTES": "30",
        "EMERGENT_LLM_KEY": "test-llm-" + secrets.token_hex(8),
        "LLM_BASE_URL": LLM,
    })
    env.update(extra_env or {})
    logf = open(os.path.join(ROOT, "tests", f".{log_name}.log"), "a")
    p = subprocess.Popen([PY, "-m", "uvicorn", "main:app", "--port", str(APP_PORT), "--log-level", "warning"],
                         cwd=ROOT, env=env, stdout=logf, stderr=subprocess.STDOUT)
    wait_http(f"{APP}/health")
    return p


def stop(p: subprocess.Popen, sig=signal.SIGTERM) -> None:
    p.send_signal(sig)
    try:
        p.wait(timeout=20)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()


def gh_files() -> dict:
    return httpx.get(f"{GH}/_debug/files").json()


def gh_state() -> dict | None:
    f = gh_files()["files"]
    k = "Keywebco/catalyst-memory:main:catalyst/state.json"
    return json.loads(f[k]) if k in f else None


def post(msg: str, recipient: str = "catalyst", sender: str = "roger") -> list:
    r = httpx.post(f"{APP}/messages", json={"sender": sender, "recipient": recipient, "message": msg}, timeout=30)
    r.raise_for_status()
    return r.json()


def last_llm_user_prompt() -> str:
    reqs = httpx.get(f"{LLM}/_debug/requests").json()["requests"]
    persona = [r for r in reqs if "memory scribe" not in r["messages"][0]["content"]
               and "LONG-TERM MEMORY" not in r["messages"][0]["content"]]
    return persona[-1]["messages"][-1]["content"]


def main() -> int:
    sys.path.insert(0, ROOT)
    from catalyst_memory import ec64_parse_block

    gh = subprocess.Popen([PY, "tests/mock_services.py", "github", str(GH_PORT), GH_TOKEN], cwd=ROOT)
    llm = subprocess.Popen([PY, "tests/mock_services.py", "llm", str(LLM_PORT)], cwd=ROOT)
    procs = [gh, llm]
    try:
        wait_http(f"{GH}/_debug/files")
        wait_http(f"{LLM}/_debug/requests")

        # ---- 1. first boot, empty memory -----------------------------------
        app = start_app()
        st = httpx.get(f"{APP}/memory/status").json()
        check("boot: store is github + durable", st["durable"] and st["store"]["backend"] == "github", json.dumps(st["store"]))
        check("boot: load_ok with empty store", st["load_ok"] and st["messages_in_memory"] == 0)

        # ---- 2. talk ---------------------------------------------------------
        post("Catalyst, remember this: the codeword is BLUEHERON.")
        post("Decision: we launch FALCONRIDGE on Monday.")
        post("Status of the relay?", recipient="all")
        time.sleep(1.5)
        s = gh_state()
        n_msgs = len(s["messages"]) if s else 0
        check("save-on-message: state.json written to (mock) GitHub", s is not None and n_msgs >= 6, f"{n_msgs} messages stored")
        check("braid ids persisted", s and s["braid_counter"] >= 8, f"braid_counter={s and s['braid_counter']}")
        check("no token in stored state", s and GH_TOKEN not in json.dumps(s) and CHECKPOINT_TOKEN not in json.dumps(s))

        # ---- 3. checkpoint endpoint auth + daily echo ------------------------
        r = httpx.post(f"{APP}/memory/checkpoint", timeout=30)
        check("checkpoint without token -> 401", r.status_code == 401, str(r.status_code))
        r = httpx.post(f"{APP}/memory/checkpoint", headers={"Authorization": "Bearer wrong"}, timeout=30)
        check("checkpoint with wrong token -> 401", r.status_code == 401, str(r.status_code))
        r = httpx.post(f"{APP}/memory/checkpoint", headers={"Authorization": f"Bearer {CHECKPOINT_TOKEN}"}, timeout=60)
        body = r.json()
        check("checkpoint with token -> 200 + day written", r.status_code == 200 and body["result"]["days_written"], json.dumps(body["result"]))
        files = gh_files()["files"]
        echo_json = [k for k in files if "/echoes/" in k and k.endswith(".json")]
        echo_md = [k for k in files if "/echoes/" in k and k.endswith(".md")]
        check("daily echo files (json + md) written", bool(echo_json and echo_md), ", ".join(k.split(":")[-1] for k in echo_json + echo_md))
        if echo_json:
            doc = json.loads(files[echo_json[0]])
            cap = ec64_parse_block(doc["ec64"])
            check("EC-64 summary block decodes byte-exact to digest", cap["digest"] == doc["digest"], doc["ec64"][:60] + "…")
            check("echo transcript contains the facts", "BLUEHERON" in json.dumps(doc["messages"]))

        # ---- 4. graceful restart (SIGTERM, like a Render deploy) -------------
        post("Also note: budget stays at ZERO dollars, free tools only.")
        stop(app, signal.SIGTERM)
        app = start_app()
        hist = httpx.get(f"{APP}/messages/history").json()["messages"]
        joined = json.dumps(hist)
        check("after SIGTERM restart: history restored", "BLUEHERON" in joined and "ZERO dollars" in joined, f"{len(hist)} messages back")
        braid = httpx.get(f"{APP}/braid").json()
        check("after restart: braid restored", braid["total"] >= 9, f"total={braid['total']}")
        post("What is the codeword, and what did we decide to launch?")
        prompt = last_llm_user_prompt()
        check("after restart: Catalyst prompt contains old memory", "BLUEHERON" in prompt and "FALCONRIDGE" in prompt)
        check("after restart: prompt contains today's digest", "TODAY SO FAR" in prompt and "MOCK DIGEST" in prompt)

        # ---- 5. hard kill (SIGKILL, like a crash/OOM) ------------------------
        post("Hard-kill test fact: the falcon is GOLDENEYE.")
        time.sleep(1.5)
        stop(app, signal.SIGKILL)
        app = start_app()
        hist = json.dumps(httpx.get(f"{APP}/messages/history").json())
        check("after SIGKILL restart (1.5 s after reply): fact survived", "GOLDENEYE" in hist)

        # ---- 6. store outage at boot must NOT wipe memory --------------------
        stop(app)
        httpx.post(f"{GH}/_debug/fail/1")
        app = start_app({"MEMORY_CHECKPOINT_MINUTES": "1"})
        st = httpx.get(f"{APP}/memory/status").json()
        check("outage at boot: load_ok=false reported", st["load_ok"] is False, st["last_error"] or "")
        post("Message during outage: OUTAGEFACT.")
        time.sleep(1)
        httpx.post(f"{GH}/_debug/fail/0")
        s = gh_state()
        check("outage: remote memory not overwritten", s and "BLUEHERON" in json.dumps(s))
        print("… waiting ~70 s for the in-process timer to recover", flush=True)
        time.sleep(70)
        st = httpx.get(f"{APP}/memory/status").json()
        s = gh_state()
        sj = json.dumps(s)
        check("timer: recovered, reloaded and merged", st["load_ok"] and "BLUEHERON" in sj and "OUTAGEFACT" in sj,
              f"last_checkpoint_at={st['last_checkpoint_at']}")
        stop(app)

        stats = gh_files()["stats"]
        print(f"mock GitHub calls: GET={stats['get']} PUT={stats['put']}")
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait(timeout=10)

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
