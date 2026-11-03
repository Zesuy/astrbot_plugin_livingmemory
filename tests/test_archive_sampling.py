"""Sampling contracts on real SQLite, with deliberately misleading evidence."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from .test_chat_events_tool import store, record, NOW
from astrbot_plugin_livingmemory.core.managers.archive_query_service import ArchiveQueryService
from astrbot_plugin_livingmemory.core.tools.chat_events_tool import ChatEventsTool

TZ = ZoneInfo('Asia/Shanghai')
SCOPE = 'qq:group:1'


def native(uid, text, stamp, person='u1', quote=''):
    value = record(uid, ('引用：'+quote+'\n' if quote else '')+text,
                   person=person, stamp=stamp)
    value.components = ([{'type':'reply','text':quote}] if quote else []) + [{'type':'plain','text':text}]
    return value


async def ask(service, **kwargs):
    return await service.query(conversation=SCOPE, requester='r', question='查看原文', now=NOW, **kwargs)


@pytest.mark.asyncio
async def test_overview_first_page_survives_dense_burst_and_long_quote(store):
    low = NOW-8*3600
    for bucket in range(8):
        for n in range(80 if bucket==0 else 7):
            await store.archive_record(native(f'{bucket}-{n}',f'本时段主题{bucket}的连续讨论{n}',
                low+bucket*3600+100+n, quote='长引用'*4000 if bucket==0 else ''))
    await store.archive_record(record('private','其他群秘密',scope='qq:group:2'))
    service=ArchiveQueryService(store, result_chars=4000)
    result=await ask(service,mode='overview',start=datetime.fromtimestamp(low,TZ).isoformat(),
                     end=datetime.fromtimestamp(NOW,TZ).isoformat(),keywords=['根本不存在'])
    assert result['sampling']['delivered_strata']==list(range(8))
    assert len(result['messages'])<=40
    assert 'private' not in result['sampling']['delivered_source_uids']
    assert sum(len(m['text'])+len(m['author_plain'] or '')+sum(map(len,m['quoted_context'])) for m in result['messages'])<=4000
    assert result['resolved_query']['keywords_used'] is False
    for window in result['sampling']['windows']:
        assert len(window['source_uids'])<=5


@pytest.mark.asyncio
async def test_style_keeps_short_author_plain_separates_quotes_and_expands_original(store):
    await store.archive_record(native('short','？',NOW-7000,quote='他人的整段文风，不是本人'))
    await store.archive_record(native('other','旁人说法',NOW-6000,person='u2'))
    await store.archive_record(native('command','/help',NOW-5000))
    await store.archive_record(record('legacy','无法分清引用与正文',stamp=NOW-4000))
    service=ArchiveQueryService(store)
    result=await ask(service,mode='style_sample',person='u1')
    assert [m['source_uid'] for m in result['messages']]==['short']
    message=result['messages'][0]
    assert message['text']==message['author_plain']=='？'
    assert message['quoted_context']==['他人的整段文风，不是本人']
    assert message['next_text_handle'] is None
    original=await ask(service,mode='read',evidence_handle=message['original_evidence_handle'])
    assert original['messages'][0]['text']=='引用：他人的整段文风，不是本人\n？'
    assert result['sampling']['excluded_candidates']['no_native_author_boundary']==1


@pytest.mark.asyncio
async def test_style_resolves_renamed_author_deduplicates_and_limits_burst(store):
    for n in range(20):
        await store.archive_record(native(f'burst{n}',f'连发{n}',NOW-200+n))
    await store.archive_record(native('repeat1','一样的短句',NOW-4000))
    await store.archive_record(native('repeat2','一样的短句',NOW-8000))
    old=native('old','早期不同话题',NOW-86400*5)
    old.nickname='旧昵称'
    await store.archive_record(old)
    result=await ask(ArchiveQueryService(store),mode='style_sample',person='旧昵称')
    assert {m['sender_id'] for m in result['messages']}=={'u1'}
    assert len([m for m in result['messages'] if m['source_uid'].startswith('burst')])<=2
    assert len([m for m in result['messages'] if m['text']=='一样的短句'])==1
    assert 'old' in [m['source_uid'] for m in result['messages']]


@pytest.mark.asyncio
async def test_activity_excludes_trigger_and_does_not_guess_absence(store):
    await store.archive_record(native('previous','在',NOW-7200))
    await store.archive_record(native('other','期间聊天',NOW-3600,person='u2'))
    await store.archive_record(native('trigger','我没说话时聊什么',NOW-1))
    result=await ask(ArchiveQueryService(store),mode='overview',person='u1',time_range='今天',exclude_uid='trigger')
    activity=result['sampling']['person_activity']
    assert activity['observed_posts']==1
    assert 'trigger' not in result['sampling']['delivered_source_uids']
    assert 'not offline' in activity['note']
    assert any(gap['other_messages']==1 for gap in activity['no_post_intervals'])
    assert any(gap['other_messages']==0 for gap in activity['no_post_intervals'])


@pytest.mark.asyncio
async def test_midnight_today_does_not_silently_become_yesterday(store):
    now=datetime(2026,10,22,0,14,34,tzinfo=TZ).timestamp()
    await store.archive_record(native('yesterday','昨日讨论',now-1800))
    await store.archive_record(native('today','今日讨论',now-300))
    result=await ArchiveQueryService(store).query(conversation=SCOPE,requester='r',
        question='今天我没有发言的时候群里面都聊了什么',mode='overview',time_range='今天',now=now)
    assert result['resolved_query']['time_start']=='2026-10-22T00:00:00+08:00'
    assert [m['source_uid'] for m in result['messages']]==['today']


@pytest.mark.asyncio
async def test_sampling_refuses_ambiguous_identity_and_future_window(store):
    await store.archive_record(native('a','甲',NOW-100))
    second=native('b','乙',NOW-200,person='u2')
    await store.archive_record(second)
    service=ArchiveQueryService(store)
    for mode in ('overview','style_sample'):
        result=await ask(service,mode=mode,person='雪地',time_range='今天')
        assert result['status']=='ambiguous'
        result=await ask(service,mode=mode,person='未出现过的人',time_range='今天')
        assert result['status']=='ambiguous'
    future=await ask(service,mode='overview',start='2027-01-01T00:00:00+08:00',end='2027-01-02T00:00:00+08:00')
    assert future['status']=='error'


@pytest.mark.asyncio
async def test_weekly_window_and_clearing_invalidates_sampling(store):
    await store.archive_record(native('a','这周的聊天',NOW-86400*5))
    service=ArchiveQueryService(store)
    result=await ask(service,mode='overview',time_range='最近一周')
    assert result['resolved_query']['time_start']=='2026-09-29T00:00:00+08:00'
    assert len(result['messages'])==1
    await store.clear_archive(SCOPE)
    invalid=await ask(service,mode='read',evidence_handle=result['messages'][0]['original_evidence_handle'])
    assert invalid['status']=='invalidated'


@pytest.mark.asyncio
async def test_sparse_strata_spend_remaining_budget_on_dense_discussions(store):
    low=NOW-8*3600
    for bucket in range(8):
        for n in range(100 if bucket==7 else 1):
            await store.archive_record(native(f'{bucket}-{n}',f'本时段不同消息{n}',low+bucket*3600+10+n))
    result=await ask(ArchiveQueryService(store),mode='overview',
        start=datetime.fromtimestamp(low,TZ).isoformat(),end=datetime.fromtimestamp(NOW,TZ).isoformat())
    assert result['sampling']['delivered_strata']==list(range(8))
    assert len(result['messages'])>=35


@pytest.mark.asyncio
async def test_style_filters_before_sampling_and_uses_native_boundary_without_platform_time(store):
    # Every other original row is an image: sampling first at stride 2 must
    # not accidentally discard every Plain. They span many ten-minute bursts.
    for n in range(100):
        value=native(str(n),f'本人第{n}条' if n%2 else '',NOW-100000+n*601)
        if not n%2:
            value.components=[{'type':'image'}]
        if n==99:
            value.time_quality='unknown'
            value.sent_at=None
            value.saved_at=NOW-100
        await store.archive_record(value)
    result=await ask(ArchiveQueryService(store,max_candidates=64),mode='style_sample',person='u1')
    assert len(result['messages'])>=8
    assert all('本人第' in m['text'] for m in result['messages'])
    assert result['sampling']['excluded_candidates']['no_plain_text']==50
    assert '99' in [m['source_uid'] for m in result['messages']]


@pytest.mark.asyncio
async def test_overview_collapses_only_exact_legacy_copies_without_changing_archive(store):
    for uid in ('working-copy','retained-copy'):
        value=record(uid,'同一条旧回复',stamp=NOW-100)
        value.native_key=None
        value.time_quality='legacy_saved'
        await store.archive_record(value)
    for uid in ('native-one','native-two'):
        await store.archive_record(native(uid,'相同秒内的真实重复发言',NOW-50))
    service=ArchiveQueryService(store)
    result=await ask(service,mode='overview',time_range='今天')
    assert len(result['messages'])==3
    assert result['sampling']['collapsed_legacy_copies']==1
    assert result['sampling']['raw_retained_records']==4
    assert result['sampling']['person_activity'] is None
    full=await ask(service,mode='time_read',time_range='今天')
    assert len(full['messages'])==4


@pytest.mark.asyncio
@pytest.mark.parametrize('person,expected',[('我','requester'),('你','bot')])
async def test_tool_binds_pronouns_and_excludes_cached_current_message(person,expected):
    query=AsyncMock(return_value={'status':'no_support_found'})
    runtime=SimpleNamespace(ready=True,enabled=True,config={},gaps=0,
        service=SimpleNamespace(query=query),authorized=AsyncMock(return_value=True))
    event=SimpleNamespace(unified_msg_origin=SCOPE,get_sender_id=lambda:'requester',get_self_id=lambda:'bot',
        get_extra=lambda key,default=None:SimpleNamespace(uid='current') if key=='_lm_archive_record' else default)
    await ChatEventsTool(runtime=runtime).call(SimpleNamespace(context=SimpleNamespace(event=event)),
        query='这段时间聊了什么',mode='overview',person=person,time_range='今天')
    assert query.await_args.kwargs['person']==expected
    assert query.await_args.kwargs['exclude_uid']=='current'


@pytest.mark.asyncio
async def test_no_post_interval_includes_message_at_window_start(store):
    midnight=datetime.fromtimestamp(NOW,TZ).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
    await store.archive_record(native('boundary','午夜消息',midnight,person='u2'))
    # Resolve u1 through an earlier observed name, outside this time window.
    await store.archive_record(native('previous','早先发言',midnight-100))
    result=await ask(ArchiveQueryService(store),mode='overview',person='u1',time_range='今天')
    gap=result['sampling']['person_activity']['no_post_intervals'][0]
    assert gap['start_inclusive']
    assert gap['other_messages']==1


@pytest.mark.asyncio
async def test_style_does_not_take_only_oldest_prefix_inside_busy_bucket(store):
    # All messages fit within one of the thirty-day window's eight strata.
    for n in range(50):
        await store.archive_record(native(str(n),f'第{n}次独立聊天',NOW-86400+n*1200))
    result=await ask(ArchiveQueryService(store),mode='style_sample',person='u1',page_size=8)
    chosen={m['source_uid'] for m in result['messages']}
    assert {'0','49'}<=chosen
    assert any(15<=int(uid)<=35 for uid in chosen)
