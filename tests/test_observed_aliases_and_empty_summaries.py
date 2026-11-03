"""Account evidence isolation and exclusion before candidate limits."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from astrbot_plugin_livingmemory.core.models.graph_models import GraphNode
from astrbot_plugin_livingmemory.core.processors.graph_extractor import GraphExtractor
from astrbot_plugin_livingmemory.core.retrieval.bm25_retriever import BM25Retriever
from astrbot_plugin_livingmemory.core.retrieval.vector_search import retrieve_vectors
from astrbot_plugin_livingmemory.core.utils.formatting import format_memories_for_injection
from astrbot_plugin_livingmemory.core.utils.recall_policy import is_non_substantive, name_in_query
from astrbot_plugin_livingmemory.storage.graph_store import GraphStore


def empty_metadata(**kwargs):
    return {'summary_quality':'low','topics':[],'key_facts':[],'participants':[],**kwargs}


@pytest.mark.parametrize('text', [
    '本次未提供具体的群聊记录内容。', '本次无群聊消息输入。', '无对话记录。',
    '未提供群聊对话内容，无互动记录。', '本次未提供具体的群聊对话记录，无实际内容可总结。',
])
def test_empty_summary_requires_structural_confirmation(text):
    assert is_non_substantive(text, empty_metadata())
    assert not is_non_substantive(text, {})
    assert not is_non_substantive(text, empty_metadata(key_facts=['林舟改名为夜航']))
    assert not is_non_substantive(text, empty_metadata(recall_excluded=False))


@pytest.mark.parametrize('text', ['林舟今天回家了。', '林舟说“无对话记录”是软件报错。', '林舟：无对话记录。'])
def test_low_quality_and_quoted_boilerplate_are_not_excluded(text):
    assert not is_non_substantive(text, empty_metadata())


@pytest.mark.parametrize('alias,query,match', [('Ann','Joanna是谁',False),('Ann','Ann是谁',True),('123','91234',False),('旧昵称','旧昵称是谁',True)])
def test_alias_matches_complete_name(alias,query,match):
    assert name_in_query(alias,query) is match


def identity_metadata(sender, name, session='g1',persona='p1'):
    return {
        'identity_key':f'qq:{sender}','sender_id':str(sender),'platform':'qq','aliases':[name],
        'alias_observations':[{'session_id':session,'persona_id':persona,'aliases':[name]}],
    }


async def add_identity_scope(store,node_id,session='g1',persona='p1',source=1):
    async with store._connect() as db:
        cur=await db.execute("INSERT INTO graph_entries(entry_key,source_memory_id,session_id,persona_id,entry_type,content,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",(f'{source}-{session}-{persona}',source,session,persona,'participant','Participant','{}','now','now'))
        await db.execute('INSERT INTO graph_entry_nodes(entry_id,node_id) VALUES(?,?)',(cur.lastrowid,node_id))
        await db.commit()


@pytest.mark.asyncio
async def test_concurrent_rename_accumulates_and_does_not_merge_accounts(tmp_path):
    store=GraphStore(str(tmp_path/'graph.db'))
    await store.initialize()
    await asyncio.gather(*[
        store.upsert_node(GraphNode('person',name,'account:qq:1',identity_metadata(1,name)))
        for name in ['旧昵称','新昵称']
    ])
    other=await store.upsert_node(GraphNode('person','新昵称','account:qq:2',identity_metadata(2,'新昵称')))
    async with store._connect() as db:
        rows=await (await db.execute('SELECT id,metadata FROM graph_nodes ORDER BY id')).fetchall()
    first=next(row for row in rows if row[0]!=other)
    assert set(json.loads(first[1])['aliases'])=={'旧昵称','新昵称'}
    assert json.loads(next(row[1] for row in rows if row[0]==other))['aliases']==['新昵称']
    await add_identity_scope(store,first[0],source=1)
    await add_identity_scope(store,other,source=2)
    assert len(await store.resolve_account_aliases('旧昵称是谁','g1','p1'))==1
    shared=await store.resolve_account_aliases('新昵称是谁','g1','p1')
    assert len(shared)==2 and all(item['ambiguous'] for item in shared)


@pytest.mark.asyncio
async def test_alias_scope_cannot_leak_group_or_persona(tmp_path):
    store=GraphStore(str(tmp_path/'scope.db'))
    await store.initialize()
    node=await store.upsert_node(GraphNode('person','公开昵称','account:qq:1',identity_metadata(1,'公开昵称')))
    await store.upsert_node(GraphNode('person','别群昵称','account:qq:1',identity_metadata(1,'别群昵称','g2','p2')))
    await add_identity_scope(store,node)
    await add_identity_scope(store,node,'g2','p2',2)
    assert not await store.resolve_account_aliases('别群昵称是谁','g1','p1')
    assert not await store.resolve_account_aliases('公开昵称是谁','g1','p2')
    resolved=await store.resolve_account_aliases('公开昵称是谁','g1','p1')
    assert resolved[0]['aliases']==['公开昵称']


@pytest.mark.asyncio
async def test_alias_history_survives_graph_rebuild(tmp_path):
    live=GraphStore(str(tmp_path/'live.db'))
    shadow=GraphStore(str(tmp_path/'shadow.db'))
    await live.initialize()
    await shadow.initialize()
    old=await live.upsert_node(GraphNode('person','旧昵称','account:qq:1',identity_metadata(1,'旧昵称')))
    new=await shadow.upsert_node(GraphNode('person','新昵称','account:qq:1',identity_metadata(1,'新昵称')))
    await add_identity_scope(live,old)
    await add_identity_scope(shadow,new)
    await live.replace_all_from(shadow.db_path)
    resolved=await live.resolve_account_aliases('旧昵称是谁','g1','p1')
    assert resolved and set(resolved[0]['aliases'])=={'旧昵称','新昵称'}


def test_extractor_attaches_source_scopes_and_exclusion():
    meta={'session_id':'g1','persona_id':'p1','recall_excluded':True,'participant_identities':[{'identity_key':'qq:1','sender_id':'1','platform':'qq','display_name':'林舟','aliases':['林舟']}]}
    graph=GraphExtractor().extract(1,'无群聊内容输入。',meta)
    person=next(n for n in graph.nodes if n.node_type=='person')
    assert person.metadata['alias_observations']==[{'session_id':'g1','persona_id':'p1','aliases':['林舟']}]
    assert graph.entries and all(e.metadata['recall_excluded'] for e in graph.entries)


@pytest.mark.asyncio
async def test_empty_vectors_refill_before_limit_for_document_and_graph():
    class FakeDB:
        calls=[]
        async def retrieve(self,**kwargs):
            self.calls.append(kwargs['fetch_k'])
            docs=[{'id':i,'text':'无群聊内容输入。','metadata':{'source_memory_id':i,'recall_excluded':True}} for i in range(4)]
            docs.append({'id':4,'text':'林舟今天回家','metadata':{'source_memory_id':4}})
            return [SimpleNamespace(data=x,similarity=.9) for x in docs[:kwargs['fetch_k']]]
    for source in ['id','source_memory_id']:
        db=FakeDB()
        db.calls=[]
        rows=await retrieve_vectors(db,'回家',1,{},source_key=source)
        assert [row.data['id'] for row in rows]==[4]
        assert db.calls==[2,4,8]


@pytest.mark.asyncio
async def test_bm25_exclusion_before_limit_keeps_useful_record(tmp_path):
    path=str(tmp_path/'bm25.db')
    async with aiosqlite.connect(path) as db:
        await db.execute('CREATE TABLE documents(id INTEGER PRIMARY KEY,text TEXT,metadata TEXT)')
        await db.executemany('INSERT INTO documents VALUES(?,?,?)',[(1,'回家电脑',json.dumps({'recall_excluded':True})),(2,'回家电脑 坏了',json.dumps({}))])
        await db.commit()
    text_processor=SimpleNamespace(tokenize_async=AsyncMock(return_value=['回家电脑']))
    retriever=BM25Retriever(path,text_processor)
    await retriever.initialize()
    await retriever.add_document(1, '回家电脑')
    await retriever.add_document(2, '回家电脑 坏了')
    results=await retriever.search('回家电脑',limit=1)
    assert [item.doc_id for item in results]==[2]


@pytest.mark.asyncio
async def test_graph_exclusion_applies_before_keyword_and_node_limit(tmp_path):
    store=GraphStore(str(tmp_path/'entries.db'))
    await store.initialize()
    node=await store.upsert_node(GraphNode('topic','回家电脑','回家电脑'))
    await add_identity_scope(store,node,source=1)
    await add_identity_scope(store,node,source=2)
    async with store._connect() as db:
        await db.execute("UPDATE graph_entries SET content='回家电脑',metadata=? WHERE source_memory_id=1",(json.dumps({'recall_excluded':True}),))
        await db.execute("UPDATE graph_entries SET content='回家电脑 坏了' WHERE source_memory_id=2")
        await db.execute('INSERT INTO livingmemory_graph_entries_fts(entry_id,content) SELECT id,content FROM graph_entries')
        await db.commit()
    assert [x['source_memory_id'] for x in await store.search_entries_by_bm25('回家电脑',1,'g1','p1')]==[2]
    assert [x['source_memory_id'] for x in await store.get_entries_for_node_ids([node],1,'g1','p1')]==[2]


def test_alias_evidence_survives_live_atom_formatting():
    aliases=[{'identity_key':'qq:1','sender_id':'1','aliases':['旧昵称','新昵称'],'ambiguous':False}]
    metadata={'atom_policy_applied':True,'persona_summary':'过期的旧内容','resolved_account_aliases':aliases}
    formatted=format_memories_for_injection([{'content':'新昵称的电脑不在家','metadata':metadata}])
    assert '旧昵称' in formatted and '新昵称' in formatted
    assert '过期的旧内容' not in formatted
    assert metadata['resolved_account_aliases']==aliases


@pytest.mark.asyncio
async def test_memory_search_tool_returns_identity_bridge(monkeypatch):
    from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
    from astrbot_plugin_livingmemory.core.tools import memory_search_tool

    aliases=[{'identity_key':'qq:1','sender_id':'1','aliases':['旧昵称','新昵称'],'ambiguous':False}]
    memory=SimpleNamespace(doc_id=1,content='新昵称今天回家',final_score=.8,metadata={'resolved_account_aliases':aliases})
    engine=SimpleNamespace(search_memories=AsyncMock(return_value=[memory]))
    config=ConfigManager({})
    monkeypatch.setattr(memory_search_tool,'get_persona_id',AsyncMock(return_value='p1'))
    event=SimpleNamespace(unified_msg_origin='g1')
    tool=memory_search_tool.MemorySearchTool(context=SimpleNamespace(),config_manager=config,memory_engine=engine)
    result=json.loads(await tool.call(SimpleNamespace(context=SimpleNamespace(event=event)),'旧昵称'))
    assert result['identity_aliases']==aliases
    assert result['results'][0]['content']=='新昵称今天回家'


@pytest.mark.asyncio
async def test_aliases_intersect_adapter_policy_even_for_shared_account(tmp_path):
    from astrbot_plugin_livingmemory.core.memory_scope import adapter_access

    store = GraphStore(str(tmp_path / "adapter-aliases.db"))
    await store.initialize()
    for owner, name, source in [("qq-a", "公开昵称", 1), ("qq-b", "私有昵称", 2)]:
        metadata = identity_metadata(1, name, "livingmemory:global")
        metadata["alias_observations"][0]["adapter_id"] = owner
        node = await store.upsert_node(GraphNode("person", name, "account:qq:1", metadata))
        await add_identity_scope(store, node, "livingmemory:global", source=source)
        async with store._connect() as db:
            await db.execute("UPDATE graph_entries SET metadata=? WHERE source_memory_id=?",
                             (json.dumps({"adapter_id": owner}), source))
            await db.commit()
    config = {"filtering_settings": {"shared_adapter_ids": []}}
    with adapter_access(config, "qq-a"):
        assert not await store.resolve_account_aliases("私有昵称", "livingmemory:global", "p1")
        resolved = await store.resolve_account_aliases("公开昵称", "livingmemory:global", "p1")
        assert resolved[0]["aliases"] == ["公开昵称"]
    with adapter_access({"filtering_settings": {"shared_adapter_ids": ["qq-a", "qq-b"]}}, "qq-a"):
        resolved = await store.resolve_account_aliases("私有昵称", "livingmemory:global", "p1")
        assert set(resolved[0]["aliases"]) == {"公开昵称", "私有昵称"}
