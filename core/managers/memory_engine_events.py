"""Time-scoped event evidence, independent of semantic top-k and write time."""
import json
import math
from pathlib import Path

import aiosqlite

from ..models.conversation_models import Message
from ..memory_scope import adapter_sql
from ..utils.json_utils import safe_json_dict
from ..utils.event_memory import CHAT_TIMEZONE
from datetime import datetime


class MemoryEngineEventsMixin:
    async def recall_events_window(self, window, session_id, persona_id=None, include_source=False):
        """Verify event sources against scoped archived messages before returning.

        Legacy summaries are not promoted to precise events. Their archive can
        provide exact messages instead; all missing/truncated coverage is explicit.
        """
        result = {"evidence_type": "time_scoped_chat_events", "window": window.to_dict(),
                  "coverage": {}, "events": [], "raw_messages": []}
        if window.end <= window.start or self.db_connection is None:
            result["coverage"] = {"status": "empty_or_future_window", "complete": False}
            return result
        metadata_sql = "CASE WHEN json_valid(metadata) THEN metadata ELSE '{}' END"
        filters = [f"COALESCE(json_extract({metadata_sql}, '$.status'), 'active') = 'active'",
                   f"COALESCE(json_extract({metadata_sql}, '$.recall_excluded'), 0) != 1"]
        params = []
        if session_id is not None:
            filters.append(f"json_extract({metadata_sql}, '$.session_id') = ?")
            params.append(session_id)
        if persona_id is not None:
            filters.append(f"json_extract({metadata_sql}, '$.persona_id') = ?")
            params.append(persona_id)
        ownership, ownership_params = adapter_sql(metadata_sql)
        filters.append(ownership)
        params.extend(ownership_params)
        cursor = await self.db_connection.execute(
            "SELECT id, metadata FROM documents WHERE " + " AND ".join(filters), params)
        memories = [(int(row[0]), safe_json_dict(row[1])) for row in await cursor.fetchall()]
        archive = Path(self.db_path).parent / "conversations.db"
        # Do not broaden a global/user scope into other groups' raw archives.
        if not session_id or not memories:
            result["coverage"] = {"status": "archive_or_memory_scope_unavailable", "complete": False}
            return result
        messages, archive_ids = [], set()
        archive_available = False
        try:
            if not archive.exists():
                raise aiosqlite.OperationalError("No archive")
            async with aiosqlite.connect(archive.as_uri() + "?mode=ro", uri=True) as db:
                db.row_factory = aiosqlite.Row
                scope = (session_id, window.start, window.end)
                count_cursor = await db.execute(
                    "SELECT id FROM messages WHERE session_id=? AND timestamp>=? AND timestamp<? AND role IN ('user','assistant')", scope)
                archive_ids = {int(row[0]) for row in await count_cursor.fetchall()}
                raw_cursor = await db.execute(
                    "SELECT * FROM messages WHERE session_id=? AND timestamp>=? AND timestamp<? AND role IN ('user','assistant') ORDER BY timestamp,id LIMIT 2000", scope)
                messages = [Message.from_dict(dict(row)) for row in await raw_cursor.fetchall()]
                archive_available = True
        except aiosqlite.Error:
            pass
        by_id = {int(message.id): message for message in messages}
        # Only sources attached to the already scoped, active parents may recover
        # deleted archive rows. Never invent missing timestamps or account IDs.
        retained, conflicts = {}, set()
        for memory_id, _ in memories:
            try:
                rows = await self.get_memory_source(memory_id)
            except aiosqlite.Error:
                rows = []
            for row in rows:
                if not isinstance(row, dict) or row.get("session_id") != session_id:
                    continue
                try:
                    mid, timestamp = row["id"], float(row["timestamp"])
                    if (type(mid) is not int or mid <= 0 or not math.isfinite(timestamp)
                            or not window.start <= timestamp < window.end
                            or row.get("role") not in ("user", "assistant")
                            or not row.get("sender_id") or "content" not in row):
                        continue
                    message = Message.from_dict({**row, "timestamp": timestamp})
                except (KeyError, TypeError, ValueError):
                    continue
                if mid in retained and retained[mid].to_dict() != message.to_dict():
                    conflicts.add(mid)
                retained[mid] = message
        for mid in conflicts:
            retained.pop(mid, None)
        retained_only = set(retained) - archive_ids
        for mid, message in retained.items():
            by_id.setdefault(mid, message)  # Live archive wins on conflicting copies.
        messages = sorted(by_id.values(), key=lambda message: (message.timestamp, message.id))
        total = len(archive_ids) + len(retained_only)
        used_ids = set()
        seen = set()
        partial = 0
        for memory_id, metadata in memories:
            stored = metadata.get("events", [])
            for event in stored if isinstance(stored, list) else []:
                if not isinstance(event, dict) or event.get("source_verified") is not True:
                    continue
                ids = event.get("source_message_ids", [])
                if not isinstance(ids, list) or not ids or any(type(i) is not int for i in ids):
                    continue
                inside = [i for i in ids if i in by_id]
                if not inside:
                    continue
                if len(inside) != len(ids):
                    # A multi-message event crossing the boundary must not leak
                    # its out-of-window summary into an exact time overview.
                    partial += 1
                    continue
                key = (event.get("kind"), tuple(sorted(ids)), str(event.get("summary", "")))
                if key in seen or not key[2]:
                    continue
                seen.add(key)
                linked = sorted((by_id[i] for i in ids), key=lambda message: message.timestamp)
                speakers = list({(str(message.sender_id), message.role == "assistant"): {
                    "sender_id": str(message.sender_id), "sender_name": message.sender_name,
                    "is_bot": bool(message.role == "assistant" or message.metadata.get("is_bot_message")),
                } for message in linked}.values())
                result["events"].append({
                    "event_id": event.get("event_id"), "memory_id": memory_id,
                    "kind": event.get("kind", "other"), "summary": key[2],
                    "observed_start": datetime.fromtimestamp(linked[0].timestamp, CHAT_TIMEZONE).isoformat(),
                    "observed_end": datetime.fromtimestamp(linked[-1].timestamp, CHAT_TIMEZONE).isoformat(),
                    "speakers": speakers, "source_message_ids": ids,
                })
                if include_source:
                    result["events"][-1]["source_messages"] = [{
                        "message_id": message.id, "sender_id": str(message.sender_id),
                        "sender_name": message.sender_name,
                        "observed_at": datetime.fromtimestamp(message.timestamp, CHAT_TIMEZONE).isoformat(),
                        "content": Message.content_to_text(message.content),
                    } for message in linked]
                used_ids.update(ids)
        result["events"].sort(key=lambda event: (event["observed_start"], str(event["event_id"])))
        candidates = result["events"]
        selected, event_size = [], 0
        for event in candidates[:80]:
            size = len(json.dumps(event, ensure_ascii=False))
            if event_size + size > 48000:
                break
            selected.append(event)
            event_size += size
        result["events"] = selected
        omitted_events = len(candidates) - len(selected)
        used_ids = {i for event in selected for i in event["source_message_ids"]}
        budget = 50000 - len(json.dumps(result["events"], ensure_ascii=False))
        returned_ids = set(used_ids)
        text_truncated = 0
        for message in messages:
            if message.id in used_ids:
                continue
            text = Message.content_to_text(message.content)
            clipped = len(text) > 1600
            raw = {"message_id": message.id, "observed_at": datetime.fromtimestamp(message.timestamp, CHAT_TIMEZONE).isoformat(),
                   "sender_id": str(message.sender_id), "sender_name": message.sender_name,
                   "is_bot": bool(message.role == "assistant" or message.metadata.get("is_bot_message")),
                   "content": text[:1600], "text_truncated": clipped}
            size = len(json.dumps(raw, ensure_ascii=False))
            if size > budget:
                break
            budget -= size
            text_truncated += int(clipped)
            result["raw_messages"].append(raw)
            returned_ids.add(message.id)
        result["coverage"] = {
            "status": "available" if total else ("no_archived_messages" if archive_available else "archive_unavailable"),
            "archive_available": archive_available,
            "archived_messages_in_window": len(archive_ids),
            "retained_source_messages_in_window": len(retained),
            "retained_source_only_messages": len(retained_only),
            "known_messages_in_window": total,
            "conflicting_retained_message_ids": len(conflicts),
            "messages_with_event_sources": len(used_ids),
            "represented_messages": len(returned_ids),
            "omitted_messages": total - len(returned_ids),
            "partial_or_missing_source_events": partial,
            "omitted_structured_events": omitted_events,
            "truncated_message_texts": text_truncated,
            "complete": archive_available and not retained_only and not conflicts and len(returned_ids) == total and text_truncated == 0,
            "note": "Coverage describes available archives and retained citations. Recovered citations do not prove completeness of a deleted archive or real-world events.",
        }
        return result
