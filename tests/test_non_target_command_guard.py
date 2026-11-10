"""Stock command recognition and the silent LLM hook short circuit."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot.core.message.components import At, Image, Plain, Reply

from astrbot_plugin_livingmemory.core.non_target_command_guard import (
    extract_command_text,
    non_target_command,
)
from astrbot_plugin_livingmemory.main import LivingMemoryPlugin


def _event(text, *, parts=None):
    return SimpleNamespace(
        get_messages=lambda: [Plain(text)] if parts is None else parts,
        get_message_str=lambda: text,
        get_self_id=lambda: "123456",
        stop_event=Mock(),
        send=AsyncMock(),
        plain_result=Mock(),
        unified_msg_origin="test:group:1",
    )


STOCK_COMMANDS = [
    "/图 D005", "/大盘", "/持仓", "/行情 D005", "/走势 D005",
    "/K线 D005", "/k线", "/资产", "/开户", "/买 D005 100",
    "/卖 D005 100", "/买入 D005 100", "/卖出 D005 100", "/平仓 D005",
    "/平多 D005 100", "/平空 D005 100", "/做多 D005 100", "/做空 D005 100",
    "/撤单 123", "/借 100", "/还 100", "/转账 123456 100",
    "买100股004", "卖出 100 股 D005", "做多 D005 100", "买入 004 100股",
    "/买100股004", "/买入 004 100股", "/卖出 100 股 D005",
]
ORDINARY_TEXT = [
    "今天大盘怎么跌成这样", "你觉得今天大盘怎么样", "老蜀说做空朱芳玉",
    "老蜀做空朱芳玉可还行", "买什么股票好", "买100股004怎么样",
    "大盘", "图 D005", "做空朱芳玉", "转账100元", "我想买100股004",
    "这是 /图 D005", "`/图 D005`", "/图片 D005", "/买什么股票好",
    "/图 D005 是什么意思", "/大盘 怎么样", "/图 D005\nplease explain",
    "", "@A鼠鼠0号 /图 D005", "买100股004？",
]
ASTRBOT_COMMANDS = [
    "/help", "/reset", "/sid", "/t2i", "/lmem", "/lmem search 大盘",
    "/cfg", "/plugin", "/plugin install stocks", "/new", "/unknown D005",
]


@pytest.mark.parametrize("text", STOCK_COMMANDS)
@pytest.mark.parametrize("mention_position", ["prefix", "suffix", "both", "none"])
def test_complete_stock_commands_with_own_mentions(text, mention_position):
    parts = [Plain("  " + text + "  ")]
    if mention_position in ("prefix", "both"):
        parts[:0] = [Plain(" "), At(qq="123456"), Plain(" ")]
    if mention_position in ("suffix", "both"):
        parts.extend([Plain(" "), At(qq=123456), Plain(" ")])
    assert non_target_command(_event("@A鼠鼠0号 " + text, parts=parts)) == text


@pytest.mark.parametrize("text", ORDINARY_TEXT + ASTRBOT_COMMANDS)
def test_natural_language_and_astrbot_commands_are_preserved(text):
    assert non_target_command(_event(text)) is None


def test_only_own_boundary_mentions_are_removed():
    assert non_target_command(_event("", parts=[At(qq="999"), Plain("/图 D005")])) is None
    assert non_target_command(_event("", parts=[Plain("/图 "), At(qq="123456"), Plain("D005")])) is None


@pytest.mark.parametrize("component", [Image(file="test.png"), Reply(id="123"), At(qq="999")])
def test_mixed_content_is_not_flattened_to_a_command(component):
    assert non_target_command(_event("/图 D005", parts=[Plain("/图 D005"), component])) is None


def test_cq_mentions_in_text_only_adapter_are_bound_to_bot_id():
    event = _event(" [CQ:at,qq=123456] /图 D005 [CQ:at,qq=123456] ", parts=[])
    assert extract_command_text(event) == "/图 D005"
    assert non_target_command(event) == "/图 D005"
    assert non_target_command(_event("[CQ:at,qq=999] /图 D005", parts=[])) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("text", STOCK_COMMANDS)
async def test_hook_stops_event_before_any_archive_or_recall_work(text, caplog):
    plugin = LivingMemoryPlugin.__new__(LivingMemoryPlugin)
    plugin.archive_runtime = Mock()
    plugin._ensure_plugin_ready = AsyncMock()
    plugin.event_handler = Mock(handle_memory_recall=AsyncMock())
    event = _event("@A鼠鼠0号 " + text, parts=[At(qq="123456"), Plain(" " + text)])
    request = SimpleNamespace(prompt=text)

    with caplog.at_level("INFO"):
        result = await plugin.handle_memory_recall(event, request)

    assert result is None
    event.stop_event.assert_called_once_with()
    event.send.assert_not_called()
    event.plain_result.assert_not_called()
    plugin.archive_runtime.submit.assert_not_called()
    plugin.archive_runtime.accept_work.assert_not_called()
    plugin._ensure_plugin_ready.assert_not_awaited()
    plugin.event_handler.handle_memory_recall.assert_not_awaited()
    assert "检测到非目标机器人指令" in caplog.text
    assert "停止 LLM 触发并保持静默" in caplog.text
    assert request.prompt == text


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ORDINARY_TEXT + ASTRBOT_COMMANDS)
async def test_hook_preserves_normal_recall_path(text):
    plugin = LivingMemoryPlugin.__new__(LivingMemoryPlugin)
    plugin.archive_runtime = Mock()
    plugin.context = Mock()
    plugin._ensure_plugin_ready = AsyncMock(return_value=(True, ""))
    plugin.event_handler = Mock(handle_memory_recall=AsyncMock())
    event = _event(text)
    request = SimpleNamespace(prompt=text)

    await plugin.handle_memory_recall(event, request)

    event.stop_event.assert_not_called()
    plugin.archive_runtime.submit.assert_called_once()
    plugin.archive_runtime.accept_work.assert_called_once_with(event)
    plugin.event_handler.handle_memory_recall.assert_awaited_once_with(event, request)
