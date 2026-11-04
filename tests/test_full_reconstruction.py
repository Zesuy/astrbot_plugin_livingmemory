"""Offline selector coverage: no real chats or cloud calls."""
import json
from pathlib import Path
import runpy
import sqlite3
import sys
from types import SimpleNamespace

import pytest


def test_full_selection_preserves_short_windows_and_excludes_bots(tmp_path, monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    select = runpy.run_path(str(scripts / "run_full_reconstruction.py"))["select_windows"]
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE archive_messages(id INTEGER,conversation TEXT,sender_id TEXT,role TEXT,sent_at INTEGER,components TEXT)")
        rows = [(i, "main", "100", "user", 1000+i, json.dumps([{"type": "plain", "text": "message"}])) for i in range(1, 37)]
        rows += [(40, "main", "100", "user", 4000, json.dumps([{"type": "plain", "text": "tail"}])),
                 (41, "main", "3162054915", "user", 4001, json.dumps([{"type": "plain", "text": "bot"}])),
                 (42, "main", "900", "assistant", 4002, "[]"),
                 (43, "main", "100", "user", 4003, json.dumps([{"type": "reply", "text": "quoted"}])),
                 (44, "other", "100", "user", 4004, json.dumps([{"type": "plain", "text": "other"}]))]
        db.executemany("INSERT INTO archive_messages VALUES(?,?,?,?,?,?)", rows)
    result = select(source, None, 35)
    assert result["scope"] == "main"
    assert result["valid_messages"] == 37
    assert [len(w["messages"]) for w in result["windows"]] == [35, 1, 1]
    assert result["members"] == ["100"]


@pytest.mark.asyncio
async def test_upstream_http_failures_open_circuit_and_remain_in_ledger(tmp_path, monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    calls = []
    class Client:
        async def post(self, *args, **kwargs):
            calls.append(1)
            return SimpleNamespace(status_code=400)
    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(AsyncClient=lambda **kw: Client()))
    monkeypatch.setenv("CLIPROXYAPI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("CLIPROXYAPI_API_KEY", "test-only")
    provider_class = runpy.run_path(str(scripts / "run_full_reconstruction.py"))["Provider"]
    with sqlite3.connect(tmp_path / "ledger.db") as ledger:
        provider = provider_class("test-model", 4, ledger)
        for _ in range(6):
            with pytest.raises(RuntimeError, match="cloud_http_400"):
                await provider.text_chat(prompt="test", system_prompt="test")
        with pytest.raises(RuntimeError, match="cloud_suspended"):
            await provider.text_chat(prompt="test", system_prompt="test")
        assert len(calls) == 6
        assert ledger.execute("SELECT status,COUNT(*) FROM calls GROUP BY status").fetchall() == [("cloud_http_400", 6)]
        assert provider.service_error == "consecutive_http_400"
