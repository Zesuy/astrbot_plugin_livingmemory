"""Archive persistence on the ConversationStore transaction owner.

No FK to working sessions. FTS is optional and may be rebuilt from originals.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from functools import wraps
from pathlib import Path

import aiosqlite

from ..core.models.archive_models import ArchiveMessage


SCHEMA = """
CREATE TABLE IF NOT EXISTS archive_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS archive_scopes(
 conversation TEXT PRIMARY KEY, epoch INTEGER NOT NULL DEFAULT 0,
 generation INTEGER NOT NULL DEFAULT 0, cleared_at REAL);
CREATE TABLE IF NOT EXISTS archive_messages(
 id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL UNIQUE,
 native_key TEXT UNIQUE, conversation TEXT NOT NULL, sender_id TEXT NOT NULL,
 nickname TEXT NOT NULL, platform TEXT NOT NULL, role TEXT NOT NULL,
 text TEXT NOT NULL, sent_at REAL, saved_at REAL, ingested_at REAL NOT NULL,
 time_quality TEXT NOT NULL, state TEXT NOT NULL, mentions TEXT NOT NULL,
 reply_key TEXT, components TEXT NOT NULL, provenance TEXT NOT NULL,
 epoch INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 working_id INTEGER, projected INTEGER NOT NULL DEFAULT 0,
 indexed INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS archive_time ON archive_messages(conversation, sent_at, saved_at, id);
CREATE INDEX IF NOT EXISTS archive_speaker ON archive_messages(conversation,sender_id,id);
CREATE INDEX IF NOT EXISTS archive_reply ON archive_messages(conversation,reply_key);
CREATE TABLE IF NOT EXISTS archive_names(
 conversation TEXT NOT NULL, sender_id TEXT NOT NULL, nickname TEXT NOT NULL,
 uid TEXT NOT NULL, PRIMARY KEY(conversation,sender_id,nickname,uid));
CREATE TABLE IF NOT EXISTS archive_provenance(
 uid TEXT NOT NULL, source_key TEXT NOT NULL, PRIMARY KEY(uid,source_key));
CREATE TABLE IF NOT EXISTS archive_tombstones(uid TEXT PRIMARY KEY, conversation TEXT NOT NULL);
"""


def source_guarded(func):
    """Old source read/write routes share the clear/backup publication gate."""
    @wraps(func)
    async def wrapped(self,*args,**kwargs):
        guard = getattr(self,"source_access_guard",None)
        if guard is None:
            return await func(self,*args,**kwargs)
        async with guard.publication_lock:
            return await func(self,*args,**kwargs)
    return wrapped


class ArchiveStoreMixin:
    async def initialize_archive(self) -> None:
        await self.connection.executescript(SCHEMA)
        await self.connection.execute("INSERT OR IGNORE INTO archive_settings VALUES('instance',?)", (uuid.uuid4().hex,))
        await self.connection.commit()
        rows = await self.connection.execute_fetchall("SELECT conversation,epoch FROM archive_scopes")
        self.archive_epochs = {r[0]: r[1] for r in rows}
        self.archive_enabled = False
        self.publication_lock = asyncio.Lock()
        self.fts_ready = False
        try:
            await self.connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS archive_fts USING fts5(uid UNINDEXED, tokens)")
            await self.connection.commit()
            self.fts_ready = True
        except sqlite3.OperationalError:
            await self.connection.rollback()

    def accepted_epoch(self, conversation: str) -> int:
        return self.archive_epochs.get(conversation, 0)

    async def _archive_insert(self, record: ArchiveMessage) -> tuple[str, int | None, bool]:
        """Called only inside the owning write transaction."""
        state = await self.connection.execute_fetchall("SELECT epoch FROM archive_scopes WHERE conversation=?",(record.conversation,))
        persisted_epoch = state[0][0] if state else 0
        if record.epoch != persisted_epoch:
            raise ValueError("source_epoch_revoked")
        blocked = await self.connection.execute_fetchall("SELECT uid FROM archive_tombstones WHERE uid=?", (record.uid,))
        if blocked:
            raise ValueError("source_deleted")
        existing = await self.connection.execute_fetchall(
            "SELECT uid,text,working_id,projected FROM archive_messages WHERE uid=? OR (native_key IS NOT NULL AND native_key=?)",
            (record.uid, record.native_key))
        if existing:
            row = existing[0]
            if row[1] != record.text:
                raise ValueError("native_identity_content_conflict")
            await self.connection.execute("INSERT OR IGNORE INTO archive_provenance VALUES(?,?)", (row[0], record.provenance))
            return row[0], row[2], bool(row[3])
        await self.connection.execute("INSERT OR IGNORE INTO archive_scopes(conversation) VALUES(?)", (record.conversation,))
        await self.connection.execute("""INSERT INTO archive_messages
         (uid,native_key,conversation,sender_id,nickname,platform,role,text,sent_at,saved_at,
          ingested_at,time_quality,state,mentions,reply_key,components,provenance,epoch)
         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
         (record.uid,record.native_key,record.conversation,record.sender_id,record.nickname,
          record.platform,record.role,record.text,record.sent_at,record.saved_at,record.ingested_at,
          record.time_quality,record.state,json.dumps(record.mentions),record.reply_key,
          json.dumps(record.components,ensure_ascii=False),record.provenance,record.epoch))
        if record.sender_id not in ("", "unknown", "user", "assistant") and record.nickname:
            await self.connection.execute("INSERT OR IGNORE INTO archive_names VALUES(?,?,?,?)",
                                         (record.conversation,record.sender_id,record.nickname,record.uid))
        await self.connection.execute("INSERT OR IGNORE INTO archive_provenance VALUES(?,?)", (record.uid,record.provenance))
        return record.uid, None, False

    async def archive_record(self, record: ArchiveMessage) -> str:
        async with self.transaction():
            uid, _, _ = await self._archive_insert(record)
        return uid

    async def backfill_archive(self) -> dict:
        """Idempotent current-store migration before any working trim starts."""
        row = await self.connection.execute_fetchall("SELECT value FROM archive_settings WHERE key='instance'")
        instance = row[0][0]
        watermark = (await self.connection.execute_fetchall("SELECT COALESCE(MAX(id),0) FROM messages"))[0][0]
        checkpoint = (await self.connection.execute_fetchall("SELECT value FROM archive_settings WHERE key='working_checkpoint'"))
        position = int(checkpoint[0][0]) if checkpoint else 0
        count,invalid_metadata = 0,0
        while position < watermark:
            async with self.transaction():
                rows = await self.connection.execute_fetchall("SELECT * FROM messages WHERE id>? AND id<=? ORDER BY id LIMIT 250",(position,watermark))
                if not rows:
                    break
                for r in rows:
                    try:
                        meta = json.loads(r["metadata"] or "{}")
                    except (TypeError,ValueError):
                        meta = {}
                        invalid_metadata += 1
                    if not isinstance(meta,dict):
                        meta = {}
                    position = r["id"]
                    if self.accepted_epoch(r["session_id"]) or meta.get("archive_uid"):
                        continue
                    record = ArchiveMessage(uid=f"legacy:{instance}:messages:{r['id']}",conversation=r["session_id"],
                        sender_id=r["sender_id"],nickname=r["sender_name"] or "",platform=r["platform"] or "unknown",
                        text=r["content"],role=r["role"],saved_at=r["timestamp"] if r["timestamp"]>0 else None,
                        time_quality="legacy_saved",state="legacy_unknown",provenance=f"working:{instance}:{r['id']}")
                    await self._archive_insert(record)
                    await self.connection.execute("UPDATE archive_messages SET working_id=?,projected=1 WHERE uid=?",(r["id"],record.uid))
                    meta.update({"archive_uid":record.uid,"archive_epoch":0})
                    await self.connection.execute("UPDATE messages SET metadata=? WHERE id=?",(json.dumps(meta,ensure_ascii=False),r["id"]))
                    count += 1
                await self.connection.execute("INSERT OR REPLACE INTO archive_settings VALUES('working_checkpoint',?)",(str(position),))
        return {"migrated":count,"invalid_metadata":invalid_metadata,"watermark":watermark,"checkpoint":position,"time_quality":"legacy_saved"}

    async def import_retained_sources(self, path: str) -> dict:
        """Additional independent legacy copies; never dedup by local integer IDs."""
        if not Path(path).exists():
            return {"imported":0,"quarantined":0}
        async with aiosqlite.connect(Path(path).resolve().as_uri()+"?mode=ro",uri=True) as conn:
            try:
                rows = await conn.execute_fetchall("SELECT s.memory_id,s.source_json,d.metadata FROM memory_sources s JOIN documents d ON d.id=s.memory_id")
            except sqlite3.OperationalError:
                return {"imported":0,"quarantined":0}
        instance = (await self.connection.execute_fetchall("SELECT value FROM archive_settings WHERE key='instance'"))[0][0]
        count, isolated = 0, 0
        for memory_id, source_json, metadata in rows:
            try:
                sources = json.loads(source_json)
                parent = json.loads(metadata or "{}")
            except (TypeError,ValueError):
                isolated += 1
                continue
            if not isinstance(parent,dict) or not isinstance(sources,list):
                isolated += 1
                continue
            async with self.transaction():
                for offset, source in enumerate(sources if isinstance(sources,list) else []):
                    if not isinstance(source,dict):
                        isolated += 1
                        continue
                    conversation = str(source.get("session_id") or parent.get("session_id") or "")
                    if not conversation or conversation.startswith("livingmemory:") or self.accepted_epoch(conversation):
                        isolated += 1
                        continue
                    old_meta = source.get("metadata") or {}
                    if not isinstance(old_meta,dict):
                        isolated += 1
                        continue
                    uid = old_meta.get("archive_uid")
                    if uid:
                        # UID-backed sources must resolve canonically, not overwrite by imported text.
                        continue
                    uid = f"legacy:{instance}:retained:{memory_id}:{offset}"
                    saved = source.get("timestamp")
                    saved = float(saved) if isinstance(saved,(float,int)) and saved>0 else None
                    sender = str(source.get("sender_id") or "unknown")
                    record = ArchiveMessage(uid=uid,conversation=conversation,sender_id=sender,
                        nickname=str(source.get("sender_name") or ""),platform=str(source.get("platform") or "unknown"),
                        text=str(source.get("content") or ""),role=str(source.get("role") or "user"),saved_at=saved,
                        time_quality="legacy_saved" if saved else "unknown",state="legacy_unknown",
                        provenance=f"retained:{instance}:{memory_id}:{offset}")
                    await self._archive_insert(record)
                    count += 1
        return {"imported_or_existing":count,"quarantined":isolated,"possible_legacy_duplicates":True}

    async def index_pending(self, limit: int = 1000) -> int:
        if not self.fts_ready:
            return 0
        import jieba
        async with self.transaction():
            rows = await self.connection.execute_fetchall("SELECT uid,text FROM archive_messages WHERE indexed=0 LIMIT ?",(limit,))
            for uid,text in rows:
                tokens = " ".join(jieba.cut(text))
                await self.connection.execute("INSERT INTO archive_fts(uid,tokens) VALUES(?,?)",(uid,tokens))
                await self.connection.execute("UPDATE archive_messages SET indexed=1 WHERE uid=?",(uid,))
        return len(rows)

    async def filter_sources(self, sources: list[dict]) -> list[dict]:
        """Compatibility reads remain allowed until affected by an explicit clear."""
        result = []
        cleared = any(self.archive_epochs.values())
        for source in sources:
            meta = source.get("metadata") or {}
            conversation = str(source.get("session_id") or "")
            epoch = self.accepted_epoch(conversation)
            unknown_scope = not conversation or conversation.startswith("livingmemory:") or ":" not in conversation
            if (epoch and meta.get("archive_epoch",0) != epoch) or (cleared and unknown_scope):
                continue
            uid = meta.get("archive_uid")
            if uid:
                rows = await self.read_connection.execute_fetchall("SELECT uid FROM archive_messages WHERE uid=? AND conversation=?",(uid,conversation))
                if not rows:
                    continue
            result.append(source)
        return result

    async def clear_archive(self, conversation: str) -> dict:
        """Internal admin operation; not exposed to the LLM read-only tool."""
        task = asyncio.create_task(self._clear_archive_impl(conversation))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Finish the durable barrier and epoch cache before allowing reuse.
            await task
            raise

    async def _clear_archive_impl(self,conversation):
        async with self.publication_lock:
            async with self.transaction():
                epoch = self.accepted_epoch(conversation) + 1
                await self.connection.execute("INSERT OR IGNORE INTO archive_scopes(conversation) VALUES(?)",(conversation,))
                await self.connection.execute("INSERT OR IGNORE INTO archive_tombstones SELECT uid,conversation FROM archive_messages WHERE conversation=?",(conversation,))
                await self.connection.execute("DELETE FROM archive_names WHERE conversation=?",(conversation,))
                if self.fts_ready:
                    await self.connection.execute("DELETE FROM archive_fts WHERE uid IN(SELECT uid FROM archive_messages WHERE conversation=?)",(conversation,))
                await self.connection.execute("DELETE FROM archive_messages WHERE conversation=?",(conversation,))
                await self.connection.execute("DELETE FROM user_profiles WHERE scope_id=?",(conversation,))
                await self.connection.execute("DELETE FROM messages WHERE session_id=?",(conversation,))
                await self.connection.execute("UPDATE sessions SET message_count=0,metadata='{}' WHERE session_id=?",(conversation,))
                await self.connection.execute("UPDATE archive_scopes SET epoch=?,generation=generation+1,cleared_at=? WHERE conversation=?",(epoch,time.time(),conversation))
            self.archive_epochs[conversation] = epoch
        return {"conversation":conversation,"epoch":epoch,"raw_scope":"registered_and_accepted_before_barrier"}

    async def backup_conversations(self, destination: str) -> None:
        async with self.publication_lock:
            async with self._write_lock:
                async with aiosqlite.connect(destination) as target:
                    await self.connection.backup(target)
