"""Source records, not reconstructed facts or personality memories."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ArchiveMessage:
    uid: str
    conversation: str
    sender_id: str
    nickname: str
    platform: str
    text: str
    role: str = "user"
    native_key: str | None = None
    sent_at: float | None = None
    saved_at: float | None = None
    ingested_at: float = field(default_factory=time.time)
    time_quality: str = "unknown"
    state: str = "observed"
    mentions: list[str] = field(default_factory=list)
    reply_key: str | None = None
    components: list[dict[str, Any]] = field(default_factory=list)
    provenance: str = "platform_capture"
    epoch: int = 0

    def metadata(self) -> dict[str, Any]:
        return {"archive_uid": self.uid, "archive_epoch": self.epoch, "time_quality":self.time_quality,
                "archive_record": self.__dict__.copy()}


def capture_event(event: Any, epoch: int = 0) -> ArchiveMessage:
    """Use native input only; never capture req.prompt or a generated reply."""
    cached = event.get_extra("_lm_archive_record", None)
    if isinstance(cached, ArchiveMessage):
        return cached
    obj = event.message_obj
    conversation = str(event.unified_msg_origin)
    platform = str(event.get_platform_name())
    raw = getattr(obj, "raw_message", None)
    raw = raw if isinstance(raw, dict) else {}
    native_id = str(getattr(obj, "message_id", None) or raw.get("message_id") or "")
    # OneBot IDs are adapter observations, scoped to this bot and conversation.
    # Use raw bot/group IDs, not the configured connection display name.
    self_id = str(event.get_self_id())
    group = str(event.get_group_id() or event.get_sender_id())
    namespace = json.dumps([platform, self_id, bool(event.get_group_id()), group], ensure_ascii=False)
    native_key = namespace + ":" + native_id if native_id else None
    uid = "native:" + hashlib.sha256(native_key.encode()).hexdigest() if native_key else "capture:" + uuid.uuid4().hex
    stamp = raw.get("time", getattr(obj, "timestamp", None))
    try:
        stamp = float(stamp)
        if not 946684800 <= stamp <= time.time() + 86400:
            stamp = None
    except (TypeError, ValueError):
        stamp = None
    components, mentions, text, reply_key = [], [], [], None
    for component in event.get_messages():
        kind_value = getattr(component,"type",component.__class__.__name__)
        kind = str(getattr(kind_value,"value",kind_value)).lower()
        data: dict[str, Any] = {"type": kind}
        if kind == "plain":
            data["text"] = str(getattr(component, "text", ""))
            text.append(data["text"])
        elif kind == "at":
            account = str(getattr(component, "qq", ""))
            data["account_id"] = account
            if account:
                mentions.append(account)
            text.append("[At:" + account + "]")
        elif kind == "reply":
            target = str(getattr(component, "id", "") or "")
            data.update({"native_id": target, "text": str(getattr(component, "message_str", "") or "")})
            reply_key = namespace + ":" + target if target else None
            text.append("[Reply: " + data["text"] + "]")
        else:
            # Media placeholders do not assert unobserved visual contents.
            text.append("[" + kind + "]")
        components.append(data)
    original = " ".join(text).strip() or str(event.get_message_str() or "")
    sender = str(event.get_sender_id())
    result = ArchiveMessage(uid=uid, conversation=conversation, sender_id=sender,
                            nickname=str(event.get_sender_name() or ""), platform=platform,
                            text=original, native_key=native_key, sent_at=stamp,
                            time_quality="platform_sent" if stamp else "unknown",
                            mentions=mentions, reply_key=reply_key, components=components,
                            role="assistant" if sender == self_id else "user", epoch=epoch)
    event.set_extra("_lm_archive_record", result)
    return result
