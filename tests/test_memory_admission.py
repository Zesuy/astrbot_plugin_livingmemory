"""Admission gate and reversible legacy audit regression tests."""
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_livingmemory.core.processors.memory_processor import MemoryProcessor
from astrbot_plugin_livingmemory.core.prompts import prompt_manager
from astrbot_plugin_livingmemory.core.utils.recall_policy import is_non_substantive
from astrbot_plugin_livingmemory.scripts.audit_dirty_memories import apply, rollback, scan, classify, _RULES
from tests.test_time_scoped_events import message, NOW
from tests.event_handler_support import _make_real_handler
from tests.test_reflection_backlog import _seed_messages, _build_plan, _run_scheduled


@pytest.mark.asyncio
@pytest.mark.parametrize('payload,mode,reason', [
    ({'events': [], 'key_facts': []}, 'events', 'no_durable_events'),
    ({'events': [], 'summary': '强制凑数的长篇群聊记录内容', 'key_facts': ['哈哈']}, 'events', 'no_durable_events'),
    ({'summary': '', 'key_facts': []}, 'persona', 'low_quality'),
    ({'summary': '短', 'key_facts': ['短']}, 'persona', 'low_quality'),
    ({'summary': '林舟明确表示以后喜欢使用深色主题界面', 'key_facts': ['林舟喜欢深色主题'], 'quality': 'low'}, 'persona', 'low_quality'),
    ({'events': [{'summary': '林舟明确表示喜欢深色主题', 'source_refs': ['unknown']}]}, 'events', 'no_durable_events'),
])
async def test_no_memory_contract(payload, mode, reason):
    llm = SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(completion_text=json.dumps(payload))))
    processor = MemoryProcessor(llm_provider=llm, config={'summary_mode': mode})
    content, metadata, importance = await processor.process_conversation([message(1, '不应摘抄入库的真实原文', NOW)], True)
    assert content == '' and importance == 0
    assert metadata['processing_status'] == 'processed_no_memory'
    assert metadata['no_memory_reason'] == reason
    assert processor.classify_atoms_from_metadata(metadata) == []


def test_no_raw_excerpt_fallback():
    processor = MemoryProcessor()
    content, metadata = processor._build_storage_format('原文绝对不能填入', {'summary': '', 'key_facts': []}, False)
    assert content == ''


def test_low_quality_cannot_create_atoms():
    processor = MemoryProcessor()
    assert processor.classify_atoms_from_metadata({'summary_quality': 'low', 'key_facts': ['某人说了句话']}) == []


@pytest.mark.asyncio
async def test_event_mode_honors_live_system_and_task_overrides(tmp_path, monkeypatch):
    manager = prompt_manager.PromptManager(str(tmp_path))
    monkeypatch.setattr(prompt_manager, 'get_prompt_manager', lambda: manager)
    processor = MemoryProcessor()
    manager.update_prompt('memory_system_prompt_base', '自定义系统规则')
    manager.update_prompt('event_summary_prompt', '自定义事件准入 {conversation}')
    assert processor._get_chat_prompt(True).startswith('自定义事件准入')
    system = await processor._build_system_prompt_with_persona('unused')
    assert '自定义系统规则' in system and 'events: []' in system
    manager.update_prompt('memory_system_prompt_base', '新系统规则')
    assert '新系统规则' in await processor._build_system_prompt_with_persona(None)


@pytest.mark.asyncio
async def test_empty_window_commits_cursor_without_atoms_or_engine_write(tmp_path):
    handler, manager, store = await _make_real_handler(tmp_path)
    session = 'test:group:empty'
    await _seed_messages(manager, session, [NOW, NOW+1, NOW+2, NOW+3])
    handler.memory_processor.process_conversation = AsyncMock(return_value=('', {'processing_status': 'processed_no_memory'}, 0))
    handler.memory_processor.classify_atoms_from_metadata = Mock(side_effect=AssertionError('must skip atoms'))
    handler.memory_engine.add_memory = AsyncMock(side_effect=AssertionError('must skip documents/vector/graph'))
    try:
        await _run_scheduled(handler, await _build_plan(handler, session, 4))
        assert await manager.get_session_metadata(session, 'last_summarized_index', 0) == 4
        assert await manager.get_session_metadata(session, 'pending_summary', None) is None
        handler.memory_engine.add_memory.assert_not_awaited()
        handler.memory_processor.classify_atoms_from_metadata.assert_not_called()
    finally:
        await handler.shutdown()
        await store.close()


@pytest.fixture
def audit_db():
    connection = sqlite3.connect(':memory:')
    connection.execute('CREATE TABLE documents(id INTEGER PRIMARY KEY, text TEXT, metadata TEXT)')
    for identity, text, metadata in [
        (1, '哈哈哈', '{}'), (2, '林舟周五发布版本，需要保留备份', '{}'),
        (3, 'Bot认错', '{}'), (4, '[表情]', '{}'),
        (5, '旧的低质量总结', '{ "summary_quality": "low" }'),
        (6, '不可覆盖的损坏数据', '{'), (7, '哈哈', '{"recall_excluded":true}'),
    ]:
        connection.execute('INSERT INTO documents VALUES (?, ?, ?)', (identity, text, metadata))
    connection.commit()
    yield connection
    connection.close()


def test_audit_scan_and_exact_rollback(audit_db):
    before = audit_db.execute('SELECT * FROM documents').fetchall()
    assert {item['id'] for item in scan(audit_db, _RULES)} == {1, 3, 4, 5}
    assert audit_db.execute('SELECT * FROM documents').fetchall() == before
    batch, count = apply(audit_db, _RULES)
    assert count == 4
    assert scan(audit_db, _RULES) == []
    assert is_non_substantive('哈哈', {'recall_excluded': 1})
    assert rollback(audit_db, batch) == 4
    assert audit_db.execute('SELECT * FROM documents').fetchall() == before


def test_audit_rollback_refuses_later_changes_atomically(audit_db):
    batch, _ = apply(audit_db, _RULES)
    audit_db.execute('UPDATE documents SET metadata=? WHERE id=5', ('{"new":"business data"}',))
    audit_db.commit()
    with pytest.raises(ValueError, match='changed after audit'):
        rollback(audit_db, batch)
    assert json.loads(audit_db.execute('SELECT metadata FROM documents WHERE id=1').fetchone()[0])['recall_excluded'] == 1
    assert audit_db.execute('SELECT metadata FROM documents WHERE id=5').fetchone()[0] == '{"new":"business data"}'


def test_reviewed_ids_and_rules_only(audit_db):
    batch, count = apply(audit_db, {'banter'}, {1, 5})
    assert count == 1
    assert rollback(audit_db, batch) == 1


@pytest.mark.parametrize('text', ['林舟喜欢哈哈这个表情包', '林舟发现Bot认错的原因是解析器错误，已修复', '林舟确认周五发布', '群友调侃后确认以后每周备份'])
def test_noise_rules_do_not_match_fact_substrings(text):
    assert classify(text, {}) == []

@pytest.mark.asyncio
async def test_manual_no_memory_advances_cursor_and_skips_write():
    from unittest.mock import patch
    from tests.test_features_77_76_74_59 import _make_command_handler, _MockEvent
    processor = Mock()
    processor.process_conversation = AsyncMock(return_value=('', {'processing_status': 'processed_no_memory'}, 0))
    handler = _make_command_handler(memory_processor=processor)
    handler.conversation_manager.commit_summary_cursor = AsyncMock(return_value=True)
    with patch('astrbot_plugin_livingmemory.core.utils.get_persona_id', new=AsyncMock(return_value='p')):
        replies = [reply async for reply in handler.handle_summarize(_MockEvent())]
    assert any('没有需要保存的长期记忆' in reply for reply in replies)
    handler.conversation_manager.commit_summary_cursor.assert_awaited_once()
    handler.memory_engine.add_memory.assert_not_awaited()
    processor.classify_atoms_from_metadata.assert_not_called()


@pytest.mark.asyncio
async def test_source_import_skips_empty_generated_memory():
    from tests.test_page_api import FakeMemoryEngine, FakePlugin, PluginPageApi, _mock_page_request, _patch_page_request
    engine = FakeMemoryEngine()
    engine.get_memory_import_keys = AsyncMock(return_value=set())
    engine.add_memory = AsyncMock()
    processor = Mock()
    processor.process_conversation = AsyncMock(return_value=('', {'processing_status': 'processed_no_memory'}, 0))
    plugin = FakePlugin(memory_engine=engine)
    plugin.initializer.memory_processor = processor
    api = PluginPageApi(plugin)
    req = _mock_page_request(get_json={
        'format': 'json', 'dry_run': False,
        'content': json.dumps([{'role': 'user', 'content': '哈哈'}, {'role': 'assistant', 'content': '嘿嘿'}]),
    })
    with _patch_page_request(req):
        result = await api.import_memories()
    assert result['data']['processed_no_memory_count'] == 1
    assert result['data']['imported_count'] == 0
    engine.add_memory.assert_not_awaited()
    processor.classify_atoms_from_metadata.assert_not_called()


@pytest.mark.asyncio
async def test_resummarize_empty_keeps_original_record():
    from tests.test_page_api import FakeMemoryEngine, FakePlugin, PluginPageApi, _mock_page_request, _patch_page_request
    engine = FakeMemoryEngine()
    engine.get_memory_record = AsyncMock(return_value={'id': 7, 'text': 'original', 'metadata': {}})
    engine.get_memory_source = AsyncMock(return_value=[
        {'id': 1, 'session_id': 's', 'role': 'user', 'content': '哈哈', 'sender_id': 'u', 'timestamp': 1, 'metadata': {}},
        {'id': 2, 'session_id': 's', 'role': 'assistant', 'content': '嘿嘿', 'sender_id': 'b', 'timestamp': 2, 'metadata': {}},
    ])
    engine.replace_memory = AsyncMock()
    processor = Mock()
    processor.process_conversation = AsyncMock(return_value=('', {'processing_status': 'processed_no_memory'}, 0))
    plugin = FakePlugin(memory_engine=engine)
    plugin.initializer.memory_processor = processor
    api = PluginPageApi(plugin)
    with _patch_page_request(_mock_page_request(get_json={'memory_id': 7})):
        result = await api.resummarize_memory()
    assert result['data']['processing_status'] == 'processed_no_memory'
    engine.replace_memory.assert_not_awaited()
