"""Audit a local livingmemory.db without printing private text.

Default is read-only. Use --apply to soft-exclude matching documents, and
--rollback BATCH to undo that batch. Undo data stays in the same private DB.
Rules are conservative heuristics: review the reported IDs before applying.
Mixed-content candidates are reported for manual review and never soft-excluded.
No document, atom, vector or source data is deleted. Restart/reload the plugin
through its normal lifecycle after offline maintenance to refresh caches.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sqlite3
import uuid


_RULES = {"low_quality", "empty", "reaction", "banter", "bot_apology",
          "pure_banter_or_bot_noise", "mixed_content"}
_NOISE_KINDS = {"joke", "bot_reply", "other"}
_REACTION = re.compile(r"\[(?:表情|图片|动画表情|image|face|emoji)[^\]]*\]|^[\W_]+$", re.I)
_BANTER = re.compile(r"(?<!\w)(?:哈哈+|呵呵+|[哈嘿嘻]{2,}|[6６]{2,})(?!\w)|笑死|绷不住|调侃|闲聊|刚醒又困|排行榜.{0,30}(?:争辩|争论|打脸)")
_APOLOGY = re.compile(r"(?:Bot|机器人|助手|鼠鼠|我)(?:表示|承认|回复|说|随后|最终|再次|主动)*[：:,，\s]*(?:对不起|抱歉|道歉|认错|错了|搞错了|弄错了|理解错了)", re.I)
# Preserve durable facts even when an event was assigned a generic noise kind.
_DURABLE = re.compile(r"计划|决定|约定|备份|每周|发布|修复|喜欢|偏好|安排|截止|负责|技术|原理|算法|代码|配置|定理|教程|规则|跑团|开发|编译|部署|考研|读博|求职|招聘")


def event_kind_counts(metadata: dict) -> dict[str, int]:
    """Count structured events without exposing their private summaries."""
    events = metadata.get("events")
    if not isinstance(events, list):
        return {}
    return dict(Counter(
        str(event.get("kind") or "unknown") if isinstance(event, dict) else "unknown"
        for event in events
    ))


def classify(text: str, metadata: dict) -> list[str]:
    """Find noise in narrative summaries, protecting any accompanying facts."""
    reasons = []
    if metadata.get("summary_quality") == "low":
        reasons.append("low_quality")
    text = str(metadata.get("canonical_summary") or text or "").strip()
    if not text:
        reasons.append("empty")
    for rule, pattern in (("reaction", _REACTION), ("banter", _BANTER), ("bot_apology", _APOLOGY)):
        if pattern.search(text):
            reasons.append(rule)
    counts = event_kind_counts(metadata)
    events = metadata.get("events")
    event_text = " ".join(str(event.get("summary") or "") for event in events if isinstance(event, dict)) if isinstance(events, list) else ""
    protected = bool(_DURABLE.search(text + " " + event_text)) or bool(set(counts) - _NOISE_KINDS)
    if counts and not protected and set(counts) <= _NOISE_KINDS:
        reasons.append("pure_banter_or_bot_noise")
    if reasons and protected:
        reasons.append("mixed_content")
    elif counts and protected and set(counts) & _NOISE_KINDS:
        reasons.append("mixed_content")
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
        all_reasons = set(classify(text, metadata))
        reasons = sorted(all_reasons & rules)
        if reasons:
            # Keep protection visible even for a narrowly selected --rules list.
            if "mixed_content" in all_reasons and "mixed_content" not in reasons:
                reasons.append("mixed_content")
            found.append({"id": identity, "rules": reasons, "before": raw,
                          "event_kind_counts": event_kind_counts(metadata)})
    return found


def apply(connection: sqlite3.Connection, rules: set[str], ids: set[int] | None = None) -> tuple[str, int]:
    """Serialize scan and writes; keep exact metadata bytes for reversible rollback."""
    batch = uuid.uuid4().hex
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        matches = scan(connection, rules)
        matches = [item for item in matches if "mixed_content" not in item["rules"]]
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
            result = {"read_only": True, "candidates": [
                {"id": item["id"], "rules": item["rules"], "event_kind_counts": item["event_kind_counts"]}
                for item in matches]}
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
