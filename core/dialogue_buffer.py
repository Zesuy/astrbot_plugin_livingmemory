"""Bounded, process-local group dialogue snapshots with bot spam quota protection."""

import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from itertools import count

_EVENT_IDS = count()


def sanitize_display_name(name: str, max_length: int = 24) -> str:
    """Render one bounded name, escaping delimiters reserved by context framing."""
    text = "".join(c for c in str(name or "") if c.isprintable())
    text = " ".join(text.split()).translate(str.maketrans("[]():<>", "［］（）：＜＞"))
    return (text or "群友")[:max(1, max_length)]


@dataclass(frozen=True)
class DialogueEntry:
    message_id: str | None
    timestamp: float
    sender_name: str
    sender_id: str
    content: str
    role: str = "user"
    mention_ids: tuple[str, ...] = ()
    reply_sender_id: str | None = None
    reply_text: str | None = None
    is_reply_to_self: bool = False
    reply_sender_name: str | None = None
    is_bot: bool = False

    @property
    def formatted_time(self) -> str:
        return datetime.fromtimestamp(self.timestamp).strftime("%H:%M")


class DialogueBuffer:
    """Each session has a bounded window with human protection quota against bot spam."""

    def __init__(self, max_messages: int = 50, *, max_sessions: int = 1000):
        self.max_messages = min(100, max(10, int(max_messages)))
        self.max_sessions = max(1, int(max_sessions))
        self.max_bot_messages = max(1, int(self.max_messages * 0.1))
        self.min_human_messages = self.max_messages - self.max_bot_messages
        self._sessions: OrderedDict[str, list[DialogueEntry]] = OrderedDict()

    def add_message(
        self,
        session_id,
        sender_name,
        sender_id,
        content,
        message_id=None,
        timestamp=None,
        *, role="user", mention_ids=(), reply_sender_id=None, reply_text=None,
        is_reply_to_self=False, reply_sender_name=None, is_bot: bool = False,
    ):
        if not session_id or not isinstance(content, str) or not content.strip():
            return
        message_id = str(message_id) if message_id not in (None, "") else None
        stamp = timestamp.timestamp() if isinstance(timestamp, datetime) else timestamp
        try:
            stamp = time.time() if stamp is None else float(stamp)
            if stamp > 100_000_000_000:
                stamp /= 1000
            if not math.isfinite(stamp):
                stamp = time.time()
            datetime.fromtimestamp(stamp)
        except (ValueError, TypeError, OverflowError, OSError):
            stamp = time.time()
        entries = self._sessions.setdefault(session_id, [])
        self._sessions.move_to_end(session_id)
        if message_id is not None and any(e.message_id == message_id for e in entries):
            return

        # 机器人消息单条最多截断保留 200 字符，防长报表污染滑窗
        text = content.strip()
        if is_bot and len(text) > 200:
            text = text[:200] + "…[长报表折叠]"
        else:
            text = text[:2000]

        # 如果新消息来自机器人且已达机器人配额上限，淘汰最老的一条机器人消息保全真人配额
        if is_bot:
            bot_entries = [e for e in entries if e.is_bot]
            if len(bot_entries) >= self.max_bot_messages:
                for idx, e in enumerate(entries):
                    if e.is_bot:
                        entries.pop(idx)
                        break

        entries.append(
            DialogueEntry(
                message_id,
                stamp,
                str(sender_name or sender_id),
                str(sender_id),
                text,
                role, tuple(str(uid) for uid in mention_ids), reply_sender_id,
                reply_text, is_reply_to_self, reply_sender_name, is_bot,
            )
        )

        # 维持总容量限制：优先淘汰超限的 Bot 消息，保障人类消息保底
        while len(entries) > self.max_messages:
            bots = [e for e in entries if e.is_bot]
            humans = [e for e in entries if not e.is_bot]
            if len(bots) > self.max_bot_messages:
                for idx, e in enumerate(entries):
                    if e.is_bot:
                        entries.pop(idx)
                        break
            elif len(humans) > self.min_human_messages:
                for idx, e in enumerate(entries):
                    if not e.is_bot:
                        entries.pop(idx)
                        break
            else:
                entries.pop(0)

        while len(self._sessions) > self.max_sessions:
            self._sessions.popitem(last=False)

    def get_recent(
        self, session_id, max_count=30, exclude_message_id=None
    ) -> list[DialogueEntry]:
        if max_count <= 0:
            return []
        excluded = (
            str(exclude_message_id) if exclude_message_id not in (None, "") else None
        )
        return [
            e
            for e in self._sessions.get(session_id, ())
            if excluded is None or e.message_id != excluded
        ][-max_count:]

    def format_for_context(
        self, session_id, max_count=30, exclude_message_id=None
    ) -> str:
        entries = self.get_recent(session_id, max_count, exclude_message_id)
        if not entries:
            return ""
        lines = ["[群聊近况]"]
        def label(value, limit=24):
            return "".join(c for c in str(value or "") if c.isprintable())[:limit]

        for entry in entries:
            content = entry.content
            if entry.mention_ids:
                content += " " + " ".join("@" + label(uid, None) for uid in entry.mention_ids)
            if entry.reply_sender_id or entry.reply_text:
                name = sanitize_display_name(entry.reply_sender_name or entry.reply_sender_id)
                own = "(你)" if entry.is_reply_to_self else ""
                content += f" [引用 >>> {name}{own}(ID:{label(entry.reply_sender_id, None)}): {label(entry.reply_text, 30)}]"
            bot_tag = "(机器人)" if entry.is_bot else ""
            lines.append(f"[{entry.formatted_time}] {sanitize_display_name(entry.sender_name)}{bot_tag}(ID:{label(entry.sender_id, None)}): {content}")
        return "\n".join(lines)

    def clear(self, session_id=None):
        if session_id is None:
            self._sessions.clear()
        else:
            self._sessions.pop(session_id, None)


def dialogue_event_id(event) -> str:
    """Stable fallback within one event when the adapter supplies no message ID."""
    message_id = getattr(getattr(event, "message_obj", None), "message_id", None)
    if message_id not in (None, ""):
        return str(message_id)
    fallback = getattr(event, "_livingmemory_dialogue_id", None)
    if not isinstance(fallback, str):
        fallback = f"event:{next(_EVENT_IDS)}"
        setattr(event, "_livingmemory_dialogue_id", fallback)
    return fallback
