"""Private, local-only pilot. No AstrBot process, QQ, old memory, or vector service.

Run with aiosqlite, httpx, jieba, pytz and tzdata installed. Credentials stay in the
CLIPROXYAPI_BASE_URL / CLIPROXYAPI_API_KEY environment. Output contains private
chat evidence and must remain in the ignored data directory.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import time
import types

ROOT = Path(__file__).resolve().parents[1]


def bootstrap():
    # Only the host API is shimmed. All memory, validation and profile code is real.
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("pilot")
    api.sp = types.SimpleNamespace()
    host = types.ModuleType("astrbot")
    host.api = api
    sys.modules.update({"astrbot": host, "astrbot.api": api})
    for name, attr in [("event", "AstrMessageEvent"), ("star", "Context")]:
        module = types.ModuleType("astrbot.api." + name)
        setattr(module, attr, type(attr, (), {}))
        sys.modules[module.__name__] = module
    # Avoid plugin registration and heavy manager imports in package __init__.
    for suffix in ["", ".core", ".core.processors", ".core.models", ".core.base", ".storage"]:
        name = "pilot_plugin" + suffix
        package = types.ModuleType(name)
        package.__path__ = [str(ROOT.joinpath(*suffix.strip(".").split("."))) if suffix else str(ROOT)]
        sys.modules[name] = package


class CloudProvider:
    def __init__(self, model):
        import httpx
        self.client = httpx.AsyncClient(timeout=120)
        self.model = model
        self.calls = []
        self.provider_config = {"id": "local-pilot"}

    async def text_chat(self, *, prompt, system_prompt):
        started = time.perf_counter()
        response = await self.client.post(
            os.environ["CLIPROXYAPI_BASE_URL"].rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + os.environ["CLIPROXYAPI_API_KEY"]},
            json={"model": self.model, "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}], "temperature": 0},
        )
        # Never print HTTP payloads, credentials, or raw chats on errors.
        if response.status_code != 200:
            raise RuntimeError("cloud_http_" + str(response.status_code))
        result = response.json()
        self.calls.append({"usage": result.get("usage"), "model": result.get("model"),
                           "seconds": time.perf_counter() - started})
        return types.SimpleNamespace(completion_text=result["choices"][0]["message"]["content"])


async def run(args):
    bootstrap()
    from pilot_plugin.core.processors.memory_processor import MemoryProcessor
    from pilot_plugin.core.models.conversation_models import Message
    from pilot_plugin.core.models.archive_models import ArchiveMessage
    from pilot_plugin.storage.conversation_store import ConversationStore
    from pilot_plugin.core.user_profile_runtime import UserProfileRuntime, EXTRACTION_POLICY

    source = Path(args.input).resolve()
    dest = Path(args.output).resolve()
    if not all(os.environ.get(k) for k in ("CLIPROXYAPI_BASE_URL", "CLIPROXYAPI_API_KEY")):
        raise RuntimeError("required_CLIPROXYAPI_environment_missing")
    if not dest.is_relative_to(ROOT / "data") or dest.exists():
        raise ValueError("output_must_be_new_and_inside_ignored_data")
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not 1 <= args.limit <= len(payload["windows"]):
        raise ValueError("limit_must_select_at_least_one_existing_window")
    provider = CloudProvider(args.model)
    processor = MemoryProcessor(llm_provider=provider, config={"summary_mode": "events"})
    store = ConversationStore(str(dest))
    await store.initialize()
    await store.connection.executescript("""
        CREATE TABLE documents(id INTEGER PRIMARY KEY,window_id INTEGER UNIQUE,content TEXT,metadata TEXT,importance REAL);
        CREATE TABLE memory_atoms(id INTEGER PRIMARY KEY,parent_memory_id INTEGER,atom_json TEXT);
        CREATE TABLE pilot_windows(id INTEGER PRIMARY KEY,result_json TEXT);
    """)
    config = {"user_profiles.enabled": True, "user_profiles.update_interval_seconds": 0,
              "user_profiles.bot_ids": ",".join(payload["rules"]["bot_ids"])}
    async def allowed(event):
        return True
    runtime = UserProfileRuntime(types.SimpleNamespace(
        store=store, config=config, enabled=True, may_capture=allowed))
    report = {"model": args.model, "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
              "selection": {k: v for k, v in payload.items() if k != "windows"},
              "windows": [], "errors": [], "storage": "pilot documents and atoms; official archive/profile schema; no embeddings"}
    started = time.perf_counter()
    try:
        for window in payload["windows"][:args.limit]:
            wid = window["id"]
            rows = window["messages"]
            scope = rows[0]["conversation"]
            count_before = len(provider.calls)
            for row in rows:
                record = ArchiveMessage(uid=row["uid"], conversation=scope, sender_id=row["sender_id"],
                    nickname=row["nickname"], platform=row["platform"], text=row["text"],
                    sent_at=row["sent_at"], time_quality="platform", components=json.loads(row["components"]),
                    mentions=json.loads(row["mentions"]), epoch=store.accepted_epoch(scope))
                await store.archive_record(record)
            result = {"id": wid, "tags": window["tags"], "messages": len(rows)}
            try:
                messages = [Message(id=r["id"],session_id=scope,role="user",content=r["text"],
                    sender_id=r["sender_id"],sender_name=r["nickname"],group_id=scope,
                    platform=r["platform"],timestamp=r["sent_at"],metadata={"archive_uid": r["uid"]}) for r in rows]
                content, metadata, importance = await processor.process_conversation(messages, is_group_chat=True)
                result.update(status=metadata.get("processing_status", "stored"), content=content, metadata=metadata)
                if content and result["status"] != "processed_no_memory":
                    cursor = await store.connection.execute("INSERT INTO documents(window_id,content,metadata,importance) VALUES(?,?,?,?)",
                        (wid,content,json.dumps(metadata,ensure_ascii=False),importance))
                    for atom in processor.classify_atoms_from_metadata(metadata,importance,session_id=scope):
                        await store.connection.execute("INSERT INTO memory_atoms(parent_memory_id,atom_json) VALUES(?,?)",
                            (cursor.lastrowid,json.dumps(asdict(atom),ensure_ascii=False,default=str)))
                    await store.connection.commit()
            except Exception as exc:
                result["status"] = "failed"
                report["errors"].append({"window":wid,"stage":"memory","type":type(exc).__name__})
            for user in dict.fromkeys(r["sender_id"] for r in rows):
                event = types.SimpleNamespace(unified_msg_origin=scope,
                    get_sender_id=lambda user=user:user, get_group_id=lambda:scope, get_self_id=lambda:"offline")
                async def extract(prompt):
                    return await processor._call_llm_with_retry(prompt, EXTRACTION_POLICY, max_retries=1)
                try:
                    await runtime.refresh(scope,user,event=event,extractor=extract)
                except Exception as exc:
                    report["errors"].append({"window":wid,"stage":"profile","type":type(exc).__name__})
            result["calls"] = provider.calls[count_before:]
            report["windows"].append(result)
            await store.connection.execute("INSERT INTO pilot_windows VALUES(?,?)",(wid,json.dumps(result,ensure_ascii=False)))
            await store.connection.commit()
            report["elapsed_seconds"] = time.perf_counter() - started
            Path(str(dest)+".report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
            print(json.dumps({"window":wid,"status":result["status"],"calls":len(provider.calls),"elapsed":round(report["elapsed_seconds"],1)}),flush=True)
        report["calls"] = provider.calls
        report["usage"] = {k:sum((c["usage"] or {}).get(k,0) or 0 for c in provider.calls)
                           for k in ["prompt_tokens","completion_tokens","total_tokens"]}
        report["usage_missing_calls"] = sum(c["usage"] is None for c in provider.calls)
        profiles = await store.read_connection.execute_fetchall("SELECT scope_id,user_id,profile_json FROM user_profiles")
        report["profiles"] = [{"scope_id":r[0],"user_id":r[1],"profile":json.loads(r[2])} for r in profiles]
        report["summary"] = await summarize(store, report)
        Path(str(dest)+".report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    finally:
        await store.close()
        await provider.client.aclose()


async def summarize(store, report):
    async def count(table):
        return (await store.read_connection.execute_fetchall("SELECT COUNT(*) FROM " + table))[0][0]
    profiles = [p["profile"] for p in report["profiles"]]
    skipped = sum(w["status"] == "processed_no_memory" for w in report["windows"])
    return {"windows": len(report["windows"]), "messages": sum(w["messages"] for w in report["windows"]),
            "processed_no_memory": skipped, "skip_ratio": skipped / len(report["windows"]),
            "stored_windows": sum(w["status"] == "stored" for w in report["windows"]),
            "documents": await count("documents"), "memory_atoms": await count("memory_atoms"),
            "profile_rows_including_empty_cursors": len(profiles),
            "single_slot_facts": sum(len(p["single_slots"]) for p in profiles),
            "set_slot_facts": sum(sum(map(len,p["set_slots"].values())) for p in profiles),
            "preferred_names": sum(bool(p["identity"]["preferred_name"]) for p in profiles),
            "real_names": sum(bool(p["identity"]["real_name"]) for p in profiles),
            "aliases": sum(len(p["identity"]["aliases"]) for p in profiles),
            "quality_note": "Stored means automatic quality-gate acceptance; independent human-quality review remains required."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input",default=str(ROOT/"data/pilot_slices.json"))
    parser.add_argument("--output",default=str(ROOT/"data/pilot_reconstructed.db"))
    parser.add_argument("--model",default="gemini-3.8-flash")
    parser.add_argument("--limit",type=int,default=50)
    logging.disable(logging.CRITICAL)
    asyncio.run(run(parser.parse_args()))
