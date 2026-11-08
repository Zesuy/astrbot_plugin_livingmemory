"""Recognize complete third-party stock commands without involving an LLM."""

import re

from astrbot.core.message.components import At, Plain


# An explicit vocabulary leaves AstrBot commands and unknown plugin commands alone.
_SLASH_COMMAND = re.compile(
    r"/(?:大盘|持仓|图|行情|走势|k线|资产|开户|买入|卖出|买|卖|"
    r"平仓|平多|平空|做多|做空|撤单|借|还|转账)"
    r"(?:[ \t]+[A-Za-z0-9_.+%:@-]+)*",
    re.IGNORECASE,
)
_BARE_TRADE = re.compile(
    r"(?:买入|卖出|买|卖|平仓|平多|平空|做多|做空)\s*"
    r"(?:[0-9]+(?:\.[0-9]+)?\s*股\s*[A-Za-z]?[0-9]{3,6}"
    r"|[A-Za-z]?[0-9]{3,6}\s+[0-9]+(?:\.[0-9]+)?(?:\s*股)?)",
    re.IGNORECASE,
)


def extract_command_text(event) -> str:
    """Strip only boundary mentions of this bot; never flatten mixed media/quotes.

    AstrBot Plain components contain the original text while message_str may
    contain rendered @nicknames. Structured At components let us remove the
    actual bot mention without guessing its current display name.
    """
    messages = event.get_messages()
    self_id = str(event.get_self_id())
    if messages:
        parts = list(messages)
        while parts and (
            isinstance(parts[0], Plain) and not parts[0].text.strip()
            or isinstance(parts[0], At) and str(parts[0].qq) == self_id
        ):
            parts.pop(0)
        while parts and (
            isinstance(parts[-1], Plain) and not parts[-1].text.strip()
            or isinstance(parts[-1], At) and str(parts[-1].qq) == self_id
        ):
            parts.pop()
        if not parts or any(not isinstance(part, Plain) for part in parts):
            return ""
        return "".join(part.text for part in parts).strip()

    # Text-only adapters may retain OneBot CQ mentions instead of components.
    text = event.get_message_str().strip()
    mention = rf"\[CQ:at,qq={re.escape(self_id)}\]"
    text = re.sub(rf"^(?:{mention}\s*)+", "", text)
    return re.sub(rf"(?:\s*{mention})+$", "", text).strip()


def non_target_command(event) -> str | None:
    """Return the complete matched command, or None for ordinary conversation."""
    text = extract_command_text(event)
    if _SLASH_COMMAND.fullmatch(text) or _BARE_TRADE.fullmatch(text.removeprefix("/")):
        return text
    return None
