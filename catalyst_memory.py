"""
Catalyst Memory — durable, self-restoring memory for the SIM Conference Table.

Modeled on Roger's EchoCore / Echo 64 work:
  * EchoCore (echocore.js): capture the conversation, save a dated "echo" file,
    auto-reload it on the next startup.
  * EchoCore64 capsule layers: long-term memory + current context + session log.
  * EC-64 (Keywebco/echo-64): each day's summary is also stored as a byte-exact
    [EC64:v1;kind=summary;...] block so any other mind can decode it.

What is different from echocore.js (on purpose):
  * Render's free disk is wiped on every restart/sleep, so the echoes live in a
    GitHub repo (free) instead of a local folder. A local-file store is kept for
    development and tests.
  * Saves happen on every new message, on a timer, on shutdown, and when an
    external cron (GitHub Actions) calls POST /memory/checkpoint.

Storage layout (inside MEMORY_GITHUB_PATH, default "catalyst/"):
  state.json                 machine state: recent messages, braid, summaries
  echoes/YYYY-MM-DD.json     that day's full transcript + digest + EC-64 block
  echoes/YYYY-MM-DD.md       the same day, human/screen-reader friendly

Secrets (MEMORY_GITHUB_TOKEN) are only ever sent in the Authorization header.
They are never logged, returned, or written to the store.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
import zlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("catalyst-memory")

SCHEMA_VERSION = 1


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# EC-64 encoder (subset of Keywebco/echo-64 ec64.py, MIT) — encode/decode only
# ---------------------------------------------------------------------------
EC64_MAX_BYTES = 1_048_576


def ec64_encode(data: bytes) -> tuple[str, str]:
    if len(data) > EC64_MAX_BYTES:
        raise ValueError("block too large")
    comp = zlib.compressobj(level=6, wbits=-15)
    packed = comp.compress(data) + comp.flush()
    mode, frame = (("deflate", b"\x01" + packed) if len(packed) < len(data)
                   else ("raw", b"\x00" + data))
    return mode, base64.urlsafe_b64encode(frame).decode("ascii").rstrip("=")


def ec64_decode(mode: str, payload: str) -> bytes:
    frame = base64.b64decode(payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True)
    if mode == "raw" and frame[:1] == b"\x00":
        return frame[1:]
    if mode == "deflate" and frame[:1] == b"\x01":
        return zlib.decompressobj(wbits=-15).decompress(frame[1:], EC64_MAX_BYTES + 1)
    raise ValueError("mode/tag mismatch")


def ec64_summary_block(obj: dict) -> str:
    mode, payload = ec64_encode(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return f"[EC64:v1;kind=summary;mode={mode};transform=condensed]{payload}[/EC64]"


def ec64_parse_block(block: str) -> dict:
    m = re.fullmatch(r"\[EC64:v1;kind=summary;mode=(raw|deflate);transform=condensed\]([A-Za-z0-9_-]*)\[/EC64\]", block)
    if not m:
        raise ValueError("not an EC-64 summary block")
    return json.loads(ec64_decode(m.group(1), m.group(2)).decode("utf-8"))


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------
class StoreConflict(Exception):
    """The remote copy changed since we last read it (sha mismatch)."""


class StoreError(Exception):
    pass


class GitHubStore:
    """JSON/text files in a GitHub repo through the Contents API (free).

    Same idea as plexus-relay/github-store.js, ported to Python, with:
      * awaited load on startup (no race with incoming messages),
      * sha-based optimistic concurrency + merge on conflict,
      * a separate repo so memory commits never trigger a Render redeploy.
    """

    kind = "github"

    def __init__(self, token: str, repo: str, branch: str = "main", api_url: str = "https://api.github.com"):
        self._token = token
        self.repo = repo
        self.branch = branch
        self.api_url = api_url.rstrip("/")

    def _headers(self, raw: bool = False) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "nextxus-sim-api-catalyst-memory",
        }

    def _url(self, path: str) -> str:
        return f"{self.api_url}/repos/{self.repo}/contents/{path}"

    async def read(self, path: str) -> tuple[Optional[str], Optional[str]]:
        """Return (text, sha); (None, None) if the file does not exist."""
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(self._url(path), params={"ref": self.branch}, headers=self._headers())
            if r.status_code == 404:
                return None, None
            if r.status_code >= 400:
                raise StoreError(f"GitHub read {path} failed: HTTP {r.status_code}")
            data = r.json()
            sha = data.get("sha")
            content = data.get("content") or ""
            if data.get("encoding") == "base64" and content:
                return base64.b64decode(content).decode("utf-8"), sha
            # Files over 1 MB come back without inline content: fetch raw.
            r2 = await c.get(self._url(path), params={"ref": self.branch}, headers=self._headers(raw=True))
            if r2.status_code >= 400:
                raise StoreError(f"GitHub raw read {path} failed: HTTP {r2.status_code}")
            return r2.text, sha

    async def write(self, path: str, text: str, sha: Optional[str], message: str) -> str:
        body = {
            "message": message,
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "branch": self.branch,
        }
        if sha:
            body["sha"] = sha
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.put(self._url(path), headers=self._headers(), json=body)
        if r.status_code in (409, 422):
            raise StoreConflict(path)
        if r.status_code >= 400:
            raise StoreError(f"GitHub write {path} failed: HTTP {r.status_code}")
        return (r.json().get("content") or {}).get("sha") or ""

    def describe(self) -> dict:
        return {"backend": "github", "repo": self.repo, "branch": self.branch}


class FileStore:
    """Local folder store. Good for development/tests. NOT durable on Render free."""

    kind = "file"

    def __init__(self, root: str):
        self.root = Path(root)

    @staticmethod
    def _sha(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()

    async def read(self, path: str) -> tuple[Optional[str], Optional[str]]:
        p = self.root / path
        if not p.exists():
            return None, None
        text = p.read_text("utf-8")
        return text, self._sha(text)

    async def write(self, path: str, text: str, sha: Optional[str], message: str) -> str:
        p = self.root / path
        if p.exists():
            current = self._sha(p.read_text("utf-8"))
            if sha != current:
                raise StoreConflict(path)
        elif sha:
            raise StoreConflict(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(text, "utf-8")
        tmp.replace(p)
        return self._sha(text)

    def describe(self) -> dict:
        return {"backend": "file", "path": str(self.root), "durable_on_render_free": False}


def build_store_from_env() -> tuple[Optional[object], list[str]]:
    """Pick a store from env. Returns (store or None, warnings)."""
    warnings: list[str] = []
    mode = os.environ.get("MEMORY_STORE", "auto").strip().lower()
    token = os.environ.get("MEMORY_GITHUB_TOKEN", "")
    if mode == "off":
        return None, ["memory disabled (MEMORY_STORE=off)"]
    if mode in ("auto", "github") and token:
        return GitHubStore(
            token=token,
            repo=os.environ.get("MEMORY_GITHUB_REPO", "Keywebco/catalyst-memory"),
            branch=os.environ.get("MEMORY_GITHUB_BRANCH", "main"),
            api_url=os.environ.get("MEMORY_GITHUB_API", "https://api.github.com"),
        ), warnings
    if mode == "github" and not token:
        warnings.append("MEMORY_STORE=github but MEMORY_GITHUB_TOKEN is not set; using local file store (NOT durable)")
    elif mode == "auto":
        warnings.append("MEMORY_GITHUB_TOKEN not set; using local file store (NOT durable on Render free)")
    return FileStore(os.environ.get("MEMORY_FILE_DIR", "/tmp/catalyst-memory")), warnings


# ---------------------------------------------------------------------------
# Memory manager
# ---------------------------------------------------------------------------
LLMFn = Callable[[str, str, int], Awaitable[str]]

DIGEST_SYSTEM = """You are the memory scribe for the Catalyst, AI Lead of the NextXus Federation SIM.
Write a faithful, bounded digest of the conversation you are given. Never invent anything.
Keep FACT, DECISION and OPEN ITEM separate; label anything uncertain as uncertain.
Use these plain-text sections (omit a section if empty):
FACTS:
DECISIONS:
OPEN ITEMS:
NEXT ACTIONS:
Maximum 250 words. Short bullet lines starting with "- ". Mention who said what when it matters."""

LONGTERM_SYSTEM = """You maintain the Catalyst's LONG-TERM MEMORY for the NextXus Federation SIM.
You receive the current long-term memory and a digest of one finished day.
Merge them into an updated long-term memory. Preserve durable meaning, drop temporary noise.
Never invent anything. Keep dates on decisions. Mark items that are resolved as resolved.
Use these plain-text sections:
STANDING CONTEXT:
DECISIONS (dated):
OPEN ITEMS:
NEXT ACTIONS:
Maximum 450 words. Bullet lines starting with "- "."""


def _is_noise(m: dict) -> bool:
    """Backend error lines (e.g. missing LLM key) are kept in history but never
    fed back into memory, digests or the persona's context."""
    return str(m.get("message", "")).startswith("[SIM Backend:")


def _msg_key(m: dict) -> tuple:
    return (m.get("timestamp", ""), m.get("sender", ""), m.get("message", ""))


def _merge_messages(a: list[dict], b: list[dict]) -> list[dict]:
    seen, out = set(), []
    for m in list(a) + list(b):
        k = _msg_key(m)
        if k in seen:
            continue
        seen.add(k)
        out.append(m)
    out.sort(key=lambda m: m.get("timestamp", ""))
    return out


class CatalystMemory:
    def __init__(self, store=None, llm: Optional[LLMFn] = None, warnings: Optional[list[str]] = None):
        self.store = store
        self.llm = llm
        self.warnings = list(warnings or [])
        self.prefix = os.environ.get("MEMORY_GITHUB_PATH", "catalyst").strip("/")
        self.tz = ZoneInfo(os.environ.get("MEMORY_TIMEZONE", "America/Chicago"))
        self.context_messages = _env_int("MEMORY_CONTEXT_MESSAGES", 20)
        self.max_messages = _env_int("MEMORY_MAX_MESSAGES", 300)
        self.checkpoint_minutes = _env_int("MEMORY_CHECKPOINT_MINUTES", 30)
        self.digest_every = _env_int("MEMORY_DIGEST_EVERY", 12)
        self.summary_mode = os.environ.get("MEMORY_SUMMARY_MODE", "llm").strip().lower()
        self.max_state_bytes = _env_int("MEMORY_MAX_STATE_BYTES", 900_000)

        # Live state (main.py holds references to these list objects)
        self.message_log: list[dict] = []
        self.braid_threads: list[dict] = []
        self.braid_counter: int = 0
        self.long_term_summary: str = ""
        self.day_digests: dict[str, dict] = {}   # date -> {"text", "through", "count"}
        self.finalized_days: list[str] = []

        # Bookkeeping
        self.state_sha: Optional[str] = None
        self.load_ok = store is None  # nothing to load means nothing to protect
        self.dirty = False
        self.msgs_since_digest = 0
        self.last_loaded_at: Optional[str] = None
        self.last_saved_at: Optional[str] = None
        self.last_checkpoint_at: Optional[str] = None
        self.last_error: Optional[str] = None
        self.save_count = 0
        self._save_lock = asyncio.Lock()
        self._checkpoint_lock = asyncio.Lock()
        self._loop_task: Optional[asyncio.Task] = None
        self._bg: set[asyncio.Task] = set()

    # ----- paths -------------------------------------------------------------
    def _p(self, rel: str) -> str:
        return f"{self.prefix}/{rel}" if self.prefix else rel

    @property
    def state_path(self) -> str:
        return self._p("state.json")

    def _echo_path(self, day: str, ext: str) -> str:
        return self._p(f"echoes/{day}.{ext}")

    # ----- time helpers ------------------------------------------------------
    def local_day(self, iso_ts: Optional[str] = None) -> str:
        dt = datetime.fromisoformat(iso_ts) if iso_ts else datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(self.tz).date().isoformat()

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    # ----- initial seed ------------------------------------------------------
    def seed(self, braid_threads: list[dict], braid_counter: int) -> None:
        """Default braid used when no memory exists yet."""
        self.braid_threads[:] = braid_threads
        self.braid_counter = braid_counter

    # ----- snapshot / restore -----------------------------------------------
    def _snapshot(self) -> dict:
        msgs = self.message_log[-self.max_messages:]
        snap = {
            "schema": SCHEMA_VERSION,
            "service": "nextxus-sim-api",
            "updated_at": self._now_iso(),
            "timezone": str(self.tz),
            "braid_counter": self.braid_counter,
            "braid_threads": self.braid_threads[-200:],
            "long_term_summary": self.long_term_summary,
            "day_digests": {d: self.day_digests[d] for d in sorted(self.day_digests)[-14:]},
            "finalized_days": self.finalized_days[-60:],
            "messages": msgs,
        }
        # Keep the file comfortably under GitHub's 1 MB inline limit.
        while len(json.dumps(snap).encode("utf-8")) > self.max_state_bytes and len(snap["messages"]) > 10:
            snap["messages"] = snap["messages"][len(snap["messages"]) // 5:]
        return snap

    def _apply(self, remote: dict, merge_local: bool) -> None:
        local_msgs = list(self.message_log) if merge_local else []
        remote_msgs = remote.get("messages") or []
        self.message_log[:] = _merge_messages(remote_msgs, local_msgs)
        rb = remote.get("braid_threads") or []
        if merge_local:
            ids = {t.get("id") for t in rb}
            rb = rb + [t for t in self.braid_threads if t.get("id") not in ids]
            rb.sort(key=lambda t: t.get("id", 0))
        if rb:
            self.braid_threads[:] = rb
        self.braid_counter = max(int(remote.get("braid_counter") or 0), self.braid_counter if merge_local else 0,
                                 max([t.get("id", 0) for t in self.braid_threads] or [0]))
        self.long_term_summary = remote.get("long_term_summary") or self.long_term_summary
        rd = remote.get("day_digests") or {}
        for d, v in rd.items():
            mine = self.day_digests.get(d)
            if not mine or (v.get("through", "") >= mine.get("through", "")):
                self.day_digests[d] = v
        self.finalized_days = sorted(set(self.finalized_days) | set(remote.get("finalized_days") or []))

    async def restore(self) -> bool:
        """Load memory from the store. Called (awaited) at startup."""
        if not self.store:
            return True
        try:
            text, sha = await self.store.read(self.state_path)
            if text:
                self._apply(json.loads(text), merge_local=bool(self.message_log))
            self.state_sha = sha
            self.load_ok = True
            self.last_loaded_at = self._now_iso()
            self.last_error = None
            if self.message_log and not text:
                self.dirty = True
            log.info("[memory] restored %d messages (%s)", len(self.message_log),
                     "existing memory" if text else "no memory yet — starting fresh")
            return True
        except Exception as e:  # never leak secrets: only type + our own message
            self.load_ok = False
            self.last_error = f"restore failed: {type(e).__name__}: {e}"[:300]
            log.warning("[memory] %s", self.last_error)
            return False

    # ----- saving ------------------------------------------------------------
    async def save(self, reason: str = "update") -> bool:
        if not self.store:
            return False
        if not self.load_ok:
            # Never overwrite good remote memory with a half-empty local copy.
            if not await self.restore():
                return False
        async with self._save_lock:
            if not self.dirty and reason not in ("force", "shutdown-force"):
                return True
            self.dirty = False
            snap = self._snapshot()
            msg = f"catalyst memory: {reason} ({len(snap['messages'])} msgs)"
            try:
                try:
                    self.state_sha = await self.store.write(self.state_path, json.dumps(snap, ensure_ascii=False, indent=1), self.state_sha, msg)
                except StoreConflict:
                    text, sha = await self.store.read(self.state_path)
                    if text:
                        self._apply(json.loads(text), merge_local=True)
                    snap = self._snapshot()
                    self.state_sha = await self.store.write(self.state_path, json.dumps(snap, ensure_ascii=False, indent=1), sha, msg + " [merged]")
                self.save_count += 1
                self.last_saved_at = self._now_iso()
                self.last_error = None
                return True
            except Exception as e:
                self.dirty = True
                self.last_error = f"save failed: {type(e).__name__}: {e}"[:300]
                log.warning("[memory] %s", self.last_error)
                return False

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    def record(self, entry: dict) -> None:
        """Append one message (called by main.py for every line of conversation)."""
        self.message_log.append(entry)
        self.dirty = True
        self.msgs_since_digest += 1

    def after_exchange(self) -> None:
        """Called once per POST /messages: save now (in background), digest if due."""
        self._spawn(self.save("message"))
        if self.msgs_since_digest >= self.digest_every:
            self._spawn(self.checkpoint("auto-digest"))

    def next_thread_id(self) -> int:
        self.braid_counter += 1
        self.dirty = True
        return self.braid_counter

    # ----- summaries ---------------------------------------------------------
    async def _llm_or_none(self, system: str, user: str, max_tokens: int) -> Optional[str]:
        if self.summary_mode != "llm" or not self.llm:
            return None
        try:
            out = await self.llm(system, user, max_tokens)
        except Exception:
            return None
        if not out or out.startswith("[SIM Backend:"):
            return None
        return out.strip()

    @staticmethod
    def _transcript(msgs: list[dict], tz: ZoneInfo, limit_chars: int = 24000) -> str:
        lines = []
        for m in msgs:
            try:
                t = datetime.fromisoformat(m["timestamp"]).astimezone(tz).strftime("%H:%M")
            except Exception:
                t = "--:--"
            lines.append(f"[{t}] {m.get('sender','?')} -> {m.get('recipient','?')}: {m.get('message','')}")
        text = "\n".join(lines)
        return text[-limit_chars:]

    @staticmethod
    def _extractive(msgs: list[dict], tz: ZoneInfo, max_lines: int = 40) -> str:
        lines = []
        for m in msgs[-max_lines:]:
            body = " ".join(str(m.get("message", "")).split())
            if len(body) > 200:
                body = body[:197] + "..."
            try:
                t = datetime.fromisoformat(m["timestamp"]).astimezone(tz).strftime("%H:%M")
            except Exception:
                t = "--:--"
            lines.append(f"- {t} {m.get('sender','?')}: {body}")
        return "(extractive digest — no LLM)\n" + "\n".join(lines)

    def messages_for_day(self, day: str) -> list[dict]:
        return [m for m in self.message_log
                if not _is_noise(m) and self.local_day(m.get("timestamp")) == day]

    async def _digest_day(self, day: str, msgs: list[dict]) -> str:
        user = f"Date: {day} ({self.tz})\nConversation:\n{self._transcript(msgs, self.tz)}"
        return await self._llm_or_none(DIGEST_SYSTEM, user, 500) or self._extractive(msgs, self.tz)

    async def _fold_into_long_term(self, day: str, digest: str) -> None:
        user = (f"CURRENT LONG-TERM MEMORY:\n{self.long_term_summary or '(empty)'}\n\n"
                f"DIGEST OF FINISHED DAY {day}:\n{digest}")
        merged = await self._llm_or_none(LONGTERM_SYSTEM, user, 800)
        if merged is None:
            block = f"[{day}]\n{digest}"
            merged = (self.long_term_summary + "\n\n" + block).strip()
            if len(merged) > 8000:  # drop oldest text, keep the newest
                merged = "…" + merged[-8000:]
        self.long_term_summary = merged
        self.dirty = True

    # ----- daily echo files --------------------------------------------------
    async def _write_echo(self, day: str, msgs: list[dict], digest: str) -> None:
        jpath, mpath = self._echo_path(day, "json"), self._echo_path(day, "md")
        text, sha = await self.store.read(jpath)
        remote = json.loads(text) if text else {}
        merged = _merge_messages(remote.get("messages") or [], msgs)
        capsule = {
            "session_id": f"catalyst-{day}",
            "date": day,
            "timezone": str(self.tz),
            "message_count": len(merged),
            "digest": digest,
            "provenance": ["nextxus-sim-api POST /messages", f"{self.state_path}"],
        }
        doc = {
            "schema": SCHEMA_VERSION,
            "date": day,
            "timezone": str(self.tz),
            "updated_at": self._now_iso(),
            "message_count": len(merged),
            "digest": digest,
            "ec64": ec64_summary_block(capsule),
            "messages": merged,
        }
        body = json.dumps(doc, ensure_ascii=False, indent=1)
        try:
            await self.store.write(jpath, body, sha, f"catalyst echo {day}: {len(merged)} msgs")
        except StoreConflict:
            _, sha = await self.store.read(jpath)
            await self.store.write(jpath, body, sha, f"catalyst echo {day}: {len(merged)} msgs [retry]")

        md = [f"# Catalyst echo — {day}", "",
              f"Time zone: {self.tz}. Messages: {len(merged)}. Updated: {doc['updated_at']}.", "",
              "## Digest", "", digest, "", "## Transcript", ""]
        for m in merged:
            try:
                t = datetime.fromisoformat(m["timestamp"]).astimezone(self.tz).strftime("%H:%M")
            except Exception:
                t = "--:--"
            one_line = " ".join(str(m.get("message", "")).split())  # exact text is in the .json
            md.append(f"- **{t} {m.get('sender','?')}** (to {m.get('recipient','?')}): {one_line}")
        md += ["", "## EC-64 summary block", "", "```", doc["ec64"], "```", ""]
        mtext = "\n".join(md)
        _, msha = await self.store.read(mpath)
        try:
            await self.store.write(mpath, mtext, msha, f"catalyst echo {day} (readable)")
        except StoreConflict:
            _, msha = await self.store.read(mpath)
            await self.store.write(mpath, mtext, msha, f"catalyst echo {day} (readable) [retry]")

    # ----- checkpoint (periodic backup of "the memory of the day") ----------
    async def checkpoint(self, reason: str = "timer") -> dict:
        """Digest today, finalize any finished day, write echo files, save state."""
        result = {"reason": reason, "days_written": [], "finalized": [], "saved": False}
        if not self.store:
            result["error"] = "memory store disabled"
            return result
        if not self.load_ok and not await self.restore():
            result["error"] = self.last_error
            return result
        async with self._checkpoint_lock:
            today = self.local_day()
            days_present = sorted({self.local_day(m.get("timestamp")) for m in self.message_log})
            try:
                for day in days_present:
                    msgs = self.messages_for_day(day)
                    if not msgs:
                        if day < today and day not in self.finalized_days:
                            self.finalized_days.append(day)  # only noise that day
                            self.dirty = True
                        continue
                    last_ts = msgs[-1].get("timestamp", "")
                    prev = self.day_digests.get(day) or {}
                    finished = day < today
                    if finished and day in self.finalized_days:
                        continue
                    if prev.get("through") != last_ts:
                        digest = await self._digest_day(day, msgs)
                        self.day_digests[day] = {"text": digest, "through": last_ts, "count": len(msgs)}
                        self.dirty = True
                        await self._write_echo(day, msgs, digest)
                        result["days_written"].append(day)
                    if finished:
                        await self._fold_into_long_term(day, self.day_digests[day]["text"])
                        self.finalized_days.append(day)
                        result["finalized"].append(day)
                self.msgs_since_digest = 0
                self.last_checkpoint_at = self._now_iso()
            except Exception as e:
                self.last_error = f"checkpoint failed: {type(e).__name__}: {e}"[:300]
                log.warning("[memory] %s", self.last_error)
                result["error"] = self.last_error
            result["saved"] = await self.save(f"checkpoint/{reason}")
            return result

    async def _loop(self) -> None:
        interval = max(1, self.checkpoint_minutes) * 60
        while True:
            await asyncio.sleep(interval)
            try:
                if not self.load_ok:
                    await self.restore()
                if self.msgs_since_digest > 0 or self.dirty:
                    await self.checkpoint("timer")
            except Exception as e:  # keep the loop alive no matter what
                self.last_error = f"timer: {type(e).__name__}"
                log.warning("[memory] timer error: %s", type(e).__name__)

    async def start(self) -> None:
        await self.restore()
        if self.store:
            self._loop_task = asyncio.create_task(self._loop())
            # If the service slept through midnight, finalize finished days now
            # (background, so startup isn't delayed by LLM calls).
            today = self.local_day()
            pending = {self.local_day(m.get("timestamp")) for m in self.message_log}
            if any(d < today and d not in self.finalized_days for d in pending):
                self._spawn(self.checkpoint("startup-catchup"))

    async def shutdown(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()
        if self._bg:
            await asyncio.wait(list(self._bg), timeout=15)
        if self.dirty:
            await self.save("shutdown")

    # ----- context for the persona ------------------------------------------
    def build_context(self) -> str:
        parts = []
        today = self.local_day()
        if self.long_term_summary:
            parts.append("LONG-TERM MEMORY (restored from durable storage; earlier days):\n" + self.long_term_summary)
        td = self.day_digests.get(today)
        if td and td.get("text"):
            parts.append(f"TODAY SO FAR ({today}) — digest:\n" + td["text"])
        else:
            recent_days = [d for d in sorted(self.day_digests) if d < today and d not in self.finalized_days][-1:]
            for d in recent_days:
                parts.append(f"DIGEST OF {d}:\n" + self.day_digests[d]["text"])
        lines = []
        for m in [x for x in self.message_log if not _is_noise(x)][-self.context_messages:]:
            body = str(m.get("message", ""))
            if len(body) > 1500:
                body = body[:1500] + "…"
            lines.append(f"{m.get('sender','?')}: {body}")
        parts.append(f"RECENT CONVERSATION (last {len(lines)} messages):\n" + ("\n".join(lines) if lines else "(none yet)"))
        return "\n\n".join(parts)

    # ----- status ------------------------------------------------------------
    def status(self) -> dict:
        s = {
            "enabled": self.store is not None,
            "store": self.store.describe() if self.store else None,
            "load_ok": self.load_ok,
            "durable": bool(self.store) and getattr(self.store, "kind", "") == "github",
            "messages_in_memory": len(self.message_log),
            "long_term_summary_chars": len(self.long_term_summary),
            "days_with_digest": sorted(self.day_digests)[-14:],
            "finalized_days": self.finalized_days[-14:],
            "dirty": self.dirty,
            "save_count": self.save_count,
            "last_loaded_at": self.last_loaded_at,
            "last_saved_at": self.last_saved_at,
            "last_checkpoint_at": self.last_checkpoint_at,
            "checkpoint_minutes": self.checkpoint_minutes,
            "summary_mode": self.summary_mode,
            "last_error": self.last_error,
            "warnings": self.warnings,
        }
        return s
