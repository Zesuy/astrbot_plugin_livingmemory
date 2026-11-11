"""Classification and filtering for automated game/command transactions (e.g. 修仙, 股市)."""

from __future__ import annotations

import re
from typing import Any

# 修仙与群聊游戏常见命令前缀及词汇
_GAME_COMMAND_REGEX = re.compile(
    r"^[/!#／！＃]"
    r"(?:修仙|宗门|炼丹|闭关|突破|我的灵石|灵石|签到|打劫|探索|装备|背包|打坐|秘境|功法|境界|灵根|渡劫|传功|坊市|抽卡)"
    r"(?:[ \t]+.*)?$",
    re.IGNORECASE,
)

# 股票/金融命令
_STOCK_COMMAND_REGEX = re.compile(
    r"^[/!#／！＃]"
    r"(?:大盘|持仓|图|行情|走势|k线|资产|开户|买入|卖出|买|卖|平仓|平多|平空|做多|做空|撤单|借|还|转账)"
    r"(?:[ \t]+.*)?$",
    re.IGNORECASE,
)

# 常见无前缀的硬交易命令
_BARE_TRADE_REGEX = re.compile(
    r"^(?:买入|卖出|买|卖|平仓|平多|平空|做多|做空)\s*"
    r"(?:[0-9]+(?:\.[0-9]+)?\s*股\s*[A-Za-z]?[0-9]{3,6}|[A-Za-z]?[0-9]{3,6}\s+[0-9]+(?:\.[0-9]+)?(?:\s*股)?)$",
    re.IGNORECASE,
)

# 常见游戏/修仙机器人的响应模板特征（面板/数值报表）
_GAME_RESPONSE_HEADER_REGEX = re.compile(
    r"[【\[]"
    r"(?:天道突破|突破成功|突破失败|闭关结算|炼丹结果|宗门信息|人物面板|秘境结算|签到成功|灵石增加|修为增加|修仙面板|道友状态|灵石报表)"
    r"[】\]]"
)

_STAT_REPORT_REGEX = re.compile(
    r"(?:修为|灵石|寿元|境界|气血|攻击|防御|丹药|战力|功力|胜率|持仓|市值)\s*[:：]?\s*[+\-]?\d+"
)


def is_game_command(text: str) -> bool:
    """Check if text is an explicit third-party bot game/finance command."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return False
    if _GAME_COMMAND_REGEX.match(cleaned):
        return True
    if _STOCK_COMMAND_REGEX.match(cleaned):
        return True
    if _BARE_TRADE_REGEX.match(cleaned):
        return True
    return False


def is_game_response(text: str, is_bot: bool = False) -> bool:
    """Check if text represents an automated game response/stat sheet."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return False
    if _GAME_RESPONSE_HEADER_REGEX.search(cleaned):
        return True
    stat_matches = _STAT_REPORT_REGEX.findall(cleaned)
    if is_bot and len(stat_matches) >= 2:
        return True
    if len(stat_matches) >= 3:
        return True
    return False


def is_automated_game_transaction(text: str, role: str = "user", is_bot: bool = False) -> bool:
    """Check if a message is part of an automated game transaction (command or response)."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return False
    if is_game_command(cleaned):
        return True
    if is_game_response(cleaned, is_bot=is_bot or role == "assistant"):
        return True
    return False


def filter_social_messages(messages: list[Any], config: Any = None) -> tuple[list[Any], dict[str, int]]:
    """Extract real human social messages, filtering out automated game command/response pairs."""
    from .source_policy import is_bot_account
    
    # 核心守卫：如果整批切片内没有哪怕一条真正的真人发言（非助理、非外部Bot、非游戏命令）
    has_real_human = False
    for m in messages:
        sid = getattr(m, "sender_id", "")
        r = getattr(m, "role", "user")
        cnt = getattr(m, "content", "")
        raw = " ".join(c.get("text", "") for c in cnt if isinstance(c, dict) and c.get("type") in ("plain", "text")).strip() if isinstance(cnt, list) else str(cnt or "").strip()
        if r != "assistant" and not is_bot_account(sid, config) and not getattr(m, "is_bot", False) and not is_game_command(raw):
            has_real_human = True
            break

    if not has_real_human:
        return [], {
            "retained_social": 0,
            "filtered_commands": sum(1 for m in messages if is_game_command(getattr(m, "content", ""))),
            "filtered_responses": 0,
            "filtered_bot_spam": len(messages),
        }

    social_messages = []
    filtered_commands = 0
    filtered_responses = 0
    filtered_bot_spam = 0

    for msg in messages:
        sender_id = getattr(msg, "sender_id", "")
        role = getattr(msg, "role", "user")
        is_bot = getattr(msg, "is_bot", False) or is_bot_account(sender_id, config) or role == "assistant"
        content = getattr(msg, "content", "")
        if isinstance(content, list):
            text_parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") in ("plain", "text")]
            raw_text = " ".join(text_parts).strip()
        else:
            raw_text = str(content or "").strip()

        if is_game_command(raw_text):
            filtered_commands += 1
            continue
        if is_game_response(raw_text, is_bot=is_bot):
            filtered_responses += 1
            continue
        if is_bot_account(sender_id, config) or getattr(msg, "is_bot", False):
            filtered_bot_spam += 1
            continue

        social_messages.append(msg)

    counts = {
        "retained_social": len(social_messages),
        "filtered_commands": filtered_commands,
        "filtered_responses": filtered_responses,
        "filtered_bot_spam": filtered_bot_spam,
    }
    return social_messages, counts
