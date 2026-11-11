"""Exact calendar boundaries, provenance validation and both LLM entry points."""
import json
from datetime import datetime
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
from astrbot_plugin_livingmemory.core.managers.consolidation_manager import MemoryConsolidationManager
from astrbot_plugin_livingmemory.core.memory_source import select_memory_sources
from astrbot_plugin_livingmemory.core.models.conversation_models import Message
from astrbot_plugin_livingmemory.core.processors.memory_processor import MemoryProcessor
from astrbot_plugin_livingmemory.core.tools.memory_search_tool import MemorySearchTool
from astrbot_plugin_livingmemory.core.utils.event_memory import normalize_events, parse_overview_window
from astrbot_plugin_livingmemory.core.utils.injection_adapter import InjectionAdapter


def ts(text):
    return datetime.fromisoformat(text+'+08:00').timestamp()


NOW=ts('2026-10-05T16:00:00')


@pytest.mark.parametrize('query,start,end', [
    ('昨天晚上发生了什么事情？','2026-10-04T18:00:00','2026-10-05T06:00:00'),
    ('昨晚群里聊了啥','2026-10-04T18:00:00','2026-10-05T06:00:00'),
    ('前天晚上聊了哪些事情','2026-10-03T18:00:00','2026-10-04T06:00:00'),
    ('昨天发生了什么','2026-10-04T00:00:00','2026-10-05T00:00:00'),
    ('2026-10-04晚上聊了什么','2026-10-04T18:00:00','2026-10-05T06:00:00'),
])
def test_calendar_range_is_timezone_stable(query,start,end):
    window=parse_overview_window(query,NOW)
    assert (window.start,window.end)==(ts(start),ts(end))
    assert window.to_dict()['timezone']=='Asia/Shanghai'


@pytest.mark.parametrize('query', ['昨晚我太累了','雪地的角色叫什么','2026-02-30晚上发生了什么', '昨晚发生什么'+('x'*400)])
def test_not_every_mention_or_expanded_history_is_an_overview(query):
    assert parse_overview_window(query,NOW) is None


def test_window_does_not_include_future_messages():
    now=ts('2026-10-05T02:00:00')
    assert parse_overview_window('昨晚发生了什么',now).end==now


def message(i,text,when,sender='u1',name='林舟',role='user'):
    return Message(id=i,session_id='g1',role=role,content=text,sender_id=sender,
                   sender_name=name,platform='qq',timestamp=when,metadata={})


def test_event_times_and_authors_cannot_be_supplied_by_model():
    messages=[message(7,'我上半年住过毛坯房',ts('2026-10-04T22:00:00')),
              message(8,'明天可能开会',ts('2026-10-05T01:00:00'),'u2','夜航')]
    events=normalize_events([{'kind':'self_report','summary':'林舟自述上半年经历','source_refs':['m0'],
                             'observed_start':9999999999,'sender_id':'bot'},
                            {'kind':'plan','summary':'夜航提出明天可能开会','source_refs':['m1']}],messages)
    assert events[0]['source_message_ids']==[7]
    assert events[0]['observed_start']==messages[0].timestamp
    assert events[0]['speakers'][0]['sender_id']=='u1'
    assert events[1]['kind']=='plan'


@pytest.mark.parametrize('refs', [[],['unknown'],['m0','unknown'],'m0',[12]])
def test_invalid_or_partly_fabricated_sources_drop_the_entire_event(refs):
    assert not normalize_events([{'summary':'伪造事件','source_refs':refs}],[message(1,'你好',NOW)])


@pytest.mark.asyncio
async def test_objective_generation_does_not_load_persona_or_duplicate_event():
    context=Mock()
    context.persona_manager.get_persona=AsyncMock(side_effect=AssertionError('Must not load persona'))
    llm=SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(completion_text=json.dumps({
        'summary':'人格化的旧摘要','topics':['会议'],'key_facts':[], 'participants':['林舟'],
        'sentiment':'neutral','importance':.6,
        'events':[{'kind':'plan','summary':'林舟在群里提出明天可能开会','source_refs':['m0']}],
    },ensure_ascii=False))))
    processor=MemoryProcessor(context=context,llm_provider=llm)
    content,metadata,importance=await processor.process_conversation([message(1,'明天可能开会',NOW)],True,'persona')
    assert content.count('林舟在群里提出明天可能开会')==1
    assert metadata['persona_summary']==''
    assert metadata['event_source_status']=='verified'
    assert metadata['events'][0]['source_message_ids']==[1]
    context.persona_manager.get_persona.assert_not_awaited()
    atoms=processor.classify_atoms_from_metadata(metadata,importance,'g1','p1')
    assert atoms[0].atom_type.value=='episodic'
    assert atoms[0].metadata['event_kind']=='plan'
    assert atoms[0].expires_at>NOW


@pytest_asyncio.fixture
async def evidence_engine(tmp_path):
    main=tmp_path/'livingmemory.db'
    db=await aiosqlite.connect(main)
    await db.execute('CREATE TABLE documents(id INTEGER PRIMARY KEY,text TEXT,metadata TEXT)')
    metadata={'session_id':'g1','persona_id':'p1','create_time':9999999999,'events':[]}
    await db.execute('INSERT INTO documents VALUES(1,?,?)',('旧摘要',json.dumps(metadata)))
    await db.commit()
    archive=await aiosqlite.connect(tmp_path/'conversations.db')
    await archive.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,sender_id TEXT,sender_name TEXT,platform TEXT,timestamp REAL,metadata TEXT)')
    samples=[(1,'g1',ts('2026-10-04T18:00:00')),(2,'g1',ts('2026-10-05T05:59:59')),
             (3,'g1',ts('2026-10-05T06:00:00')),(4,'g1',ts('2026-10-04T17:59:59')),
             (5,'g2',ts('2026-10-04T20:00:00'))]
    await archive.executemany('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)',[(i,s,'user','源消息','u1','林舟','qq',t,'{}') for i,s,t in samples])
    await archive.commit()
    await archive.close()
    engine=MemoryEngine(str(main),SimpleNamespace())
    engine.db_connection=db
    try:
        yield engine,metadata
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_raw_fallback_has_exact_half_open_bounds(evidence_engine):
    engine,_=evidence_engine
    result=await engine.recall_events_window(parse_overview_window('昨晚发生什么',NOW),'g1','p1')
    assert [row['message_id'] for row in result['raw_messages']]==[1,2]
    assert result['coverage']['complete']
    assert result['events']==[]


@pytest.mark.asyncio
async def test_wrong_group_or_persona_cannot_access_raw_fallback(evidence_engine):
    engine,_=evidence_engine
    for group,persona in [('g2','p1'),('g1','p2')]:
        result=await engine.recall_events_window(parse_overview_window('昨晚发生什么',NOW),group,persona)
        assert result['events']==[] and result['raw_messages']==[]


@pytest.mark.asyncio
async def test_event_sources_override_write_time_and_reject_crossing_summary(evidence_engine):
    engine,metadata=evidence_engine
    metadata['events']=[{'event_id':'a','kind':'discussion','summary':'18点的讨论','source_verified':True,'source_message_ids':[1],'observed_start':9999999999},
                        {'event_id':'b','kind':'discussion','summary':'跨界摘要不得泄露6点之后的内容','source_verified':True,'source_message_ids':[2,3]}]
    await engine.db_connection.execute('UPDATE documents SET metadata=?',(json.dumps(metadata),))
    await engine.db_connection.commit()
    result=await engine.recall_events_window(parse_overview_window('昨晚发生什么',NOW),'g1','p1')
    assert [event['event_id'] for event in result['events']]==['a']
    assert result['events'][0]['observed_start']=='2026-10-04T18:00:00+08:00'
    assert [row['message_id'] for row in result['raw_messages']]==[2]
    assert result['coverage']['partial_or_missing_source_events']==1
    deep=await engine.recall_events_window(parse_overview_window('昨晚发生什么',NOW),'g1','p1',include_source=True)
    assert deep['events'][0]['source_messages'][0]['message_id']==1
    assert deep['events'][0]['source_messages'][0]['content']=='源消息'


@pytest.mark.asyncio
async def test_old_injection_and_recall_do_not_route_time_questions_to_events(monkeypatch):
    evidence={'window':{},'coverage':{'complete':False},'events':[],'raw_messages':[]}
    engine=SimpleNamespace(recall_events_window=AsyncMock(return_value=evidence),search_memories=AsyncMock(return_value=[]))
    config=ConfigManager({'recall_engine':{'passive_injection_enabled':True,'inject_with_recent_context':True},'filtering_settings':{'use_persona_filtering':False}})
    event=Mock(unified_msg_origin='g1')
    event.get_message_str.return_value='昨晚发生什么'
    event.get_message_type.return_value=MessageType.GROUP_MESSAGE
    request=SimpleNamespace(prompt='昨晚发生什么',extra_user_content_parts=[],contexts=[],system_prompt='')
    conversation=Mock()
    conversation.get_context=AsyncMock(return_value=[])
    recall=MemoryRecall(Mock(),config,engine,conversation,MessageUtils(config,conversation),InjectionAdapter())
    await recall.handle_memory_recall(event,request)
    assert len(request.extra_user_content_parts) == 1
    assert "[当前会话]" in request.extra_user_content_parts[0].text
    engine.recall_events_window.assert_not_awaited()
    tool=MemorySearchTool(context=Mock(),config_manager=config,memory_engine=engine)
    result=json.loads(await tool.call(SimpleNamespace(context=SimpleNamespace(event=event)),'昨晚发生什么'))
    assert result['results']==[]
    assert engine.search_memories.await_count==2
    engine.recall_events_window.assert_not_awaited()


def test_low_importance_events_keep_only_cited_sources():
    rows=[message(1,'一条事件',NOW),message(2,'未引用闲聊',NOW)]
    metadata={'summary_style':'events','events':[{'source_verified':True,'source_message_ids':[1]}]}
    assert [row['id'] for row in select_memory_sources(rows,metadata,.5,.8)]==[1]
    assert select_memory_sources(rows,{'summary_style':'persona'},.5,.8) is None
    assert len(select_memory_sources(rows,metadata,.9,.8))==2
    metadata['events'][0]['source_verified']=False
    assert select_memory_sources(rows,metadata,.5,.8) is None


@pytest.mark.asyncio
async def test_legacy_consolidation_protects_event_provenance():
    old={'id':1,'metadata':{'summary_style':'persona'}}
    event={'id':2,'metadata':{'summary_style':'events'}}
    engine=SimpleNamespace(get_consolidation_candidates=AsyncMock(return_value=[old,event]))
    manager=MemoryConsolidationManager(engine,Mock(),ConfigManager({}))
    assert await manager._query_candidates({})==[old]


async def save_test_sources(engine,metadata,rows):
    await engine.db_connection.execute('CREATE TABLE memory_sources(memory_id INTEGER PRIMARY KEY,source_json TEXT)')
    await engine.db_connection.execute('INSERT INTO memory_sources VALUES(1,?)',(json.dumps(rows),))
    metadata['events']=[{'event_id':'saved','summary':'保留的讨论','kind':'discussion',
                        'source_verified':True,'source_message_ids':[1]}]
    await engine.db_connection.execute('UPDATE documents SET metadata=?',(json.dumps(metadata),))
    await engine.db_connection.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize('remove_archive',[False,True])
async def test_retained_citations_survive_archive_cleanup(evidence_engine,remove_archive):
    engine,metadata=evidence_engine
    rows=[message(1,'保留的原文',ts('2026-10-04T18:00:00')).to_dict(),
          message(3,'范围外不得带入',ts('2026-10-05T06:00:00')).to_dict()]
    await save_test_sources(engine,metadata,rows)
    archive_path=__import__('pathlib').Path(engine.db_path).parent/'conversations.db'
    if remove_archive:
        archive_path.unlink()
    else:
        async with aiosqlite.connect(archive_path) as db:
            await db.execute('DELETE FROM messages WHERE session_id="g1"')
            await db.commit()
    result=await engine.recall_events_window(parse_overview_window('昨晚发生什么',NOW),'g1','p1',True)
    assert len(result['events'])==1
    assert result['events'][0]['source_messages'][0]['content']=='保留的原文'
    assert result['events'][0]['speakers'][0]['sender_id']=='u1'
    assert result['coverage']['archived_messages_in_window']==0
    assert result['coverage']['retained_source_only_messages']==1
    assert result['coverage']['complete'] is False


@pytest.mark.asyncio
async def test_archive_wins_and_bad_retained_rows_do_not_cross_scope(evidence_engine):
    engine,metadata=evidence_engine
    rows=[message(1,'错误的备份文本',ts('2026-10-04T19:00:00'),'wrong').to_dict()]
    wrong_group=message(8,'别群消息',NOW).to_dict(); wrong_group['session_id']='g2'
    missing_time=message(9,'缺失时间',NOW).to_dict(); del missing_time['timestamp']
    rows.extend([wrong_group,missing_time])
    await save_test_sources(engine,metadata,rows)
    result=await engine.recall_events_window(parse_overview_window('昨晚发生什么',NOW),'g1','p1',True)
    assert result['events'][0]['source_messages'][0]['content']=='源消息'
    assert result['events'][0]['speakers'][0]['sender_id']=='u1'
    assert result['coverage']['known_messages_in_window']==2
    assert result['coverage']['complete']


@pytest.mark.asyncio
async def test_conflicting_retained_copies_are_not_used(evidence_engine):
    engine,metadata=evidence_engine
    rows=[message(6,'版本一',ts('2026-10-04T19:00:00')).to_dict(),
          message(6,'版本二',ts('2026-10-04T19:00:00'),'u2').to_dict()]
    await save_test_sources(engine,metadata,rows)
    metadata['events'][0]['source_message_ids']=[6]
    await engine.db_connection.execute('UPDATE documents SET metadata=?',(json.dumps(metadata),))
    await engine.db_connection.commit()
    result=await engine.recall_events_window(parse_overview_window('昨晚发生什么',NOW),'g1','p1')
    assert result['events']==[]
    assert result['coverage']['conflicting_retained_message_ids']==1
    assert result['coverage']['complete'] is False


@pytest.mark.asyncio
async def test_event_documents_intersect_adapter_policy(evidence_engine):
    from astrbot_plugin_livingmemory.core.memory_scope import adapter_access

    engine, metadata = evidence_engine
    metadata["adapter_id"] = "qq-b"
    await engine.db_connection.execute("UPDATE documents SET metadata=? WHERE id=1", (json.dumps(metadata),))
    await engine.db_connection.commit()
    window = parse_overview_window("昨晚发生了什么", NOW)
    config = {"filtering_settings": {"shared_adapter_ids": []}}
    with adapter_access(config, "qq-a"):
        result = await engine.recall_events_window(window, "g1", "p1")
        assert result["raw_messages"] == []
        assert result["coverage"]["status"] == "archive_or_memory_scope_unavailable"
    with adapter_access(config, "qq-b"):
        result = await engine.recall_events_window(window, "g1", "p1")
        assert result["raw_messages"]
