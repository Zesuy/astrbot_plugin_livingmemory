import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot.api.platform import MessageType
from astrbot.core.agent.message import TextPart
from astrbot.core.message.components import Plain
from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
from astrbot_plugin_livingmemory.core.clean_context import CleanContextInjector
from astrbot_plugin_livingmemory.core.dialogue_buffer import (
    DialogueBuffer,
    dialogue_event_id,
)
from astrbot_plugin_livingmemory.core.event_handler_modules.group_capture import (
    GroupCapture,
)
from astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall import (
    MemoryRecall,
)
from astrbot_plugin_livingmemory.core.event_handler_modules.message_utils import (
    MessageUtils,
)
from astrbot_plugin_livingmemory.core.utils.injection_adapter import InjectionAdapter


def test_fifo_dedup_and_evicted_id_can_return():
    buffer = DialogueBuffer(10)
    for i in range(12):
        buffer.add_message("g", "Alice", "1", str(i), message_id=i)
    assert [e.message_id for e in buffer.get_recent("g")] == [
        str(i) for i in range(2, 12)
    ]
    buffer.add_message("g", "Bob", "2", "duplicate", message_id=11)
    assert buffer.get_recent("g")[-1].content == "11"
    buffer.add_message("g", "Alice", "1", "new zero", message_id=0)
    assert buffer.get_recent("g")[-1].content == "new zero"


def test_sessions_limits_and_current_id_exclusion():
    buffer = DialogueBuffer()
    for group in ("a", "b"):
        for i in range(3):
            buffer.add_message(group, group, group, group + str(i), message_id=i)
    assert [e.content for e in buffer.get_recent("a", 1, 2)] == ["a1"]
    assert buffer.get_recent("a", 0) == []
    assert buffer.get_recent("unknown") == []
    buffer.clear("a")
    assert not buffer.get_recent("a") and len(buffer.get_recent("b")) == 3
    buffer.clear()
    assert not buffer.get_recent("b")


def test_human_dialogue_format_and_missing_id_keeps_repeated_text():
    buffer = DialogueBuffer()
    stamp = datetime(2026, 11, 4, 15, 32)
    buffer.add_message("g", "张三", "1", "周末去南山吗？", timestamp=stamp)
    buffer.add_message(
        "g", "李四", "2", "周六有雨，周日吧。", timestamp=stamp.timestamp() * 1000
    )
    assert (
        buffer.format_for_context("g")
        == "[群聊近况]\n15:32\n[张三]: 周末去南山吗？\n[李四]: 周六有雨，周日吧。"
    )
    assert buffer.format_for_context("missing") == ""
    buffer.add_message("g", "张三", "1", "repeat")
    buffer.add_message("g", "张三", "1", "repeat")
    assert len(buffer.get_recent("g")) == 4


def test_bounds_empty_input_and_session_eviction():
    assert DialogueBuffer(1).max_messages == 10
    assert DialogueBuffer(1000).max_messages == 100
    buffer = DialogueBuffer(max_sessions=2)
    for group in "abc":
        buffer.add_message(group, "Alice", "1", "hello", timestamp=float("nan"))
    assert buffer.get_recent("a") == []
    assert len(buffer.get_recent("c")) == 1
    buffer.add_message("c", "Alice", "1", "  ")
    assert len(buffer.get_recent("c")) == 1


def group_event(message_id="current"):
    event = Mock(unified_msg_origin="test:GroupMessage:g")
    event.message_obj = SimpleNamespace(message_id=message_id, timestamp=12345)
    event.get_sender_name.return_value = "张三"
    event.get_sender_id.return_value = "1"
    event.get_self_id.return_value = "bot"
    event.get_message_type.return_value = MessageType.GROUP_MESSAGE
    event.get_message_str.return_value = "现在怎么安排？"
    event.get_messages.return_value = [
        Plain("现在怎么安排？"),
        SimpleNamespace(text="quoted private text"),
    ]
    return event


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_capture_is_plain_only_and_independent_of_archiving(enabled):
    config = ConfigManager(
        {
            "session_manager": {"enable_full_group_capture": False},
            "dialogue_buffer": {"enabled": enabled},
        }
    )
    buffer = DialogueBuffer()
    conversation = Mock()
    capture = GroupCapture(config, conversation, Mock(), buffer)
    event = group_event()
    await capture.handle_all_group_messages(event)
    await capture.handle_all_group_messages(event)
    entries = buffer.get_recent(event.unified_msg_origin)
    assert len(entries) == int(enabled)
    if enabled:
        assert entries[0].content == "现在怎么安排？"
    conversation.add_message_from_event.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("top_k", [0, 2])
@pytest.mark.parametrize("auto_remove", [True, False])
async def test_unified_temp_injection_and_repeat_hook_dedup(top_k, auto_remove):
    config = ConfigManager(
        {
            "recall_engine": {"top_k": top_k, "auto_remove_injected": auto_remove},
            "filtering_settings": {"use_persona_filtering": False},
        }
    )
    event = group_event()
    buffer = DialogueBuffer()
    buffer.add_message(
        event.unified_msg_origin, "李四", "2", "周六有雨", message_id="prior"
    )
    buffer.add_message(
        event.unified_msg_origin, "张三", "1", "本轮触发消息", message_id="current"
    )
    memory = SimpleNamespace(
        doc_id=1, content="张三喜欢徒步", final_score=0.9, metadata={}
    )
    engine = SimpleNamespace(search_memories=AsyncMock(return_value=[memory]))
    conversation = Mock()
    recall = MemoryRecall(
        Mock(),
        config,
        engine,
        conversation,
        MessageUtils(config, conversation),
        InjectionAdapter(),
        dialogue_buffer=buffer,
    )
    unrelated = TextPart(text="Other plugin context").mark_as_temp()
    req = SimpleNamespace(
        prompt="现在怎么安排？",
        extra_user_content_parts=[unrelated],
        contexts=[],
        system_prompt="persona",
    )
    for _ in range(2):
        await recall.handle_memory_recall(event, req)
    assert len(req.extra_user_content_parts) == 2
    assert req.extra_user_content_parts[0] is unrelated
    part = req.extra_user_content_parts[1]
    assert part._no_save
    assert "[群聊近况]" in part.text
    assert "周六有雨" in part.text and "本轮触发消息" not in part.text
    assert "[本轮说话人]\n张三" in part.text
    assert ("[长期记忆 / 群友印象]\n- 张三喜欢徒步" in part.text) == (top_k > 0)
    assert "Importance" not in part.text and "CRITICAL" not in part.text
    assert req.prompt == "现在怎么安排？" and req.system_prompt == "persona"
    if top_k == 0:
        engine.search_memories.assert_not_awaited()
    assert (
        recall._remove_injected_memories_from_context(req, event.unified_msg_origin)
        == 1
    )
    assert req.extra_user_content_parts == [unrelated]


@pytest.mark.asyncio
async def test_private_and_disabled_group_do_not_leak_dialogue():
    config = ConfigManager({"recall_engine": {"top_k": 0}})
    event = group_event()
    event.get_message_type.return_value = MessageType.FRIEND_MESSAGE
    recall = MemoryRecall(
        Mock(),
        config,
        Mock(),
        Mock(),
        Mock(),
        InjectionAdapter(),
        dialogue_buffer=DialogueBuffer(),
    )
    req = SimpleNamespace(extra_user_content_parts=[])
    recall._inject_clean_context(event, req, [])
    assert req.extra_user_content_parts == []
    config = ConfigManager({"dialogue_buffer": {"enabled": False}})
    recall.config_manager = config
    event.get_message_type.return_value = MessageType.GROUP_MESSAGE
    recall.dialogue_buffer.add_message(event.unified_msg_origin, "A", "1", "secret")
    recall._inject_clean_context(event, req, [])
    assert req.extra_user_content_parts == []


def test_clean_context_preserves_live_atom_filtering_and_budget():
    text = CleanContextInjector.format_context(
        memories=[
            {
                "content": "live fact",
                "metadata": {
                    "atom_policy_applied": True,
                    "persona_summary": "expired fact",
                    "key_facts": ["expired fact"],
                },
            }
        ],
        speaker="Alice",
    )
    assert text == "[长期记忆 / 群友印象]\n- live fact\n\n[本轮说话人]\nAlice"
    text = CleanContextInjector.format_context(memories=[{"content": "x" * 10000}] * 50)
    assert len(text) < 6100


def test_missing_adapter_message_id_is_stable_per_event():
    first, second = group_event(None), group_event(None)
    assert dialogue_event_id(first) == dialogue_event_id(first)
    assert dialogue_event_id(first) != dialogue_event_id(second)


def test_config_and_four_locale_parity():
    config = ConfigManager({"dialogue_buffer": {"max_messages": 999, "enabled": False}})
    assert config.get("dialogue_buffer.max_messages") == 100
    assert config.get("dialogue_buffer.enabled") is False
    root = Path(__file__).resolve().parents[1]
    for lang in ("zh-CN", "en-US", "ja-JP", "ru-RU"):
        data = json.loads(
            (root / f".astrbot-plugin/i18n/{lang}.json").read_text(encoding="utf-8")
        )["config"]
        assert set(data["reflection_engine"]["summary_mode"]["options"]) == {
            "events",
            "persona",
        }
        assert all(
            data["dialogue_buffer"][key]["description"]
            for key in ("enabled", "max_messages")
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["whitelist", "self_echo", "private"])
async def test_capture_respects_access_and_event_boundaries(denial):
    config = ConfigManager(
        {
            "session_manager": {"enable_full_group_capture": False},
            "access_control": {"whitelist_enabled": denial == "whitelist"},
        }
    )
    event = group_event()
    if denial == "self_echo":
        event.get_sender_id.return_value = "bot"
    elif denial == "private":
        event.get_message_type.return_value = MessageType.FRIEND_MESSAGE
    buffer = DialogueBuffer()
    capture = GroupCapture(config, Mock(), Mock(), buffer)
    await capture.handle_all_group_messages(event)
    assert buffer.get_recent(event.unified_msg_origin) == []


@pytest.mark.asyncio
async def test_recall_failure_keeps_local_dialogue():
    config = ConfigManager({"filtering_settings": {"use_persona_filtering": False}})
    event = group_event(None)
    buffer = DialogueBuffer()
    buffer.add_message(
        event.unified_msg_origin, "Alice", "1", "background", message_id="prior"
    )
    buffer.add_message(
        event.unified_msg_origin,
        "Alice",
        "1",
        "trigger",
        message_id=dialogue_event_id(event),
    )
    engine = SimpleNamespace(
        search_memories=AsyncMock(side_effect=RuntimeError("offline"))
    )
    conversation = Mock()
    recall = MemoryRecall(
        Mock(),
        config,
        engine,
        conversation,
        MessageUtils(config, conversation),
        InjectionAdapter(),
        dialogue_buffer=buffer,
    )
    req = SimpleNamespace(
        prompt="question", extra_user_content_parts=[], contexts=[], system_prompt=""
    )
    await recall.handle_memory_recall(event, req)
    assert len(req.extra_user_content_parts) == 1
    assert "background" in req.extra_user_content_parts[0].text
    assert "trigger" not in req.extra_user_content_parts[0].text


def test_builtin_and_fallback_templates_have_no_defensive_boilerplate():
    from astrbot_plugin_livingmemory.core.prompts.prompt_manager import PromptManager
    from astrbot_plugin_livingmemory.core.utils.formatting import (
        _get_default_injection_footer,
        _get_default_injection_header,
    )

    manager = PromptManager()
    text = manager.get_prompt("memory_injection_header") + manager.get_prompt(
        "memory_injection_footer"
    )
    text += _get_default_injection_header() + _get_default_injection_footer()
    for phrase in (
        "CRITICAL RULES",
        "ALWAYS",
        "Do NOT",
        "BEGIN REMINDER",
        "BEGIN HISTORICAL",
    ):
        assert phrase not in text


@pytest.mark.asyncio
async def test_dialogue_snapshot_is_frozen_before_slow_recall():
    config = ConfigManager({"filtering_settings": {"use_persona_filtering": False}})
    event = group_event()
    buffer = DialogueBuffer()
    buffer.add_message(event.unified_msg_origin, "Alice", "1", "before search")

    async def search(**kwargs):
        buffer.add_message(event.unified_msg_origin, "Bob", "2", "after search")
        return []

    conversation = Mock()
    recall = MemoryRecall(
        Mock(),
        config,
        SimpleNamespace(search_memories=search),
        conversation,
        MessageUtils(config, conversation),
        InjectionAdapter(),
        dialogue_buffer=buffer,
    )
    req = SimpleNamespace(
        prompt="question", extra_user_content_parts=[], contexts=[], system_prompt=""
    )
    await recall.handle_memory_recall(event, req)
    assert "before search" in req.extra_user_content_parts[0].text
    assert "after search" not in req.extra_user_content_parts[0].text


@pytest.mark.parametrize("enabled", [True, False])
def test_passive_filter_schedules_window_when_archive_capture_is_off(enabled):
    from astrbot_plugin_livingmemory.core.passive_group_capture import (
        PassiveGroupCaptureFilter,
    )

    from tests.test_group_capture_filter import _make_event, _make_plugin

    plugin = _make_plugin()
    plugin.config_manager.get.side_effect = lambda key, default=None: {
        "session_manager.enable_full_group_capture": False,
        "dialogue_buffer.enabled": enabled,
    }.get(key, default)
    event = _make_event()
    assert PassiveGroupCaptureFilter(plugin).filter(event, {}) is False
    assert plugin._schedule_passive_group_capture.call_count == int(enabled)
    assert event.is_wake is False
