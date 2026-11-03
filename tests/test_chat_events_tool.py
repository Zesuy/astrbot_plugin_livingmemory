"""Integration contracts exercised on real SQLite, not implementation mocks."""
import asyncio
import json
import sqlite3
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from astrbot_plugin_livingmemory.storage.conversation_store import ConversationStore
from astrbot_plugin_livingmemory.core.models.archive_models import ArchiveMessage
from astrbot_plugin_livingmemory.core.models.conversation_models import Message
from astrbot_plugin_livingmemory.core.managers.archive_query_service import ArchiveQueryService,window_for
from astrbot_plugin_livingmemory.core.tools.chat_events_tool import ChatEventsTool
from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
from astrbot_plugin_livingmemory.main import LivingMemoryPlugin

TZ=ZoneInfo('Asia/Shanghai')
NOW=datetime(2026,10,5,16,tzinfo=TZ).timestamp()

@pytest_asyncio.fixture
async def store(tmp_path):
    value=ConversationStore(str(tmp_path/'conversations.db'))
    await value.initialize()
    value.archive_enabled=True
    yield value
    await value.close()

def record(uid,text,person='u1',nickname='雪地',scope='qq:group:1',stamp=None,reply=None,mentions=None):
    return ArchiveMessage(uid=uid,native_key=uid,conversation=scope,sender_id=person,
        nickname=nickname,platform='qq',text=text,sent_at=stamp or NOW-100,
        time_quality='platform_sent',reply_key=reply,mentions=mentions or [])

@pytest.mark.asyncio
async def test_about_retrieves_other_authors_short_reply_without_keywords(store):
    await store.archive_record(record('presence','在',stamp=NOW-600))
    await store.archive_record(record('question','雪地的跑团角色名字叫什么','u2','四格蝶',stamp=NOW-300,mentions=['u1']))
    await store.archive_record(record('guess','是个学者','bot','鼠鼠',stamp=NOW-200,reply='question'))
    await store.archive_record(record('answer','不可名状之孤独草逼者','u2','四格蝶',stamp=NOW-100,reply='guess'))
    service=ArchiveQueryService(store)
    result=await service.query(conversation='qq:group:1',requester='reader',question='雪地的跑团角色名',person='雪地',keywords=['跑团','角色'],page_size=100,now=NOW)
    assert '不可名状之孤独草逼者' in '\n'.join(m['text'] for m in result['messages'])
    answer=next(m for m in result['messages'] if m['source_uid']=='answer')
    assert answer['sender_id']=='u2'
    assert 'relationship' in result['interpretation']

@pytest.mark.asyncio
async def test_historical_query_does_not_see_later_correction(store):
    await store.archive_record(record('q','雪地跑团角色',stamp=NOW-200))
    await store.archive_record(record('late','未来纠正名字','u2','四格蝶',stamp=NOW+60,reply='q'))
    r=await ArchiveQueryService(store).query(conversation='qq:group:1',requester='r',question='跑团角色',keywords=['跑团'],now=NOW)
    assert 'late' not in [m['source_uid'] for m in r['messages']]

@pytest.mark.asyncio
async def test_speaker_filter_and_about_are_distinct(store):
    await store.archive_record(record('a','游戏介绍'))
    await store.archive_record(record('b','雪地推荐的游戏','u2','四格蝶',mentions=['u1']))
    service=ArchiveQueryService(store)
    result=await service.query(conversation='qq:group:1',requester='r',question='雪地说了什么',person='雪地',person_role='speaker',mode='time_read',time_range='今天',now=NOW)
    assert [m['sender_id'] for m in result['messages']]==['u1']

@pytest.mark.asyncio
async def test_same_alias_ambiguity_is_not_identity_merge(store):
    await store.archive_record(record('a','one','u1','同名'))
    await store.archive_record(record('b','two','u2','同名'))
    r=await ArchiveQueryService(store).query(conversation='qq:group:1',requester='r',question='同名是谁',person='同名',now=NOW)
    assert r['status']=='ambiguous'
    assert len(r['resolved_query']['accounts'])==2

@pytest.mark.asyncio
async def test_paging_survives_new_messages_and_nicknames_but_not_clear(store):
    for n in range(3):
        await store.archive_record(record(str(n),'内容',stamp=NOW-100+n))
    service=ArchiveQueryService(store)
    first=await service.query(conversation='qq:group:1',requester='r',question='今天发生什么',mode='time_read',time_range='今天',page_size=1,now=NOW)
    await store.archive_record(record('new','新增','u3','新的昵称',stamp=NOW-50))
    nextpage=await service.query(conversation='qq:group:1',requester='r',question='继续',cursor=first['cursor'],page_size=10)
    assert {m['source_uid'] for m in nextpage['messages']}=={'1','2'}
    await store.clear_archive('qq:group:1')
    invalid=await service.query(conversation='qq:group:1',requester='r',question='继续',cursor=first['cursor'])
    assert invalid['status']=='invalidated'

@pytest.mark.asyncio
async def test_handles_do_not_grant_other_scope_or_requester(store):
    await store.archive_record(record('a','历史'))
    service=ArchiveQueryService(store)
    r=await service.query(conversation='qq:group:1',requester='r',question='历史',now=NOW)
    other=await service.query(conversation='qq:group:2',requester='r',question='读',mode='read',evidence_handle=r['evidence_handle'])
    assert other['status']=='unavailable'
    other=await service.query(conversation='qq:group:1',requester='another',question='读',mode='read',evidence_handle=r['evidence_handle'])
    assert other['status']=='unavailable'

@pytest.mark.asyncio
async def test_archive_projection_survives_trim_without_double_count(store):
    rec=record('a','历史')
    msg=lambda:Message(0,rec.conversation,'user','历史','u1',metadata=rec.metadata())
    await store.add_message(msg())
    await store.set_session_metadata(rec.conversation,'last_summarized_index',1)
    await store.trim_session_messages(rec.conversation,1)
    await store.add_message(msg())
    assert await store.get_message_count(rec.conversation)==0
    r=await ArchiveQueryService(store).query(conversation=rec.conversation,requester='r',question='历史',now=NOW)
    assert len(r['messages'])==1

@pytest.mark.asyncio
async def test_transaction_failure_rolls_back_archive_and_work(store,monkeypatch):
    rec=record('a','历史')
    insert=store._archive_insert
    async def fail(value):
        await insert(value)
        raise RuntimeError('injected_write_failure')
    monkeypatch.setattr(store,'_archive_insert',fail)
    with pytest.raises(RuntimeError):
        await store.add_message(Message(0,rec.conversation,'user','历史','u1',metadata=rec.metadata()))
    rows=await store.read_connection.execute_fetchall('SELECT COUNT(*) FROM archive_messages')
    assert rows[0][0]==0
    assert await store.get_message_count(rec.conversation)==0

@pytest.mark.asyncio
async def test_cancellation_before_commit_rolls_back_and_writer_recovers(store):
    with pytest.raises(asyncio.CancelledError):
        async with store.transaction():
            await store._archive_insert(record('cancel','旧'))
            raise asyncio.CancelledError()
    await store.archive_record(record('next','新'))
    rows=await store.read_connection.execute_fetchall('SELECT uid FROM archive_messages')
    assert [r[0] for r in rows]==['next']

@pytest.mark.asyncio
async def test_pre_clear_accepted_source_cannot_be_reinserted(store):
    stale=record('stale','之前入队')
    await store.clear_archive(stale.conversation)
    with pytest.raises(ValueError,match='source_epoch_revoked'):
        await store.archive_record(stale)
    fresh=record('fresh','清除后新的')
    fresh.epoch=store.accepted_epoch(fresh.conversation)
    await store.archive_record(fresh)
    old=[{'session_id':fresh.conversation,'content':'旧','metadata':{'archive_epoch':0}}]
    assert await store.filter_sources(old)==[]

@pytest.mark.asyncio
async def test_legacy_source_compatibility_and_global_quarantine_after_clear(store):
    raw=[{'session_id':'livingmemory:global','content':'旧的原文'}]
    assert await store.filter_sources(raw)==raw
    await store.clear_archive('qq:group:1')
    assert await store.filter_sources(raw)==[]

@pytest.mark.asyncio
async def test_legacy_backfill_is_idempotent_and_time_honest(store):
    store.archive_enabled=False
    await store.add_message(Message(0,'qq:group:1','user','旧档案','u1',timestamp=NOW-20))
    one=await store.backfill_archive()
    two=await store.backfill_archive()
    assert one['migrated']==1 and two['migrated']==0
    r=await ArchiveQueryService(store).query(conversation='qq:group:1',requester='r',question='旧档案',now=NOW)
    assert r['messages'][0]['time_quality']=='legacy_saved'

@pytest.mark.asyncio
async def test_read_expands_truncated_message_in_same_scope(store):
    await store.archive_record(record('long','原文'*3000))
    service=ArchiveQueryService(store)
    r=await service.query(conversation='qq:group:1',requester='r',question='原文',now=NOW)
    assert r['messages'][0]['truncated']
    read=await service.query(conversation='qq:group:1',requester='r',question='展开',mode='read',evidence_handle=r['messages'][0]['evidence_handle'])
    assert len(read['messages'][0]['text'])==6000

@pytest.mark.asyncio
async def test_online_backup_contains_independent_archive(store,tmp_path):
    await store.archive_record(record('a','档案'))
    backup=tmp_path/'backup.db'
    await store.backup_conversations(str(backup))
    with sqlite3.connect(backup) as conn:
        assert conn.execute('SELECT text FROM archive_messages').fetchone()[0]=='档案'

def test_night_bounds_and_explicit_dates():
    start,end=window_for('昨晚','','',NOW)
    assert datetime.fromtimestamp(start,TZ).isoformat()=='2026-10-04T18:00:00+08:00'
    assert datetime.fromtimestamp(end,TZ).isoformat()=='2026-10-05T06:00:00+08:00'
    with pytest.raises(ValueError):
        window_for('','2026-10-04T18:00','2026-10-05T06:00',NOW)

def test_archive_tool_registers_without_fuzzy_providers():
    plugin=LivingMemoryPlugin.__new__(LivingMemoryPlugin)
    plugin.context=Mock()
    plugin.config_manager=ConfigManager({'archive':{'enabled':True}})
    plugin._archive_tool_registered=False
    plugin.archive_runtime=SimpleNamespace(ready=True,enabled=True)
    plugin._register_archive_tool_if_needed()
    plugin._register_archive_tool_if_needed()
    plugin.context.add_llm_tools.assert_called_once()
    assert isinstance(plugin.context.add_llm_tools.call_args.args[0],ChatEventsTool)

def test_no_time_regex_or_archive_query_in_original_injection_or_old_recall():
    import inspect
    from astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall import MemoryRecall
    from astrbot_plugin_livingmemory.core.tools.memory_search_tool import MemorySearchTool
    for source in [inspect.getsource(MemoryRecall),inspect.getsource(MemorySearchTool)]:
        assert 'parse_overview_window' not in source
        assert 'recall_events_window' not in source
        assert 'archive_runtime.service.query' not in source

@pytest.mark.asyncio
async def test_neighbors_are_relative_to_conversation_not_global_id(store):
    await store.archive_record(record('q','雪地的跑团角色是什么',stamp=NOW-100))
    for n in range(100):
        await store.archive_record(record('other'+str(n),'无关',scope='qq:group:2',stamp=NOW-99))
    await store.archive_record(record('short','不可名状之孤独草逼者','u2','四格蝶',stamp=NOW-98))
    r=await ArchiveQueryService(store).query(conversation='qq:group:1',requester='r',question='雪地角色',keywords=['跑团'],now=NOW)
    assert 'short' in [m['source_uid'] for m in r['messages']]

@pytest.mark.asyncio
async def test_paging_retry_is_stable_and_long_tail_is_readable(store):
    await store.archive_record(record('long','开头'+('原文'*9000)+'最后的名字'))
    await store.archive_record(record('second','原文2'))
    service=ArchiveQueryService(store)
    r=await service.query(conversation='qq:group:1',requester='r',question='今天',mode='time_read',time_range='今天',page_size=1,now=NOW)
    first=await service.query(conversation='qq:group:1',requester='r',question='继续',cursor=r['cursor'])
    retry=await service.query(conversation='qq:group:1',requester='r',question='继续',cursor=r['cursor'])
    assert first['messages']==retry['messages']
    handle=r['messages'][0]['evidence_handle']
    chunks=[]
    while handle:
        page=await service.query(conversation='qq:group:1',requester='r',question='展开',mode='read',evidence_handle=handle)
        chunks.append(page['messages'][0]['text'])
        handle=page['messages'][0]['next_text_handle']
    assert ''.join(chunks).endswith('最后的名字')

@pytest.mark.asyncio
async def test_optional_graph_failure_does_not_break_archive_query(store):
    await store.archive_record(record('a','跑团原文'))
    service=ArchiveQueryService(store)
    service.alias_resolver=AsyncMock(side_effect=RuntimeError('graph unavailable'))
    r=await service.query(conversation='qq:group:1',requester='r',question='雪地的跑团',person='雪地',keywords=['跑团'],now=NOW)
    assert r['messages']
    assert r['resolved_query']['identity_hint']=='alias_hint_unavailable'

@pytest.mark.asyncio
async def test_clear_cancellation_waits_for_durable_epoch(store,monkeypatch):
    begun=asyncio.Event()
    resume=asyncio.Event()
    commit=store.connection.commit
    async def blocked_commit():
        begun.set()
        await resume.wait()
        await commit()
    monkeypatch.setattr(store.connection,'commit',blocked_commit)
    clear=asyncio.create_task(store.clear_archive('qq:group:1'))
    await begun.wait()
    clear.cancel()
    resume.set()
    with pytest.raises(asyncio.CancelledError):
        await clear
    assert store.accepted_epoch('qq:group:1')==1
    monkeypatch.setattr(store.connection,'commit',commit)
    fresh=record('fresh','新')
    fresh.epoch=1
    await store.archive_record(fresh)

@pytest.mark.asyncio
async def test_query_only_does_not_archive_work_and_new_work_keeps_epoch(store):
    from astrbot_plugin_livingmemory.core.managers.conversation_manager import ConversationManager
    manager=ConversationManager(store)
    store.capture_authorizer=AsyncMock(return_value=False)
    await store.clear_archive('qq:group:1')
    extras={'_lm_work_epoch':1}
    event=SimpleNamespace(unified_msg_origin='qq:group:1',get_platform_name=lambda:'qq',
        get_sender_id=lambda:'u1',get_sender_name=lambda:'雪地',get_self_id=lambda:'bot',
        get_extra=lambda k,default=None:extras.get(k,default),set_extra=lambda k,v:extras.update({k:v}))
    await manager.add_message_from_event(event,'user','新输入')
    assistant=await manager.add_message_from_event(event,'assistant','新回答')
    assert assistant.metadata['archive_epoch']==1
    assert await store.get_message_count('qq:group:1')==2
    assert not await store.read_connection.execute_fetchall('SELECT uid FROM archive_messages')

@pytest.mark.asyncio
async def test_disabled_archive_pins_input_epoch_before_delayed_model_reply(store):
    from astrbot_plugin_livingmemory.core.archive_runtime import ArchiveRuntime
    from astrbot_plugin_livingmemory.core.managers.conversation_manager import ConversationManager
    runtime=ArchiveRuntime.__new__(ArchiveRuntime)
    runtime.store=store
    runtime.ready=True
    extras={}
    event=SimpleNamespace(unified_msg_origin='qq:group:1',get_platform_name=lambda:'qq',
        get_sender_id=lambda:'u1',get_sender_name=lambda:'雪地',get_self_id=lambda:'bot',
        get_extra=lambda k,default=None:extras.get(k,default),set_extra=lambda k,v:extras.update({k:v}))
    runtime.accept_work(event)
    await store.clear_archive(event.unified_msg_origin)
    manager=ConversationManager(store)
    with pytest.raises(ValueError,match='source_epoch_revoked'):
        await manager.add_message_from_event(event,'assistant','旧请求的晚回答')

def test_native_component_enum_is_decoded_as_text_at_and_reply():
    from enum import Enum
    from astrbot_plugin_livingmemory.core.models.archive_models import capture_event
    class ComponentType(Enum):
        Plain='Plain'
        At='At'
        Reply='Reply'
    extras={}
    event=SimpleNamespace(unified_msg_origin='qq:GroupMessage:1',message_obj=SimpleNamespace(message_id='2',timestamp=NOW,raw_message={}),
        get_platform_name=lambda:'qq',get_sender_id=lambda:'u1',get_sender_name=lambda:'雪地',get_self_id=lambda:'bot',get_group_id=lambda:'1',
        get_message_str=lambda:'真实原文',get_messages=lambda:[SimpleNamespace(type=ComponentType.Plain,text='真实原文'),SimpleNamespace(type=ComponentType.At,qq='u2'),SimpleNamespace(type=ComponentType.Reply,id='1',message_str='此前的问题')],
        get_extra=lambda k,default=None:extras.get(k,default),set_extra=lambda k,v:extras.update({k:v}))
    r=capture_event(event)
    assert '真实原文' in r.text
    assert r.mentions==['u2']
    assert r.reply_key and r.reply_key.endswith(':1')
    assert r.components[0]['type']=='plain'

@pytest.mark.asyncio
async def test_empty_legacy_message_does_not_stall_time_pagination(store):
    for n,text in enumerate(['正常','', '之后的消息']):
        await store.archive_record(record(str(n),text,stamp=NOW-100+n))
    service=ArchiveQueryService(store)
    r=await service.query(conversation='qq:group:1',requester='r',question='今天',mode='time_read',time_range='今天',page_size=1,now=NOW)
    all_messages=list(r['messages'])
    while r.get('cursor'):
        r=await service.query(conversation='qq:group:1',requester='r',question='继续',cursor=r['cursor'],page_size=1)
        all_messages.extend(r['messages'])
    assert [m['source_uid'] for m in all_messages]==['0','1','2']
    assert all_messages[1]['empty_text']

@pytest.mark.asyncio
async def test_unobserved_nickname_can_find_mentions_without_inventing_alias(store):
    await store.archive_record(record('q','玉哥的跑团角色名字','u2','四格蝶'))
    service=ArchiveQueryService(store)
    r=await service.query(conversation='qq:group:1',requester='r',question='玉哥角色',person='玉哥',keywords=['跑团'],now=NOW)
    assert r['messages']
    assert r['resolved_query']['accounts']==[]
    assert r['resolved_query']['identity_resolution']=='literal_mentions_only_unresolved'
    assert await store.read_connection.execute_fetchall("SELECT nickname FROM archive_names WHERE nickname='玉哥'")==[]

@pytest.mark.asyncio
async def test_explicit_admin_alias_resolves_without_graph_and_is_separate_from_observation(store):
    await store.archive_record(record('q','跑团角色'))
    service=ArchiveQueryService(store,identity_aliases='qq:u1=玉哥')
    r=await service.query(conversation='qq:group:1',requester='r',question='玉哥角色',person='玉哥',keywords=['跑团'],now=NOW)
    account=r['resolved_query']['accounts'][0]
    assert account['account_id']=='u1'
    assert account['configured_names']==['玉哥']
    assert '玉哥' not in account['observed_names']

@pytest.mark.asyncio
async def test_tool_input_is_bounded_before_tokenization_or_database_scan(store):
    r=await ArchiveQueryService(store).query(conversation='qq:group:1',requester='r',question='长'*4001)
    assert r['status']=='error'

@pytest.mark.asyncio
async def test_person_related_original_without_topic_words_is_a_labelled_lead(store):
    await store.archive_record(record('command','登记：林舟，力量40',stamp=NOW-2000))
    service=ArchiveQueryService(store)
    r=await service.query(conversation='qq:group:1',requester='r',question='雪地的角色',person='雪地',keywords=['角色'],now=NOW)
    message=next(m for m in r['messages'] if m['source_uid']=='command')
    assert message['context_only']
    assert 'person_context_candidate' in message['hit_reasons']

@pytest.mark.asyncio
async def test_broad_character_and_identity_do_not_outrank_specific_topic(store):
    await store.archive_record(record('answer','角色名字是林舟',stamp=NOW-2000))
    for i in range(30):
        await store.archive_record(record('noise'+str(i),'雪地卡住了，隔一会卡一下',stamp=NOW-100+i))
    r=await ArchiveQueryService(store).query(conversation='qq:group:1',requester='r',question='雪地角色',person='雪地',keywords=['雪地','角色','卡'],now=NOW,page_size=1)
    assert r['messages'][0]['source_uid']=='answer'

@pytest.mark.asyncio
async def test_first_page_includes_different_discussions_before_dense_neighbors(store):
    for n in range(6):
        clock=NOW-10000+n*1200
        await store.archive_record(record('q'+str(n),'跑团角色'+('资料' if n==0 else ''),stamp=clock))
        await store.archive_record(record('a'+str(n),'角色名'+str(n),'u2','四格蝶',stamp=clock+1,reply='q'+str(n)))
        for i in range(12):
            await store.archive_record(record('chat'+str(n)+'_'+str(i),'路过聊天','u3','路人',stamp=clock+2+i))
    r=await ArchiveQueryService(store).query(conversation='qq:group:1',requester='r',question='跑团角色',keywords=['跑团','角色','资料'],now=NOW,page_size=12)
    uids={m['source_uid'] for m in r['messages']}
    assert {'q0','q1','q2','q3','q4','q5'}<=uids
    assert len(uids & {'a0','a1','a2','a3','a4','a5'})>=4

@pytest.mark.asyncio
async def test_correction_can_query_other_name_without_old_name_restriction(store):
    await store.archive_record(record('old','角色旧名：松枝',stamp=NOW-2000))
    await store.archive_record(record('question','后来新角色面板',stamp=NOW-1000))
    await store.archive_record(record('new','改为林舟，职业是学者','u2','四格蝶',stamp=NOW-999,reply='question'))
    service=ArchiveQueryService(store)
    await service.query(conversation='qq:group:1',requester='r',question='松枝的角色',keywords=['松枝'],now=NOW)
    corrected=await service.query(conversation='qq:group:1',requester='r',question='雪地后来新建的角色',person='雪地',keywords=['新角色','面板'],now=NOW)
    assert '林舟' in '\n'.join(m['text'] for m in corrected['messages'])


@pytest.mark.asyncio
async def test_tool_output_is_explicit_idempotent_and_revoked_by_clear(store):
    from unittest.mock import AsyncMock
    from astrbot_plugin_livingmemory.core.archive_runtime import ArchiveRuntime

    runtime = ArchiveRuntime.__new__(ArchiveRuntime)
    runtime.store, runtime.ready, runtime.accepting, runtime.gaps = store, True, True, 0
    runtime.may_capture = AsyncMock(return_value=True)
    extras = {}
    event = SimpleNamespace(unified_msg_origin="qq:group:1", get_platform_name=lambda: "qq",
        get_self_id=lambda: "bot", get_sender_id=lambda: "u1", get_sender_name=lambda: "Reader",
        get_extra=lambda key, default=None: extras.get(key, default),
        set_extra=lambda key, value: extras.update({key: value}))
    snapshot = await runtime.prepare_tool_output(event, tool_name="comic_catalog",
        items=[{"item_id": "book1", "title": "Book", "body": "Synthetic catalog result"}],
        delivery_mode="single")
    assert (await store.read_connection.execute_fetchall("SELECT COUNT(*) FROM archive_messages"))[0][0] == 0
    first = await runtime.record_tool_output(event, snapshot)
    assert first["status"] == "stored"
    assert await runtime.record_tool_output(event, snapshot) == first
    rows = await store.read_connection.execute_fetchall("SELECT role,text FROM archive_messages")
    assert [tuple(row) for row in rows] == [("assistant", "Synthetic catalog result")]
    event.unified_msg_origin = "qq:group:2"
    assert (await runtime.record_tool_output(event, snapshot))["status"] == "unavailable"
    event.unified_msg_origin = "qq:group:1"
    await store.clear_archive(event.unified_msg_origin)
    assert (await runtime.record_tool_output(event, snapshot))["status"] == "not_stored"
    assert (await store.read_connection.execute_fetchall("SELECT COUNT(*) FROM archive_messages"))[0][0] == 0
