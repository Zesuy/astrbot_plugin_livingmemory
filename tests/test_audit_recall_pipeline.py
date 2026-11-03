"""Diagnostic reproductions, with repaired contracts promoted to regressions.

The remaining strict xfails describe desired contracts not addressed yet.
They use synthetic data, real SQLite FTS, and mocked model/retrieval boundaries.
No production database or model API is accessed.
"""

import asyncio
import json
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiosqlite
import pytest

from astrbot.api.platform import MessageType
from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
from astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall import MemoryRecall
from astrbot_plugin_livingmemory.core.event_handler_modules.message_utils import MessageUtils
from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine
from astrbot_plugin_livingmemory.core.models.memory_atom import AtomStatus, MemoryAtom
from astrbot_plugin_livingmemory.core.models.memory_atom import AtomType
from astrbot_plugin_livingmemory.core.processors.atom_classifier import classify_atoms, _parse_event_time
from astrbot_plugin_livingmemory.core.processors.graph_extractor import GraphExtractor
from astrbot_plugin_livingmemory.core.retrieval.hybrid_retriever import HybridResult
from astrbot_plugin_livingmemory.core.tools.memory_search_tool import MemorySearchTool
from astrbot_plugin_livingmemory.core.utils.formatting import format_memories_for_injection
from astrbot_plugin_livingmemory.core.utils.injection_adapter import InjectionAdapter
from astrbot_plugin_livingmemory.storage.atom_store import AtomStore


def result(doc_id, score=0.5, content="context", metadata=None):
    return HybridResult(doc_id=doc_id, final_score=score, rrf_score=score,
                        bm25_score=None, vector_score=None, content=content,
                        metadata=metadata or {})


def engine_at(tmp_path, **config):
    return MemoryEngine(str(tmp_path / "memory.db"), SimpleNamespace(),
                        config={"search_cache_enabled": False, **config})


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="Recent background displaces query evidence")
async def test_precise_recall_does_not_lose_slots_to_unrelated_recent(tmp_path):
    engine = engine_at(tmp_path, recent_memory_count=2)
    relevant = [result(i, 1 - i / 10) for i in range(1, 6)]
    engine._get_recent_memory_results = AsyncMock(
        return_value=[result(901, 1, "model error"), result(902, 1, "weather")])
    actual = await engine._merge_recent_memories(relevant, 5, None, None)
    assert [item.doc_id for item in actual] == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="Existing parent ignores stronger atom evidence")
async def test_existing_parent_receives_stronger_atom_signal(tmp_path):
    engine = engine_at(tmp_path, recent_memory_count=0)
    engine.hybrid_retriever = SimpleNamespace(search=AsyncMock(
        return_value=[result(1, .9, "general context"), result(2, .2, "exact answer")]))
    engine.atom_retriever = SimpleNamespace(search=AsyncMock(
        return_value=[SimpleNamespace(parent_memory_id=2, final_score=.99)]))
    engine._apply_atom_policy = AsyncMock(side_effect=lambda items, **kwargs: items)
    actual = await engine.search_memories("character name", k=2)
    await asyncio.gather(*engine._pending_tasks)
    assert actual[0].doc_id == 2


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="A generic FTS hit prevents substring fallback")
async def test_chinese_entity_inside_sentence_can_retrieve_exact_answer(tmp_path):
    store = AtomStore(str(tmp_path / "atoms.db"))
    await store.initialize()
    await store.insert(MemoryAtom(parent_memory_id=1,
        content="林舟、夏岚讨论角色属性和设定"))
    await store.insert(MemoryAtom(parent_memory_id=2,
        content="林舟扮演研究员（角色名‘夜航者’）"))
    actual = await store.search_fts("林舟 角色 名字")
    assert any("夜航者" in atom.content for atom in actual)


async def atom_filtered_result(tmp_path, summary, live, expired=None):
    store = AtomStore(str(tmp_path / "atoms.db"))
    await store.initialize()
    await store.insert(MemoryAtom(parent_memory_id=1, content=live))
    if expired:
        atom_id = await store.insert(MemoryAtom(parent_memory_id=1, content=expired))
        async with store._connect() as db:
            await db.execute("UPDATE memory_atoms SET expires_at=0 WHERE id=?", (atom_id,))
            await db.commit()
        await store.expire_stale_atoms()
    engine = engine_at(tmp_path)
    engine.atom_enabled = True
    engine.atom_store = store
    engine.db_connection = await aiosqlite.connect(store.db_path)
    metadata = {"atom_types": ["unknown"], "persona_summary": summary,
                "key_facts": [live] + ([expired] if expired else [])}
    try:
        return (await engine._apply_atom_policy([result(1, content=summary,
                                                     metadata=metadata)]))[0]
    finally:
        await engine.db_connection.close()


def injected(item):
    return format_memories_for_injection([
        {"id": item.doc_id, "content": item.content,
         "metadata": item.metadata, "score": item.final_score}])


@pytest.mark.asyncio
async def test_expired_fact_is_not_resurrected_by_prompt_formatting(tmp_path):
    old = "林舟目前住在旧地址"
    item = await atom_filtered_result(tmp_path, old + "，喜欢红茶", "林舟喜欢红茶", old)
    assert old not in item.content  # The engine's expiry filtering does work.
    assert old not in injected(item)  # The downstream formatter bypasses it.


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="Name is absent from active atoms; this fix cannot recover it")
async def test_tool_and_injection_agree_on_character_name(tmp_path):
    item = await atom_filtered_result(tmp_path,
        "林舟的角色叫夜航者，大家讨论了角色属性", "林舟和夏岚讨论了角色属性")
    assert "夜航者" in injected(item)
    assert "夜航者" in item.content


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="Every participant is connected to every atom")
def test_unrelated_participant_is_not_linked_to_specific_fact():
    graph = GraphExtractor().extract(1, "summary", {"participants": ["林舟", "夏岚"]},
        atoms=[MemoryAtom(parent_memory_id=1, content="林舟的角色叫夜航者",
                          entities=["林舟"], expires_at=time.time() + 86400)])
    unrelated = next(node for node in graph.nodes if node.value == "夏岚")
    assert not any(edge.source_key == unrelated.node_key for edge in graph.edges)


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="Requested but unavailable source has no status")
async def test_missing_requested_source_is_explicit():
    memory_engine = Mock()
    memory_engine.search_memories = AsyncMock(return_value=[result(1)])
    memory_engine.get_memory_source = AsyncMock(return_value=[])
    tool = MemorySearchTool(context=Mock(), config_manager=ConfigManager(
        {"filtering_settings": {"use_persona_filtering": False}}), memory_engine=memory_engine)
    event = Mock(unified_msg_origin="test:private:session-1")
    context = SimpleNamespace(context=SimpleNamespace(event=event))
    response = json.loads(await tool.call(context, "name", include_source=True))
    assert response["count"] == 1
    assert "source_status" in response["results"][0]


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="Automatic query ignores the request's reference context")
async def test_automatic_query_uses_available_reference_context():
    config = ConfigManager({"recall_engine": {"top_k": 1, "inject_with_recent_context": False},
                            "filtering_settings": {"use_persona_filtering": False}})
    event = Mock(unified_msg_origin="test:GroupMessage:group-1")
    event.get_message_str.return_value = "名字叫什么"
    event.get_message_type.return_value = MessageType.GROUP_MESSAGE
    memory_engine = Mock(search_memories=AsyncMock(return_value=[]))
    conversation = Mock()
    recall = MemoryRecall(Mock(), config, memory_engine, conversation,
                          MessageUtils(config, conversation), InjectionAdapter())
    request = SimpleNamespace(prompt="引用：林舟的跑团角色；当前追问：名字叫什么",
                              extra_user_content_parts=[], contexts=[], system_prompt="")
    with patch("astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall.get_persona_id",
               new=AsyncMock(return_value="persona")):
        await recall.handle_memory_recall(event, request)
    memory_engine.search_memories.assert_awaited_once()
    assert "林舟" in memory_engine.search_memories.await_args.kwargs["query"]


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="Dated historical actions are classified as future plans")
def test_dated_past_event_is_not_a_future_plan():
    reference = datetime(2026, 10, 1, 12).timestamp()
    with patch("astrbot_plugin_livingmemory.core.processors.atom_classifier.time.time",
               return_value=reference):
        atom = classify_atoms(["2026年9月23日林舟参加跑团"])[0]
    assert atom.atom_type == AtomType.EPISODIC


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="Explicit historical year is discarded and rolled forward")
def test_explicit_past_year_is_preserved():
    reference = datetime(2026, 10, 1, 12).timestamp()
    with patch("astrbot_plugin_livingmemory.core.processors.atom_classifier.time.time",
               return_value=reference):
        event_time = _parse_event_time("2025年9月23日林舟参加跑团")
    assert event_time is not None
    assert datetime.fromtimestamp(event_time).year == 2025
