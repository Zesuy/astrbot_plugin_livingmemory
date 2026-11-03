"""Audit a local livingmemory.db without printing private text.

Default is read-only. Use --apply to soft-exclude matching documents, and
--rollback BATCH to undo that batch. Undo data stays in the same private DB.
Rules are conservative heuristics: review the reported IDs before applying.
No document, atom, vector or source data is deleted. Restart/reload the plugin
through its normal lifecycle after offline maintenance to refresh caches.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sqlite3
import uuid


_RULES = {"low_quality", "empty", "reaction", "banter", "bot_apology"}
_REACTION = re.compile(r"(?:\[(?:表情|图片|动画表情|image|face|emoji)[^\]]*\]|[\W_])+", re.I)
_BANTER = re.compile(r"(?:哈哈+|呵呵+|笑死(?:了)?|绷不住(?:了)?|乐(?:死|了)|[哈嘿嘻]+|[6６]+|[?？!！]+)[。.!！~～\s]*")
_APOLOGY = re.compile(r"(?:Bot|机器人|助手|鼠鼠|我)(?:表示|承认|回复|说)?[：:,，\s]*(?:对不起|抱歉|认错|错了|搞错了|弄错了|理解错了)[。.!！\s]*", re.I)


def classify(text: str, metadata: dict) -> list[str]:
    """Whole-text noise patterns avoid quarantining facts merely quoting a joke."""
    reasons = []
    if metadata.get("summary_quality") == "low":
        reasons.append("low_quality")
    text = str(metadata.get("canonical_summary") or text or "").strip()
    if not text:
        reasons.append("empty")
    elif _REACTION.fullmatch(text):
        reasons.append("reaction")
    elif _BANTER.fullmatch(text):
        reasons.append("banter")
    elif _APOLOGY.fullmatch(text):
        reasons.append("bot_apology")
    return reasons


def scan(connection: sqlite3.Connection, rules: set[str]) -> list[dict]:
    found = []
    for identity, text, raw in connection.execute("SELECT id, text, metadata FROM documents ORDER BY id"):
        try:
            metadata = json.loads(raw or "{}")
        except (ValueError, TypeError):
            continue  # malformed metadata must never be silently overwritten
        if not isinstance(metadata, dict) or metadata.get("recall_excluded") in (True, 1):
            continue
        reasons = sorted(set(classify(text, metadata)) & rules)
        if reasons:
            found.append({"id": identity, "rules": reasons, "before": raw})
    return found


def apply(connection: sqlite3.Connection, rules: set[str], ids: set[int] | None = None) -> tuple[str, int]:
    """Serialize scan and writes; keep exact metadata bytes for reversible rollback."""
    batch = uuid.uuid4().hex
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        matches = scan(connection, rules)
        if ids is not None:
            matches = [item for item in matches if item["id"] in ids]
        connection.execute("CREATE TABLE IF NOT EXISTS dirty_memory_audit (batch TEXT NOT NULL, document_id INTEGER NOT NULL, before_metadata TEXT, after_metadata TEXT NOT NULL, PRIMARY KEY(batch, document_id))")
        for item in matches:
            metadata = json.loads(item["before"] or "{}")
            metadata["recall_excluded"] = 1
            metadata["recall_exclusion_reason"] = "dirty_memory_audit:" + ",".join(item["rules"])
            after = json.dumps(metadata, ensure_ascii=False)
            connection.execute("INSERT INTO dirty_memory_audit VALUES (?, ?, ?, ?)", (batch, item["id"], item["before"], after))
            connection.execute("UPDATE documents SET metadata=? WHERE id=?", (after, item["id"]))
    return batch, len(matches)


def rollback(connection: sqlite3.Connection, batch: str) -> int:
    """Refuse the entire rollback if later edits would be overwritten."""
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute("SELECT document_id, before_metadata, after_metadata FROM dirty_memory_audit WHERE batch=?", (batch,)).fetchall()
        if not rows:
            raise ValueError("Unknown or already rolled back batch")
        for identity, before, after in rows:
            current = connection.execute("SELECT metadata FROM documents WHERE id=?", (identity,)).fetchone()
            if current is None or current[0] != after:
                raise ValueError(f"Document {identity} changed after audit; rollback aborted")
        for identity, before, after in rows:
            connection.execute("UPDATE documents SET metadata=? WHERE id=?", (before, identity))
        connection.execute("DELETE FROM dirty_memory_audit WHERE batch=?", (batch,))
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true")
    action.add_argument("--rollback", metavar="BATCH")
    parser.add_argument("--rules", nargs="+", choices=sorted(_RULES), default=sorted(_RULES))
    parser.add_argument("--ids", nargs="+", type=int, help="Only apply to these reviewed document IDs")
    args = parser.parse_args()
    mode = "rw" if args.apply or args.rollback else "ro"
    # mode=rw never creates a missing/typo database path.
    with sqlite3.connect(args.database.resolve().as_uri() + "?mode=" + mode, uri=True) as connection:
        if args.rollback:
            result = {"rolled_back": rollback(connection, args.rollback)}
        elif args.apply:
            batch, count = apply(connection, set(args.rules), set(args.ids) if args.ids else None)
            result = {"batch": batch, "excluded": count}
        else:
            matches = scan(connection, set(args.rules))
            result = {"read_only": True, "candidates": [{"id": item["id"], "rules": item["rules"]} for item in matches]}
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
