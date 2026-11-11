"""Regression coverage for lifecycle-filtered recall at both LLM entry points."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import pytest
import pytest_asyncio

from astrbot.api.platform import MessageType
from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
from astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall import MemoryRecall
from astrbot_plugin_livingmemory.core.event_handler_modules.message_utils import MessageUtils
from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine
from astrbot_plugin_livingmemory.core.models.memory_atom import AtomStatus, MemoryAtom
from astrbot_plugin_livingmemory.core.retrieval.hybrid_retriever import HybridResult
from astrbot_plugin_livingmemory.core.tools.memory_search_tool import MemorySearchTool
from astrbot_plugin_livingmemory.core.utils.formatting import format_memories_for_injection
from astrbot_plugin_livingmemory.core.utils.injection_adapter import InjectionAdapter
from astrbot_plugin_livingmemory.storage.atom_store import AtomStore


LIVE_NAME = "林舟的角色叫夜航者"
OLD_NAME = "林舟的角色叫旧船长"
OLD_FACT = "林舟住在旧地址"


def _result(doc_id=1, content=None, metadata=None):
    return HybridResult(
        doc_id=doc_id,
        final_score=0.8,
        rrf_score=0.8,
        bm25_score=None,
        vector_score=None,
        content=content if content is not None else OLD_NAME,
        metadata=metadata if metadata is not None else {
            "atom_types": ["factual"],
            "summary_schema_version": "v2",
            "persona_summary": OLD_NAME,
            "key_facts": [LIVE_NAME, OLD_FACT],
            "topics": ["跑团"],
            "participants": ["林舟"],
            "time_tags": ["2026-09-30"],
        },
    )


def _injected(memory, as_dict=False):
    entry = (
        {"content": memory.content, "metadata": memory.metadata}
        if as_dict else memory
    )
    return format_memories_for_injection([entry])


@pytest_asyncio.fixture
async def atom_engine(tmp_path):
    store = AtomStore(str(tmp_path / "atoms.db"))
    await store.initialize()
    engine = MemoryEngine(store.db_path, SimpleNamespace())
    engine.atom_enabled = True
    engine.atom_store = store
    engine.db_connection = await aiosqlite.connect(store.db_path)
    engine._update_access_times_internal = AsyncMock()
    try:
        yield engine, store
    finally:
        if engine._pending_tasks:
            await asyncio.gather(*engine._pending_tasks)
        await engine.db_connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("as_dict", [False, True])
@pytest.mark.parametrize("status,ttl_expired", [
    (AtomStatus.EXPIRED, False),
    (AtomStatus.SUPERSEDED, False),
    (AtomStatus.ACTIVE, True),
])
async def test_injection_does_not_restore_filtered_summary_or_key_facts(
    atom_engine, as_dict, status, ttl_expired
):
    engine, store = atom_engine
    await store.insert(MemoryAtom(parent_memory_id=1, content=LIVE_NAME))
    old_id = await store.insert(MemoryAtom(
        parent_memory_id=1,
        content=OLD_NAME,
        status=status,
    ))
    if ttl_expired:
        # Insertion recomputes TTL, so expire the persisted row afterwards.
        await engine.db_connection.execute(
            "UPDATE memory_atoms SET expires_at = ? WHERE id = ?",
            (time.time() - 60, old_id),
        )
        await engine.db_connection.commit()
    original = _result()
    memory, = await engine._apply_atom_policy([original])

    assert memory.content == LIVE_NAME
    text = _injected(memory, as_dict=as_dict)
    assert OLD_NAME not in text
    assert OLD_FACT not in text
    assert text.count(LIVE_NAME) == 1
    assert "Topics: 跑团" in text
    assert "Participants: 林舟" in text
    assert "Source time: 2026-09-30" in text
    # Filtering a recall view must not rewrite the stored/source summary.
    assert original.content == OLD_NAME
    assert original.metadata["persona_summary"] == OLD_NAME
    assert original.metadata["key_facts"] == [LIVE_NAME, OLD_FACT]


@pytest.mark.asyncio
async def test_tool_and_automatic_injection_preserve_the_same_live_name(atom_engine):
    engine, store = atom_engine
    await store.insert(MemoryAtom(parent_memory_id=1, content=LIVE_NAME))
    memories = await engine._apply_atom_policy([_result()])
    search_engine = SimpleNamespace(search_memories=AsyncMock(return_value=memories))
    config = ConfigManager({
        "recall_engine": {"passive_injection_enabled": True, "top_k": 1, "inject_with_recent_context": False},
        "filtering_settings": {"use_persona_filtering": False},
    })
    event = Mock(unified_msg_origin="test:GroupMessage:group-1")
    event.get_message_str.return_value = "林舟的角色叫什么"
    event.get_message_type.return_value = MessageType.GROUP_MESSAGE
    request = SimpleNamespace(
        prompt="林舟的角色叫什么",
        extra_user_content_parts=[], contexts=[], system_prompt="",
    )
    conversation = Mock()
    recall = MemoryRecall(
        Mock(), config, search_engine, conversation,
        MessageUtils(config, conversation), InjectionAdapter(),
    )
    await recall.handle_memory_recall(event, request)
    assert len(request.extra_user_content_parts) == 1
    injected_text = request.extra_user_content_parts[0].text

    tool = MemorySearchTool(
        context=Mock(), config_manager=config, memory_engine=search_engine,
    )
    context = SimpleNamespace(context=SimpleNamespace(event=event))
    response = json.loads(await tool.call(context, "林舟的角色叫什么"))
    assert response["count"] == 1
    tool_content = response["results"][0]["content"]
    assert tool_content == LIVE_NAME
    assert tool_content in injected_text
    assert OLD_NAME not in injected_text
    assert OLD_FACT not in injected_text


@pytest.mark.asyncio
async def test_cached_recall_rechecks_expiry_before_display(atom_engine):
    engine, store = atom_engine
    name_id = await store.insert(MemoryAtom(parent_memory_id=1, content=LIVE_NAME))
    remaining_fact = "林舟喜欢跑团"
    await store.insert(MemoryAtom(parent_memory_id=1, content=remaining_fact))
    memories = await engine._apply_atom_policy([_result()])
    key = engine._search_cache_key("角色", 5, None, None)
    engine._set_cached_search_results(key, memories)
    async with store._connect() as db:
        await db.execute("UPDATE memory_atoms SET expires_at=0 WHERE id=?", (name_id,))
        await db.commit()
    await store.expire_stale_atoms()

    memory, = await engine.search_memories("角色")
    assert memory.content == remaining_fact
    text = _injected(memory)
    assert remaining_fact in text
    assert LIVE_NAME not in text
    assert OLD_NAME not in text
    assert OLD_FACT not in text


@pytest.mark.asyncio
async def test_fully_expired_parent_is_not_injected(atom_engine):
    engine, store = atom_engine
    await store.insert(MemoryAtom(
        parent_memory_id=1, content=OLD_NAME, status=AtomStatus.EXPIRED,
    ))
    memories = await engine._apply_atom_policy([_result()])
    assert memories == []
    assert format_memories_for_injection(memories) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("atom_enabled", [False, True])
async def test_legacy_memory_without_atoms_keeps_persona_display(atom_engine, atom_enabled):
    engine, _ = atom_engine
    engine.atom_enabled = atom_enabled
    metadata = {
        "persona_summary": "我记得林舟喜欢跑团呀",
        "key_facts": ["林舟喜欢跑团"],
    }
    original = _result(doc_id=2, content="中性检索摘要", metadata=metadata)
    memory, = await engine._apply_atom_policy([original])
    text = _injected(memory)
    assert memory is original
    assert "我记得林舟喜欢跑团呀" in text
    assert "中性检索摘要" not in text
    assert "Key facts: 林舟喜欢跑团" in text
