"""Conservative recall eligibility and observed account-name evidence."""

import json
import re
from typing import Any


_NO_INPUT = re.compile(
    r"(?:本次|此次)?(?:未提供(?:具体的?|实际的?)?|无(?:具体的?|实际的?)?)"
    r"(?:群聊)?(?:对话|消息|聊天|记录|内容)+"
    r"(?:内容|记录|输入|提供)?[。.!！]?"
    r"(?:[，,；;](?:无|未)(?:实际|相关)?(?:发生)?"
    r"(?:对话|事件|互动|内容|记忆|事实|总结|记录|生成|相关|可供|可)*"
    r"(?:总结|提供)?[。.!！]?)?"
)


def is_non_substantive(content: Any, metadata: Any) -> bool:
    """Require both empty structured fields and an entire no-input declaration.

    Low quality alone, a short summary, or a quoted no-input phrase is insufficient.
    Explicit False/0 provides a reversible override for audited legacy records.
    SQLite audit tools may encode boolean flags as integer 0/1.
    """
    metadata = metadata if isinstance(metadata, dict) else {}
    if metadata.get("recall_excluded") in (True, False) and "recall_excluded" in metadata:
        return bool(metadata["recall_excluded"])
    if metadata.get("summary_quality") != "low":
        return False
    if any(metadata.get(key) for key in ("topics", "key_facts", "participants")):
        return False
    text = str(metadata.get("canonical_summary") or content or "").strip()
    return not text or _NO_INPUT.fullmatch(text) is not None


def merge_account_metadata(previous: dict, incoming: dict, value: str) -> dict:
    """Accumulate names only inside the same caller-verified account node."""
    merged = {**previous, **incoming}
    merged["is_bot"] = bool(previous.get("is_bot") or incoming.get("is_bot"))
    names = []
    for source in (previous.get("aliases"), incoming.get("aliases"), [value]):
        if not isinstance(source, list):
            continue
        for name in source:
            if isinstance(name, str) and name.strip() and name.strip() not in names:
                names.append(name.strip())
    merged["aliases"] = names
    observations: dict[str, dict] = {}
    for source in (previous.get("alias_observations"), incoming.get("alias_observations")):
        for observation in source if isinstance(source, list) else []:
            if not isinstance(observation, dict):
                continue
            scope = (observation.get("session_id"), observation.get("persona_id"), observation.get("adapter_id"))
            key = json.dumps(scope, ensure_ascii=False)
            record = observations.setdefault(key, {
                "session_id": scope[0], "persona_id": scope[1], "aliases": [],
                **({"adapter_id": scope[2]} if scope[2] else {}),
            })
            for name in observation.get("aliases", []) if isinstance(observation.get("aliases"), list) else []:
                if isinstance(name, str) and name.strip() and name.strip() not in record["aliases"]:
                    record["aliases"].append(name.strip())
    merged["alias_observations"] = list(observations.values())
    return merged


def name_in_query(name: str, query: str) -> bool:
    """Match a whole observed alias, with word boundaries for ASCII names/IDs."""
    if not name:
        return False
    pattern = re.escape(name)
    if name[0].isascii() and name[0].isalnum():
        pattern = r"(?<![A-Za-z0-9_])" + pattern
    if name[-1].isascii() and name[-1].isalnum():
        pattern += r"(?![A-Za-z0-9_])"
    return re.search(pattern, query, re.IGNORECASE) is not None
