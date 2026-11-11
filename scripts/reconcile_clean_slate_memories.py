"""Reconcile clean-slate exclusions in an offline private livingmemory.db.

Survey is read-only and prints counts only. Apply preserves exact metadata in
the same private database; rollback refuses to overwrite subsequent changes.
Use the normal plugin lifecycle after offline maintenance to refresh caches.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import uuid

try:
    from .audit_dirty_memories import classify, _DURABLE, _NOISE_KINDS, event_kind_counts
except ImportError:  # direct execution: python scripts/reconcile_clean_slate_memories.py
    from audit_dirty_memories import classify, _DURABLE, _NOISE_KINDS, event_kind_counts


REASON = "clean_slate_v290_migration"
_ERROR = re.compile(
    r"traceback|\b(?:error|exception|timeout|connection refused)\b|"
    r"请求失败|请求超时|调用失败|连接失败|服务不可用|报错", re.I
)
_RELATED = {"memory_atoms": "parent_memory_id", "graph_entries": "source_memory_id"}
# Current core.utils.event_memory.EVENT_KINDS substantive types, plus explicit
# legacy factual types. Keep this script runnable with the standard library only.
_VALID_SUBSTANTIVE_KINDS = {
    "self_report", "discussion", "decision", "plan",
    "preference", "fact", "milestone", "action",
}


def _metadata(raw: str | None) -> dict:
    value = json.loads(raw or "{}")
    if not isinstance(value, dict):
        raise ValueError("Metadata must be a JSON object")
    return value


def _classification_metadata(metadata: dict) -> dict:
    """Malformed/unknown event kinds cannot confer substantive protection."""
    normalized = dict(metadata)
    normalized["events"] = []
    events = metadata.get("events")
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict):
            continue
        summary = event.get("summary")
        summary = summary.strip() if isinstance(summary, str) else ""
        kind = event.get("kind")
        kind = kind.strip().lower() if isinstance(kind, str) else ""
        substantive_kind = kind in _VALID_SUBSTANTIVE_KINDS
        if summary or substantive_kind:
            normalized["events"].append({"summary": summary,
                                         "kind": kind if kind in _VALID_SUBSTANTIVE_KINDS | _NOISE_KINDS else "other"})
    return normalized


def scan(connection: sqlite3.Connection) -> tuple[list[dict], int]:
    """Return maintenance candidates internally, never print their content."""
    candidates = []
    malformed = 0
    for identity, text, raw in connection.execute("SELECT id, text, metadata FROM documents ORDER BY id"):
        try:
            metadata = _metadata(raw)
        except (ValueError, TypeError):
            malformed += 1
            continue
        if metadata.get("recall_exclusion_reason") != REASON:
            continue
        classification_metadata = _classification_metadata(metadata)
        reasons = set(classify(text, classification_metadata))
        events = classification_metadata["events"]
        event_text = " ".join(str(e.get("summary") or "") for e in events if isinstance(e, dict)) if isinstance(events, list) else ""
        summary = str(metadata.get("canonical_summary") or text or "")
        protected = bool(_DURABLE.search(summary + " " + event_text)) or bool(set(event_kind_counts(classification_metadata)) - _NOISE_KINDS)
        if not protected and _ERROR.search(summary + " " + event_text):
            reasons.add("error")
        noise = bool(reasons) and "mixed_content" not in reasons and not protected
        candidates.append({"id": identity, "before": raw, "metadata": metadata,
                           "noise": noise, "rules": sorted(reasons)})
    return candidates, malformed


def survey(connection: sqlite3.Connection) -> dict:
    candidates, malformed = scan(connection)
    noise = sum(item["noise"] for item in candidates)
    return {"candidates": len(candidates), "restore": len(candidates) - noise,
            "audit_noise": noise, "malformed_metadata_skipped": malformed,
            "rules": dict(Counter(rule for item in candidates for rule in item["rules"]))}


def _updated(metadata: dict, noise: bool) -> str:
    metadata = dict(metadata)
    metadata["recall_excluded"] = int(noise)
    if noise:
        metadata["recall_exclusion_reason"] = "audit_noise"
    else:
        metadata.pop("recall_exclusion_reason", None)
    return json.dumps(metadata, ensure_ascii=False)


def apply(connection: sqlite3.Connection, *, dry_run: bool = False) -> dict:
    """Serialize the scan, exact backups and all three tables' updates."""
    with connection:
        connection.execute("BEGIN" if dry_run else "BEGIN IMMEDIATE")
        result = survey(connection)
        candidates, _ = scan(connection)
        batch = uuid.uuid4().hex
        applied_at = datetime.now(timezone.utc).isoformat()
        if not dry_run:
            connection.execute("""CREATE TABLE IF NOT EXISTS clean_slate_recovery_backup (
                batch_id TEXT NOT NULL, doc_id INTEGER NOT NULL, original_metadata TEXT,
                applied_at TEXT NOT NULL, executed_metadata TEXT NOT NULL,
                PRIMARY KEY (batch_id, doc_id))""")
            connection.execute("""CREATE TABLE IF NOT EXISTS clean_slate_recovery_related_backup (
                batch_id TEXT NOT NULL, table_name TEXT NOT NULL, row_id INTEGER NOT NULL,
                parent_id INTEGER NOT NULL, original_metadata TEXT,
                executed_metadata TEXT NOT NULL, applied_at TEXT NOT NULL,
                PRIMARY KEY (batch_id, table_name, row_id))""")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        synced = Counter()
        for item in candidates:
            after = _updated(item["metadata"], item["noise"])
            if not dry_run:
                connection.execute("INSERT INTO clean_slate_recovery_backup (batch_id, doc_id, original_metadata, applied_at, executed_metadata) VALUES (?, ?, ?, ?, ?)",
                                   (batch, item["id"], item["before"], applied_at, after))
                connection.execute("UPDATE documents SET metadata=? WHERE id=?", (after, item["id"]))
            for table, parent in _RELATED.items():
                if table not in tables:
                    continue
                for row_id, before in connection.execute(f"SELECT id, metadata FROM {table} WHERE {parent}=?", (item["id"],)).fetchall():
                    try:
                        related_metadata = _metadata(before)
                    except (ValueError, TypeError):
                        raise ValueError(f"Invalid metadata in {table} row {row_id}; apply aborted") from None
                    # Preserve explicit row-level quarantine, including manual/audit
                    # exclusions. Only inherited clean-slate state belongs to this job.
                    if related_metadata.get("recall_exclusion_reason") not in (None, "", REASON):
                        continue
                    related_after = _updated(related_metadata, item["noise"])
                    if not dry_run:
                        connection.execute("INSERT INTO clean_slate_recovery_related_backup (batch_id, table_name, row_id, parent_id, original_metadata, executed_metadata, applied_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                           (batch, table, row_id, item["id"], before, related_after, applied_at))
                        connection.execute(f"UPDATE {table} SET metadata=? WHERE id=?", (related_after, row_id))
                    synced[table] += 1
        if dry_run:
            connection.rollback()
            return {**result, "dry_run": True, "processed": len(candidates), "synced": dict(synced)}
        return {**result, "batch_id": batch, "processed": len(candidates), "synced": dict(synced)}


def rollback(connection: sqlite3.Connection, batch: str, *, dry_run: bool = False) -> dict:
    """Validate every row before restoring any row, retaining later edits."""
    with connection:
        connection.execute("BEGIN" if dry_run else "BEGIN IMMEDIATE")
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE name='clean_slate_recovery_backup'").fetchone()
        if not exists:
            raise ValueError("Unknown or already rolled back batch")
        documents = connection.execute("SELECT doc_id, original_metadata, executed_metadata FROM clean_slate_recovery_backup WHERE batch_id=?", (batch,)).fetchall()
        if not documents:
            raise ValueError("Unknown or already rolled back batch")
        related = connection.execute("SELECT table_name, row_id, parent_id, original_metadata, executed_metadata FROM clean_slate_recovery_related_backup WHERE batch_id=?", (batch,)).fetchall()
        rows = [("documents", identity, None, before, after) for identity, before, after in documents] + related
        for table, identity, parent_id, before, after in rows:
            if table not in {"documents", *_RELATED}:
                raise ValueError("Invalid recovery backup table")
            parent = _RELATED.get(table)
            columns = f"metadata, {parent}" if parent else "metadata"
            current = connection.execute(f"SELECT {columns} FROM {table} WHERE id=?", (identity,)).fetchone()
            if current is None or current[0] != after or (parent and current[1] != parent_id):
                raise ValueError(f"{table} row {identity} changed after recovery; rollback aborted")
        if not dry_run:
            for table, identity, parent_id, before, after in rows:
                connection.execute(f"UPDATE {table} SET metadata=? WHERE id=?", (before, identity))
            connection.execute("DELETE FROM clean_slate_recovery_related_backup WHERE batch_id=?", (batch,))
            connection.execute("DELETE FROM clean_slate_recovery_backup WHERE batch_id=?", (batch,))
        return {"batch_id": batch, "rolled_back": len(documents), "related_rows": len(related), "dry_run": dry_run}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--survey", action="store_true")
    action.add_argument("--apply", action="store_true")
    action.add_argument("--rollback", metavar="BATCH_ID")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    mode = "rw" if (args.apply or args.rollback) and not args.dry_run else "ro"
    # mode=rw prevents accidentally creating an empty database for a mistyped path.
    with sqlite3.connect(args.db.resolve().as_uri() + "?mode=" + mode, uri=True) as connection:
        if args.rollback:
            result = rollback(connection, args.rollback, dry_run=args.dry_run)
        elif args.apply:
            result = apply(connection, dry_run=args.dry_run)
        else:
            with connection:
                connection.execute("BEGIN")
                result = {"read_only": True, **survey(connection)}
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
