"""Offline clean-slate replay. Private artifacts must stay under ignored data/.

Read-only snapshot; no QQ, AstrBot instance, vectors or old memory is used.
Resume requires identical source, code and options. API usage is durably logged
even if a response cannot be parsed; unknown usage is never estimated as zero.
"""
from __future__ import annotations

import argparse
import asyncio
import contextvars
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
import hashlib
import gzip
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
import types

from local_pilot_replay import ROOT, bootstrap

JOB = contextvars.ContextVar("replay_job", default="unknown")
BOTS = {"3658276025", "3778698314", "3829158339", "3768485245", "3162054915"}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def select_windows(source, scope, cap):
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        scopes = db.execute("SELECT conversation,COUNT(*) n FROM archive_messages GROUP BY conversation ORDER BY n DESC").fetchall()
        scope = scope or scopes[0][0]
        bots = BOTS | {r[0] for r in db.execute("SELECT DISTINCT sender_id FROM archive_messages WHERE role='assistant'")}
        rows = db.execute("SELECT * FROM archive_messages WHERE conversation=? ORDER BY sent_at,id", (scope,)).fetchall()
    valid = []
    for raw in rows:
        row = dict(raw)
        if row["role"] != "user" or not row["sent_at"] or row["sender_id"] in bots or not row["sender_id"].isdecimal():
            continue
        components = json.loads(row["components"] or "[]")
        text = " ".join(str(p.get("text", "")) for p in components if str(p.get("type", "")).lower() == "plain").strip()
        if not text or re.fullmatch(r"(?:\[[^\]]+\]\s*)+", text):
            continue
        row["text"] = text
        valid.append(row)
    windows, current = [], []
    def day(row):
        return datetime.fromtimestamp(row["sent_at"], timezone(timedelta(hours=8))).date()
    for row in valid:
        if current and (len(current) >= cap or day(current[-1]) != day(row) or row["sent_at"] - current[-1]["sent_at"] >= 1800):
            windows.append({"id": len(windows) + 1, "messages": current})
            current = []
        current.append(row)
    if current:
        windows.append({"id": len(windows) + 1, "messages": current})
    return {"scope": scope, "raw_messages": len(rows), "valid_messages": len(valid),
            "members": sorted({r["sender_id"] for r in valid}), "bot_ids": sorted(bots),
            "max_messages": cap, "silence_seconds": 1800, "min_messages": 1,
            "day_timezone": "Asia/Shanghai", "windows": windows}


class Provider:
    def __init__(self, model, concurrency, ledger):
        import httpx
        self.client = httpx.AsyncClient(timeout=120)
        self.model = model
        self.provider_config = {"id": "offline-full-replay"}
        self.semaphore = asyncio.Semaphore(concurrency)
        self.ledger = ledger
        ledger.execute("CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY,job TEXT,started REAL,seconds REAL,status TEXT,model TEXT,usage TEXT)")
        ledger.commit()

    async def text_chat(self, *, prompt, system_prompt):
        async with self.semaphore:
            started = time.time()
            cursor = self.ledger.execute("INSERT INTO calls(job,started,status) VALUES(?,?,?)", (JOB.get(), started, "inflight"))
            self.ledger.commit()
            call_id = cursor.lastrowid
            try:
                response = await self.client.post(os.environ["CLIPROXYAPI_BASE_URL"].rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + os.environ["CLIPROXYAPI_API_KEY"]},
                    json={"model": self.model, "temperature": 0, "messages": [
                        {"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}]})
                if response.status_code != 200:
                    raise RuntimeError("cloud_http_" + str(response.status_code))
                result = response.json()
                self.ledger.execute("UPDATE calls SET seconds=?,status='received',model=?,usage=? WHERE id=?",
                    (time.time() - started, result.get("model"), json.dumps(result.get("usage")), call_id))
                self.ledger.commit()
                return types.SimpleNamespace(completion_text=result["choices"][0]["message"]["content"])
            except BaseException as exc:
                self.ledger.execute("UPDATE calls SET seconds=?,status=? WHERE id=? AND status='inflight'",
                    (time.time() - started, type(exc).__name__, call_id))
                self.ledger.commit()
                raise


async def run(args):
    if not 3 <= args.concurrency <= 5:
        raise ValueError("concurrency_must_be_3_to_5")
    if not all(os.environ.get(k) for k in ("CLIPROXYAPI_BASE_URL", "CLIPROXYAPI_API_KEY")):
        raise RuntimeError("required_CLIPROXYAPI_environment_missing")
    source, dest = Path(args.input).resolve(), Path(args.output).resolve()
    if not dest.is_relative_to(ROOT / "data"):
        raise ValueError("output_must_be_inside_ignored_data")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Do not allow two writers to race a checkpoint. Lock removed on clean exit.
    lock = Path(str(dest) + ".lock")
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    try:
        await reconstruct(args, source, dest)
    finally:
        lock.unlink()


async def reconstruct(args, source, dest):
    bootstrap()
    from pilot_plugin.core.processors.memory_processor import MemoryProcessor
    from pilot_plugin.core.models.conversation_models import Message
    from pilot_plugin.core.models.archive_models import ArchiveMessage
    from pilot_plugin.storage.conversation_store import ConversationStore
    from pilot_plugin.core.user_profile_runtime import UserProfileRuntime, EXTRACTION_POLICY

    if source.suffix == ".gz":
        with gzip.open(source, "rt", encoding="utf-8") as handle:
            selection = json.load(handle)
        if selection["max_messages"] != args.max_messages or (args.scope and selection["scope"] != args.scope):
            raise ValueError("slice_options_mismatch")
    else:
        selection = select_windows(source, args.scope, args.max_messages)
    code_files = sorted([*ROOT.joinpath("core").rglob("*.py"), *ROOT.joinpath("core/prompts").glob("*.txt"),
                         *ROOT.joinpath("storage").rglob("*.py"), Path(__file__),
                         ROOT / "scripts/local_pilot_replay.py"])
    manifest = {"source_sha256": digest(source), "model": args.model,
                "code_sha256": hashlib.sha256("".join(str(p.relative_to(ROOT)) + digest(p) for p in code_files).encode()).hexdigest(),
                "selection": {k: v for k, v in selection.items() if k != "windows"}}
    manifest_path = Path(str(dest) + ".manifest.json")
    if dest.exists():
        if not manifest_path.exists() or json.loads(manifest_path.read_text()) != manifest:
            raise ValueError("resume_fingerprint_mismatch")
    else:
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    slices = Path(str(dest) + ".slices.json")
    if not slices.exists():
        slices.write_text(json.dumps(selection, ensure_ascii=False), encoding="utf-8")
    ledger = sqlite3.connect(str(dest) + ".ledger.db")
    provider = Provider(args.model, args.concurrency, ledger)
    store = ConversationStore(str(dest))
    await store.initialize()
    await store.connection.executescript("""
        CREATE TABLE IF NOT EXISTS documents(id INTEGER PRIMARY KEY,window_id INTEGER UNIQUE,content TEXT,metadata TEXT,importance REAL);
        CREATE TABLE IF NOT EXISTS memory_atoms(id INTEGER PRIMARY KEY,parent_memory_id INTEGER,atom_json TEXT);
        CREATE TABLE IF NOT EXISTS reconstruction_windows(id INTEGER PRIMARY KEY,status TEXT,result_json TEXT);
        CREATE TABLE IF NOT EXISTS reconstruction_state(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS reconstruction_runs(id INTEGER PRIMARY KEY,started REAL,seconds REAL);
    """)
    started = time.time()
    cursor = await store.connection.execute("INSERT INTO reconstruction_runs(started) VALUES(?)", (started,))
    run_id = cursor.lastrowid
    await store.connection.commit()
    async def state(key, value):
        async with store.transaction():
            await store.connection.execute("INSERT OR REPLACE INTO reconstruction_state VALUES(?,?)", (key, json.dumps(value)))
    try:
        for window in selection["windows"]:
            for row in window["messages"]:
                await store.archive_record(ArchiveMessage(uid=row["uid"], conversation=row["conversation"],
                    sender_id=row["sender_id"], nickname=row["nickname"], platform=row["platform"], text=row["text"],
                    sent_at=row["sent_at"], time_quality="platform", components=json.loads(row["components"]),
                    mentions=json.loads(row["mentions"] or "[]"), epoch=store.accepted_epoch(row["conversation"])))
        completed = {r[0] for r in await store.read_connection.execute_fetchall("SELECT id FROM reconstruction_windows WHERE status != 'failed'")}
        queue = asyncio.Queue()
        for window in selection["windows"]:
            if window["id"] not in completed:
                queue.put_nowait(window)

        async def memory_worker():
            processor = MemoryProcessor(llm_provider=provider, config={"summary_mode": "events", "llm_stream_mode": "never"})
            while not queue.empty():
                window = queue.get_nowait()
                wid, rows = window["id"], window["messages"]
                JOB.set("memory:" + str(wid))
                result = {"id": wid, "messages": len(rows)}
                try:
                    messages = [Message(id=r["id"], session_id=r["conversation"], role="user", content=r["text"],
                        sender_id=r["sender_id"], sender_name=r["nickname"], group_id=r["conversation"],
                        platform=r["platform"], timestamp=r["sent_at"], metadata={"archive_uid": r["uid"]}) for r in rows]
                    content, metadata, importance = await processor.process_conversation(messages, is_group_chat=True)
                    result.update(status=metadata.get("processing_status", "stored"), content=content, metadata=metadata)
                    async with store.transaction():
                        if content and result["status"] != "processed_no_memory":
                            cur = await store.connection.execute("INSERT INTO documents(window_id,content,metadata,importance) VALUES(?,?,?,?)",
                                (wid, content, json.dumps(metadata, ensure_ascii=False), importance))
                            for atom in processor.classify_atoms_from_metadata(metadata, importance, session_id=rows[0]["conversation"]):
                                await store.connection.execute("INSERT INTO memory_atoms(parent_memory_id,atom_json) VALUES(?,?)",
                                    (cur.lastrowid, json.dumps(asdict(atom), ensure_ascii=False, default=str)))
                        await store.connection.execute("INSERT OR REPLACE INTO reconstruction_windows VALUES(?,?,?)",
                            (wid, result["status"], json.dumps(result, ensure_ascii=False)))
                except Exception as exc:
                    result.update(status="failed", error_type=type(exc).__name__)
                    async with store.transaction():
                        await store.connection.execute("INSERT OR REPLACE INTO reconstruction_windows VALUES(?,?,?)",
                            (wid, "failed", json.dumps(result, ensure_ascii=False)))
                queue.task_done()
                print(json.dumps({"window": wid, "status": result["status"], "remaining": queue.qsize()}), flush=True)

        async def profile_worker():
            async def allowed(event):
                return True
            config = {"user_profiles.enabled": True, "user_profiles.update_interval_seconds": 0,
                      "user_profiles.bot_ids": ",".join(selection["bot_ids"])}
            runtime = UserProfileRuntime(types.SimpleNamespace(store=store, config=config, enabled=True, may_capture=allowed))
            processor = MemoryProcessor(llm_provider=provider, config={"llm_stream_mode": "never"})
            scope = selection["scope"]
            # One global profile writer preserves identity transfers. Process the next
            # member batch in archive order; each successful CAS is its checkpoint.
            while True:
                pending = await store.read_connection.execute_fetchall(
                    "SELECT a.sender_id,MIN(a.id) first_id FROM archive_messages a LEFT JOIN user_profiles p "
                    "ON p.scope_id=a.conversation AND p.user_id=a.sender_id WHERE a.conversation=? "
                    "AND a.id>COALESCE(p.cursor_id,0) GROUP BY a.sender_id ORDER BY first_id LIMIT 1", (scope,))
                if not pending:
                    await state("profiles_complete", True)
                    return
                user = pending[0][0]
                JOB.set("profile:" + user + ":" + str(pending[0][1]))
                event = types.SimpleNamespace(unified_msg_origin=scope, get_sender_id=lambda: user,
                    get_group_id=lambda: scope, get_self_id=lambda: "offline")
                async def extract(prompt):
                    return await processor._call_llm_with_retry(prompt, EXTRACTION_POLICY, max_retries=1)
                for attempt in range(3):
                    try:
                        if not await runtime.refresh(scope, user, event=event, extractor=extract):
                            raise RuntimeError("profile_commit_failed")
                        break
                    except Exception as exc:
                        if attempt == 2:
                            await state("profile_error", {"user": user, "type": type(exc).__name__})
                            return
                        await asyncio.sleep(2 ** attempt)
                print(json.dumps({"profile_batch": pending[0][1]}), flush=True)

        await asyncio.gather(*(memory_worker() for _ in range(args.concurrency - 1)), profile_worker())
    finally:
        async with store.transaction():
            await store.connection.execute("UPDATE reconstruction_runs SET seconds=? WHERE id=?", (time.time() - started, run_id))
        profiles = [{"scope_id": r[0], "user_id": r[1], "profile": json.loads(r[2])}
                    for r in await store.read_connection.execute_fetchall("SELECT scope_id,user_id,profile_json FROM user_profiles")]
        windows = [json.loads(r[0]) for r in await store.read_connection.execute_fetchall("SELECT result_json FROM reconstruction_windows ORDER BY id")]
        calls = [{"job": r[0], "seconds": r[1], "status": r[2], "model": r[3], "usage": json.loads(r[4]) if r[4] else None}
                 for r in ledger.execute("SELECT job,seconds,status,model,usage FROM calls")]
        async def count(table):
            return (await store.read_connection.execute_fetchall("SELECT COUNT(*) FROM " + table))[0][0]
        elapsed = (await store.read_connection.execute_fetchall("SELECT SUM(seconds) FROM reconstruction_runs"))[0][0]
        skipped = sum(w["status"] == "processed_no_memory" for w in windows)
        report = {"manifest": manifest, "windows": windows, "profiles": profiles, "calls": calls,
            "summary": {"expected_windows": len(selection["windows"]), "completed_windows": len(windows),
                "failed_windows": sum(w["status"] == "failed" for w in windows), "processed_no_memory": skipped,
                "skip_ratio": skipped / len(windows) if windows else 0, "documents": await count("documents"),
                "memory_atoms": await count("memory_atoms"), "elapsed_seconds": elapsed,
                "profile_rows": len(profiles), "calls": len(calls), "usage_missing_calls": sum(c["usage"] is None for c in calls)},
            "usage": {key: sum((c["usage"] or {}).get(key, 0) or 0 for c in calls)
                      for key in ("prompt_tokens", "completion_tokens", "total_tokens")},
            "state": {r[0]: json.loads(r[1]) for r in await store.read_connection.execute_fetchall("SELECT * FROM reconstruction_state")},
            "cost": {"actual_charge": None, "reason": "Provider responses expose usage, not billing; no actual invoice available."},
            "quality_note": "Automatic admission is not independent quality acceptance. No embeddings or production deployment."}
        Path(str(dest) + ".report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"summary": report["summary"], "usage": report["usage"]}), flush=True)
        await store.close()
        await provider.client.aclose()
        ledger.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", default=str(ROOT / "data/full_reconstructed.db"))
    parser.add_argument("--scope")
    parser.add_argument("--max-messages", type=int, choices=range(30, 41), default=35)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--model", default="gemini-3.8-flash")
    logging.disable(logging.CRITICAL)
    os.umask(0o077)
    asyncio.run(run(parser.parse_args()))
