"""The passive injection switch must leave on-demand recall and clean context intact."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
from astrbot_plugin_livingmemory.core.clean_context import CleanContextInjector
from astrbot_plugin_livingmemory.core.event_handler import EventHandler
from tests.event_handler_support import (
    _make_event,
    _make_req,
    conversation_manager,
    memory_engine,
    memory_processor,
)


@pytest.mark.parametrize("enabled,top_k", [(None, 3), (False, 3), (True, 0), (True, 3)])
@pytest.mark.asyncio
async def test_passive_switch_controls_search_and_preserves_context(
    enabled, top_k, memory_engine, memory_processor, conversation_manager
):
    recall_config = {"top_k": top_k, "inject_with_recent_context": True}
    if enabled is not None:
        recall_config["passive_injection_enabled"] = enabled
    config = ConfigManager({
        "recall_engine": recall_config,
        "agent_tools": {"enable_recall_tool": True, "enable_agentic_recall_beta": True},
    })
    handler = EventHandler(Mock(), config, memory_engine, memory_processor, conversation_manager)
    recall = handler._memory_recall
    recall._get_dialogue_context = Mock(return_value="[群聊近况]\n刚才聊了天气")
    recall._get_cross_group_context = AsyncMock(return_value="[隔壁群实时动态]\n正在下雨")
    conversation_manager.get_context = AsyncMock(return_value=[])
    memory_engine.search_memories.return_value = [
        SimpleNamespace(doc_id=1, content="去年一起徒步", final_score=0.9, metadata={})
    ]
    event = _make_event(group=True)
    event.get_extra = Mock(return_value=None)
    req = _make_req("还记得去年的事吗")
    req.func_tool = SimpleNamespace(names=lambda: ["recall_long_term_memory", "query_chat_events"])

    with patch(
        "astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall.get_persona_id",
        new_callable=AsyncMock,
        return_value="persona",
    ):
        await handler.handle_memory_recall(event, req)

    text = "\n".join(part.text for part in req.extra_user_content_parts)
    should_search = enabled is True and top_k > 0
    assert ("[长期记忆与历史事件]" in text) is should_search
    assert ("去年一起徒步" in text) is should_search
    assert "[群聊近况]" in text
    assert "[隔壁群实时动态]" in text
    assert "[当前会话]" in text
    assert "先使用记忆检索工具" in text
    assert "query_chat_events" in text
    assert all(part._no_save for part in req.extra_user_content_parts)
    recall._get_cross_group_context.assert_awaited_once()
    if should_search:
        memory_engine.search_memories.assert_awaited_once()
        conversation_manager.get_context.assert_awaited_once()
    else:
        memory_engine.search_memories.assert_not_awaited()
        conversation_manager.get_context.assert_not_awaited()


def test_empty_memories_never_render_long_term_header():
    text = CleanContextInjector.format_context("[群聊近况]\n刚才聊了天气", [])
    assert "[长期记忆与历史事件]" not in text
    assert "[群聊近况]" in text


def test_astrbot_schema_exposes_switch_with_matching_default():
    schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8"))
    recall = schema["recall_engine"]
    assert recall.get("invisible") is True
    assert recall["items"]["passive_injection_enabled"]["type"] == "bool"
    assert recall["items"]["passive_injection_enabled"]["default"] is False
    assert ConfigManager().get("recall_engine.passive_injection_enabled") is False
