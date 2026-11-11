"""Centralized identification and source categorization for bot/human messages."""

from __future__ import annotations

import re
from typing import Any

from .memory_scope import parse_value_list, _config_get


def get_configured_bot_ids(config: Any = None) -> set[str]:
    """Extract unique bot IDs from both new filtering_settings and legacy user_profiles."""
    if config is None:
        return set()
    raw_list = []
    # 优先读取 filtering_settings.bot_ids，同时兼容 user_profiles.bot_ids
    val1 = _config_get(config, "filtering_settings.bot_ids", "")
    val2 = _config_get(config, "user_profiles.bot_ids", "")
    for val in (val1, val2):
        if val:
            raw_list.extend(parse_value_list(val))
    return {str(item).strip() for item in raw_list if str(item).strip()}


def is_bot_account(
    sender_id: str | int | None,
    config: Any = None,
    self_id: str | int | None = None,
    is_bot_flag: bool = False,
) -> bool:
    """Check if an account ID represents a bot (self or known external bot)."""
    if is_bot_flag:
        return True
    sid = str(sender_id or "").strip()
    if not sid:
        return False
    if self_id and sid == str(self_id).strip():
        return True
    bot_ids = get_configured_bot_ids(config)
    return sid in bot_ids


def classify_message_source(
    row: dict[str, Any],
    config: Any = None,
    self_id: str | int | None = None,
) -> str:
    """Return 'self_bot', 'known_bot', or 'user'."""
    sid = str(row.get("sender_id") or "").strip()
    role = str(row.get("role") or "").casefold()
    if role == "assistant" or (self_id and sid == str(self_id).strip()):
        return "self_bot"
    is_bot = bool(row.get("is_bot"))
    if is_bot or is_bot_account(sid, config, self_id):
        return "known_bot"
    return "user"
