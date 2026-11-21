"""Unit tests for catalyst_memory (no network). Run: python -m pytest tests -q"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import catalyst_memory as cm  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def msg(sender, text, when):
    return {"sender": sender, "recipient": "catalyst", "message": text, "timestamp": when.isoformat()}


def test_ec64_matches_reference_golden_vectors():
    # Golden vectors from Keywebco/echo-64 ec64.py TEST_VECTORS
    assert cm.ec64_encode(b"") == ("raw", "AA")
    assert cm.ec64_encode(b"A") == ("raw", "AEE")
    assert cm.ec64_encode(b"Truth before comfort.") == ("raw", "AFRydXRoIGJlZm9yZSBjb21mb3J0Lg")
    data = ("Truth before comfort. " * 20).encode()
    mode, payload = cm.ec64_encode(data)
    assert mode == "deflate" and cm.ec64_decode(mode, payload) == data


def test_restart_roundtrip_file_store(tmp_path):
    async def go():
        store = cm.FileStore(str(tmp_path))
        m1 = cm.CatalystMemory(store=store)
        await m1.start()
        now = datetime.now(timezone.utc)
        m1.record(msg("roger", "remember PELICAN", now))
        m1.next_thread_id()
        assert await m1.save("test")
        await m1.shutdown()

        m2 = cm.CatalystMemory(store=cm.FileStore(str(tmp_path)))
        await m2.start()
        assert any("PELICAN" in m["message"] for m in m2.message_log)
        assert m2.braid_counter == 1
        assert "PELICAN" in m2.build_context()
        await m2.shutdown()
    run(go())


def test_day_rollover_finalizes_and_builds_long_term(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_SUMMARY_MODE", "extractive")
    async def go():
        mem = cm.CatalystMemory(store=cm.FileStore(str(tmp_path)))
        await mem.start()
        yesterday = datetime.now(timezone.utc) - timedelta(days=1, hours=1)
        mem.record(msg("roger", "Yesterday decision: adopt OSPREY plan", yesterday))
        mem.record(msg("Catalyst", "Acknowledged OSPREY", yesterday + timedelta(minutes=1)))
        mem.record(msg("roger", "Today: check HERON status", datetime.now(timezone.utc)))
        res = await mem.checkpoint("test")
        yday = mem.local_day(yesterday.isoformat())
        assert yday in res["finalized"]
        assert "OSPREY" in mem.long_term_summary
        ctx = mem.build_context()
        assert "LONG-TERM MEMORY" in ctx and "TODAY SO FAR" in ctx and "HERON" in ctx
        assert (tmp_path / f"catalyst/echoes/{yday}.json").exists()
        assert (tmp_path / f"catalyst/echoes/{yday}.md").exists()
        # A second checkpoint must not finalize the same day twice
        res2 = await mem.checkpoint("again")
        assert yday not in res2["finalized"]
        await mem.shutdown()
    run(go())


def test_conflict_merges_instead_of_overwriting(tmp_path):
    async def go():
        a = cm.CatalystMemory(store=cm.FileStore(str(tmp_path)))
        b = cm.CatalystMemory(store=cm.FileStore(str(tmp_path)))
        await a.start(); await b.start()
        t = datetime.now(timezone.utc)
        a.record(msg("roger", "from instance A", t))
        b.record(msg("roger", "from instance B", t + timedelta(seconds=1)))
        assert await a.save("a")
        assert await b.save("b")          # stale sha -> conflict -> merge
        state = json.loads((tmp_path / "catalyst/state.json").read_text())
        texts = [m["message"] for m in state["messages"]]
        assert "from instance A" in texts and "from instance B" in texts
    run(go())


def test_llm_failure_falls_back_to_extractive(tmp_path):
    async def bad_llm(system, user, max_tokens):
        return "[SIM Backend: LLM returned 500. Check configuration.]"
    async def go():
        mem = cm.CatalystMemory(store=cm.FileStore(str(tmp_path)), llm=bad_llm)
        await mem.start()
        mem.record(msg("roger", "fact KESTREL", datetime.now(timezone.utc)))
        await mem.checkpoint("t")
        today = mem.local_day()
        assert "extractive" in mem.day_digests[today]["text"] and "KESTREL" in mem.day_digests[today]["text"]
    run(go())


def test_state_file_stays_under_size_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_MAX_STATE_BYTES", "50000")
    mem = cm.CatalystMemory(store=cm.FileStore(str(tmp_path)))
    t = datetime.now(timezone.utc)
    for i in range(300):
        mem.record(msg("roger", "x" * 1000 + str(i), t + timedelta(seconds=i)))
    snap = mem._snapshot()
    assert len(json.dumps(snap).encode()) <= 50000
    assert snap["messages"][-1]["message"].endswith("299")   # newest kept


def test_store_selection(monkeypatch):
    monkeypatch.delenv("MEMORY_GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("MEMORY_STORE", "auto")
    store, warnings = cm.build_store_from_env()
    assert isinstance(store, cm.FileStore) and warnings
    monkeypatch.setenv("MEMORY_GITHUB_TOKEN", "dummy-not-real")
    store, _ = cm.build_store_from_env()
    assert isinstance(store, cm.GitHubStore)
    assert "dummy-not-real" not in json.dumps(store.describe())
    monkeypatch.setenv("MEMORY_STORE", "off")
    store, _ = cm.build_store_from_env()
    assert store is None
