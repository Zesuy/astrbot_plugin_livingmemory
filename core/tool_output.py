"""Snapshots for explicit tool-output notifications, separate from sending."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from .models.archive_models import ArchiveMessage


@dataclass(frozen=True)
class ToolOutputSnapshot:
    owner: Any
    output_id: str
    conversation: str
    platform: str
    sender_id: str
    epoch: int
    text: str
    components_json: str

    def record(self, completed_at: float) -> ArchiveMessage:
        return ArchiveMessage(
            uid="tool-output:" + self.output_id,
            conversation=self.conversation, platform=self.platform,
            sender_id=self.sender_id, nickname=self.sender_id,
            role="assistant", text=self.text, saved_at=completed_at,
            time_quality="send_call_completed", state="send_call_returned",
            components=json.loads(self.components_json),
            provenance="tool_output_notification:" + self.output_id,
            epoch=self.epoch,
        )


def snapshot_output(owner, event, epoch, *, tool_name, items, delivery_mode):
    if delivery_mode not in {"single", "merged_forward"}:
        raise ValueError("unsupported_delivery_mode")
    if not isinstance(tool_name, str) or not tool_name or len(tool_name) > 120:
        raise ValueError("invalid_tool_name")
    if not isinstance(items, list) or not 1 <= len(items) <= 10:
        raise ValueError("invalid_output_items")
    if delivery_mode == "single" and len(items) != 1:
        raise ValueError("single_requires_one_item")
    normalized = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("invalid_output_item")
        if any(not isinstance(item.get(key), str) for key in ("item_id", "title", "body")):
            raise ValueError("invalid_output_item_fields")
        if not item["body"] or len(item["body"]) > 100000:
            raise ValueError("invalid_output_body")
        normalized.append({key: item[key] for key in ("item_id", "title", "body")})
    self_id = str(event.get_self_id() or "")
    if not self_id:
        raise ValueError("missing_bot_identity")
    output_id = uuid.uuid4().hex
    obj = getattr(event, "message_obj", None)
    trigger_id = str(getattr(obj, "message_id", "") or "")
    metadata = {
        "type": "tool_output", "output_id": output_id,
        "tool_name": tool_name, "delivery_mode": delivery_mode,
        "delivery_status": "send_call_returned",
        "trigger_sender_id": str(event.get_sender_id()),
        "trigger_sender_name": str(event.get_sender_name() or ""),
        "trigger_message_id": trigger_id or None,
        "items": [{"item_id": i["item_id"], "title": i["title"]} for i in normalized],
    }
    components = [{"type": "plain", "text": normalized[0]["body"]}] if delivery_mode == "single" else [
        {"type": "nodes", "nodes": [{"sender_id": self_id, "text": i["body"]} for i in normalized]}]
    return ToolOutputSnapshot(
        owner=owner, output_id=output_id,
        conversation=str(event.unified_msg_origin), platform=str(event.get_platform_name()),
        sender_id=self_id, epoch=epoch,
        text="\n\n".join(i["body"] for i in normalized),
        components_json=json.dumps([*components, metadata], ensure_ascii=False),
    )
