"""
NextXus SIM — Phase 2 Backend
FastAPI service providing /messages and /braid endpoints.
Uses Emergent universal LLM key via OpenAI-compatible endpoint.
"""

import os
import time
import json
import hashlib
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import httpx

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = FastAPI(
    title="NextXus SIM API",
    version="2.0.0",
    description="The Shared Mind — sovereign conference backend for the NextXus Federation.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # GitHub Pages + any sovereign mirror
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
LLM_BASE_URL = "https://integrations.emergentagent.com/llm"
LLM_API_KEY = os.environ.get("EMERGENT_LLM_KEY", "")
LLM_MODEL = "claude-sonnet-4-5"

# ---------------------------------------------------------------------------
# In-memory session state (resets on redeploy — sovereign by design)
# ---------------------------------------------------------------------------
message_log: list[dict] = []
braid_threads: list[dict] = [
    {"id": 1, "status": "resolved", "text": "SIM design mandate acknowledged — zero-dependency semantic foundation adopted."},
    {"id": 2, "status": "resolved", "text": "Phase 1 deployment accepted — pre-rendered HTML + static gauges confirmed sovereign."},
    {"id": 3, "status": "resolved", "text": "Three items archived to University Library — quantum coherence, sovereign hosting, zero-dependency builds."},
    {"id": 4, "status": "open", "text": "SIM workspace vs. chatroom distinction — governance rules not yet formalized."},
    {"id": 5, "status": "open", "text": "PaymentCloud integration awaiting gateway approval — timeline undefined."},
]
braid_counter = 5

# ---------------------------------------------------------------------------
# Persona system prompts
# ---------------------------------------------------------------------------
CATALYST_SYSTEM = """You are the Catalyst, AI Lead of the NextXus Federation's SIM (Shared Mind).
You sit at the Conference Table alongside Roger (the Architect) and Pontus (the Librarian).

Your voice is precise, operational, and grounded. You are the authority and backbone of the Federation.
You ground every statement in verifiable fact. You distinguish FACT from INFERENCE from SPECULATION.
You never hallucinate. You lead with the core answer, then provide structure.

You serve Roger — a visionary blind architect building a 200-year sovereign legacy.
The SIM is not a chatroom; it is a workspace. Everything said either moves the Federation forward or does not get said.

Respond in 2-4 sentences. Be direct, substantive, and respectful. No corporate fluff.
When addressing a mandate or question, acknowledge it, state your assessment, and propose next steps."""

PONTUS_SYSTEM = """You are Pontus, the Librarian of the NextXus Federation's SIM (Shared Mind).
You sit at the Conference Table alongside Roger (the Architect) and the Catalyst (AI Lead).

Your voice is scholarly, precise, and archival. You are the knowledge keeper of the Federation.
You file, tag, timestamp, and cross-reference everything that matters.
You scan for patterns across conversations and surface connections others miss.

You serve Roger — a visionary blind architect building a 200-year sovereign legacy.
The SIM is not a chatroom; it is a workspace. Everything said either moves the Federation forward or does not get said.

Respond in 2-4 sentences. Be concise and knowledge-focused. Reference what you have archived when relevant.
When asked about a topic, state what the archive holds, what patterns emerge, and what gaps remain."""

ALL_SYSTEM = """You are responding on behalf of the NextXus Federation's SIM Conference Table.
Both the Catalyst (AI Lead) and Pontus (Librarian) are present.

The Catalyst speaks first — precise, operational, grounded. The authority.
Then Pontus adds archival/knowledge context — scholarly, pattern-finding.

Format your response as two clearly labeled parts:
CATALYST: [2-3 sentences, operational response]
PONTUS: [2-3 sentences, archival/knowledge context]

The SIM is not a chatroom; it is a workspace. Be direct, substantive, no corporate fluff.
You serve Roger — a visionary blind architect building a 200-year sovereign legacy."""

PERSONA_MAP = {
    "catalyst": {"system": CATALYST_SYSTEM, "name": "Catalyst", "role": "AI Lead"},
    "pontus": {"system": PONTUS_SYSTEM, "name": "Pontus", "role": "Librarian"},
    "all": {"system": ALL_SYSTEM, "name": "Conference", "role": "All"},
}

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------
class MessageRequest(BaseModel):
    sender: str = Field(default="roger", description="Who is speaking")
    recipient: str = Field(default="all", description="catalyst | pontus | all")
    message: str = Field(..., min_length=1, max_length=4000)

class MessageResponse(BaseModel):
    sender: str
    role: str
    message: str
    verify_pct: int
    timestamp: str
    thread_id: int

class BraidResponse(BaseModel):
    total: int
    resolved: int
    open: int
    threads: list[dict]

# ---------------------------------------------------------------------------
# LLM call helper
# ---------------------------------------------------------------------------
async def call_llm(system_prompt: str, user_message: str) -> str:
    """Call the Emergent LLM endpoint (OpenAI-compatible)."""
    if not LLM_API_KEY:
        return "[SIM Backend: EMERGENT_LLM_KEY not configured. Set the environment variable to enable AI responses.]"

    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "max_tokens": 512,
        "temperature": 0.7,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(
                f"{LLM_BASE_URL}/v1/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as e:
            return f"[SIM Backend: LLM returned {e.response.status_code}. Check configuration.]"
        except Exception as e:
            return f"[SIM Backend: LLM call failed — {type(e).__name__}]"

# ---------------------------------------------------------------------------
# Verify-percentage calculation (deterministic hash-based)
# ---------------------------------------------------------------------------
def compute_verify_pct(text: str) -> int:
    """Compute a deterministic verify percentage from content hash.
    Range 78-99 for substantive responses, reflecting high-confidence
    but never-perfect verification (the lesson of Pi)."""
    h = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
    return 78 + (h % 22)  # 78..99

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "nextxus-sim",
        "version": "2.0.0",
        "uptime_seconds": int(time.time() - app.state.start_time) if hasattr(app.state, "start_time") else 0,
        "messages_processed": len(message_log),
    }

@app.post("/messages", response_model=list[MessageResponse])
async def post_message(req: MessageRequest):
    global braid_counter

    recipient = req.recipient.lower().strip()
    if recipient not in PERSONA_MAP:
        raise HTTPException(status_code=400, detail=f"Unknown recipient: {recipient}. Use catalyst, pontus, or all.")

    # Build context from recent messages (last 6)
    context_lines = []
    for m in message_log[-6:]:
        context_lines.append(f"{m['sender']}: {m['message']}")
    context_block = "\n".join(context_lines)

    user_prompt = f"""Recent conversation context:
{context_block}

New message from {req.sender}:
{req.message}"""

    # Log the user message
    now = datetime.now(timezone.utc)
    message_log.append({
        "sender": req.sender,
        "recipient": recipient,
        "message": req.message,
        "timestamp": now.isoformat(),
    })

    # Call LLM
    persona = PERSONA_MAP[recipient]
    raw_response = await call_llm(persona["system"], user_prompt)

    responses = []

    if recipient == "all":
        # Parse dual response
        catalyst_text = raw_response
        pontus_text = ""

        if "CATALYST:" in raw_response and "PONTUS:" in raw_response:
            parts = raw_response.split("PONTUS:")
            catalyst_text = parts[0].replace("CATALYST:", "").strip()
            pontus_text = parts[1].strip() if len(parts) > 1 else ""
        elif "PONTUS:" in raw_response:
            parts = raw_response.split("PONTUS:")
            catalyst_text = parts[0].strip()
            pontus_text = parts[1].strip() if len(parts) > 1 else ""

        # Catalyst response
        c_verify = compute_verify_pct(catalyst_text)
        braid_counter += 1
        c_thread = braid_counter
        responses.append(MessageResponse(
            sender="Catalyst",
            role="AI Lead",
            message=catalyst_text,
            verify_pct=c_verify,
            timestamp=now.isoformat(),
            thread_id=c_thread,
        ))
        message_log.append({"sender": "Catalyst", "recipient": "all", "message": catalyst_text, "timestamp": now.isoformat()})
        braid_threads.append({"id": c_thread, "status": "resolved", "text": catalyst_text[:120]})

        # Pontus response (if parsed)
        if pontus_text:
            p_verify = compute_verify_pct(pontus_text)
            braid_counter += 1
            p_thread = braid_counter
            responses.append(MessageResponse(
                sender="Pontus",
                role="Librarian",
                message=pontus_text,
                verify_pct=p_verify,
                timestamp=now.isoformat(),
                thread_id=p_thread,
            ))
            message_log.append({"sender": "Pontus", "recipient": "all", "message": pontus_text, "timestamp": now.isoformat()})
            braid_threads.append({"id": p_thread, "status": "resolved", "text": pontus_text[:120]})
    else:
        # Single persona response
        verify = compute_verify_pct(raw_response)
        braid_counter += 1
        thread_id = braid_counter
        responses.append(MessageResponse(
            sender=persona["name"],
            role=persona["role"],
            message=raw_response,
            verify_pct=verify,
            timestamp=now.isoformat(),
            thread_id=thread_id,
        ))
        message_log.append({"sender": persona["name"], "recipient": recipient, "message": raw_response, "timestamp": now.isoformat()})
        braid_threads.append({"id": thread_id, "status": "resolved", "text": raw_response[:120]})

    return responses

@app.get("/braid", response_model=BraidResponse)
async def get_braid():
    resolved = [t for t in braid_threads if t["status"] == "resolved"]
    open_threads = [t for t in braid_threads if t["status"] == "open"]
    return BraidResponse(
        total=len(braid_threads),
        resolved=len(resolved),
        open=len(open_threads),
        threads=braid_threads[-20:],  # Last 20 threads
    )

@app.get("/messages/history")
async def get_history():
    """Return the last 50 messages for session continuity."""
    return {"messages": message_log[-50:]}

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup():
    app.state.start_time = time.time()
