"""Bounded, process-local group dialogue snapshots; no persistence or I/O."""

import math
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime
from itertools import count

_EVENT_IDS = count()


@dataclass(frozen=True)
class DialogueEntry:
    message_id: str | None
    timestamp: float
    sender_name: str
    sender_id: str
    content: str

    @property
    def formatted_time(self) -> str:
        return datetime.fromtimestamp(self.timestamp).strftime("%H:%M")


class DialogueBuffer:
    """Each session has a FIFO window; inactive sessions are evicted by LRU."""

    def __init__(self, max_messages: int = 50, *, max_sessions: int = 1000):
        self.max_messages = min(100, max(10, int(max_messages)))
        self.max_sessions = max(1, int(max_sessions))
        self._sessions: OrderedDict[str, deque[DialogueEntry]] = OrderedDict()

    def add_message(
        self,
        session_id,
        sender_name,
        sender_id,
        content,
        message_id=None,
        timestamp=None,
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
        entries = self._sessions.setdefault(session_id, deque(maxlen=self.max_messages))
        self._sessions.move_to_end(session_id)
        if message_id is not None and any(e.message_id == message_id for e in entries):
            return
        entries.append(
            DialogueEntry(
                message_id,
                stamp,
                str(sender_name or sender_id),
                str(sender_id),
                content.strip()[:2000],
            )
        )
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
        previous_time = None
        for entry in entries:
            if entry.formatted_time != previous_time:
                lines.append(entry.formatted_time)
                previous_time = entry.formatted_time
            lines.append(f"[{entry.sender_name}]: {entry.content}")
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
