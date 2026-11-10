"""Read federation contracts on real SQLite, profiles and retrieval components."""
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
from astrbot_plugin_livingmemory.core.base.config_validator import FilteringConfig
from astrbot_plugin_livingmemory.core.memory_scope import (
    resolve_memory_scope, resolve_memory_read_scope, resolve_memory_read_scopes, adapter_access,
)
from astrbot_plugin_livingmemory.core.user_profile import empty_profile, merge_profiles, normalize_value
from astrbot_plugin_livingmemory.core.user_profile_runtime import UserProfileRuntime
from astrbot_plugin_livingmemory.core.dialogue_buffer import DialogueBuffer
from astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall import MemoryRecall
from astrbot_plugin_livingmemory.core.utils.injection_adapter import InjectionAdapter
from astrbot_plugin_livingmemory.core.managers.archive_query_service import ArchiveQueryService
from astrbot_plugin_livingmemory.core.models.archive_models import ArchiveMessage
from astrbot_plugin_livingmemory.core.models.memory_atom import MemoryAtom
from astrbot_plugin_livingmemory.storage.conversation_store import ConversationStore
from tests.integration.test_adapter_memory_sharing import adapter_engine
from tests.test_user_profile import event as profile_event, source, commit, op

ADAPTER = "鼠（napcat）"
NEW = ADAPTER + ":GroupMessage:530216971"
OLD = ADAPTER + ":GroupMessage:909660891"
OTHER = ADAPTER + ":GroupMessage:123"
SCOPES = tuple(sorted((NEW, OLD)))


def config(value="530216971, 909660891", **settings):
    return ConfigManager({"filtering_settings": {"shared_group_scopes": value, **settings},
                          "user_profiles": {"enabled": True}})


def event(scope=NEW):
    return profile_event(scope)


def fact(val, at):
    return {"val": val, "at": at, "source_uid": val}


@pytest.mark.parametrize("setting", ["530216971,909660891", "909660891 530216971 530216971", "530216971，909660891"])
def test_configuration_and_same_adapter_normalization(setting):
    cfg = config(setting)
    assert cfg.get("filtering_settings.shared_group_scopes") == setting
    assert FilteringConfig(shared_group_scopes=setting).model_dump()["shared_group_scopes"] == setting
    for scope in SCOPES:
        assert resolve_memory_read_scopes(cfg, event(scope)) == SCOPES
        assert resolve_memory_scope(cfg, event(scope)) == scope
    other_adapter = event("other:GroupMessage:530216971")
    assert resolve_memory_read_scopes(cfg, other_adapter) == ("other:GroupMessage:530216971", "other:GroupMessage:909660891")


@pytest.mark.parametrize("value,isolated", [
    ("530216971 909660891\n909660891 123", ""),
    ("530216971 909660891", OLD),
    ("530216971 909660891", NEW),
    ("530216971 malformed", ""),
    ("", ""),
])
def test_overlap_invalid_and_forced_isolation_fail_closed(value, isolated):
    cfg = config(value, isolated_sessions=isolated)
    assert resolve_memory_read_scopes(cfg, event()) == (NEW,)
    assert resolve_memory_read_scopes(cfg, event(OLD)) == (OLD,)
    private = event(ADAPTER + ":FriendMessage:530216971")
    assert resolve_memory_read_scopes(cfg, private) == (private.unified_msg_origin,)


def test_legacy_read_modes_unchanged_when_no_federation():
    cfg = config("", memory_scope_mode="global")
    assert resolve_memory_read_scope(cfg, event()) == resolve_memory_scope(cfg, event())


def test_profile_merge_source_time_tombstones_and_budget():
    old, new = empty_profile(), empty_profile()
    old["single_slots"]["current_main_game"] = fact("old game", 200)
    new["single_slots"]["current_main_game"] = fact("new game", 100)
    old["identity"]["preferred_name"] = fact("旧称", 50)
    new["identity"]["preferred_name"] = fact("新称", 150)
    old["identity"]["real_name"] = fact("真实姓名", 250)
    old["set_slots"]["hobbies"] = [fact("Linux", 100), fact("Chess", 300)]
    new["removals"] = {normalize_value("Linux"): 100, normalize_value("Chess"): 200, "alias:旧外号": 300}
    old["identity"]["aliases"] = [fact("旧外号", 200)]
    merged = merge_profiles([old, new])
    assert merged["single_slots"]["current_main_game"]["val"] == "old game"
    assert merged["identity"]["preferred_name"]["val"] == "新称"
    assert merged["identity"]["real_name"]["val"] == "真实姓名"
    assert [v["val"] for v in merged["set_slots"]["hobbies"]] == ["Chess"]
    assert merged["identity"]["aliases"] == []
    assert old["identity"]["aliases"]  # read merge never mutates persisted snapshots
    new["set_slots"]["hobbies"] = [fact(str(i)*20, 400+i) for i in range(20)]
    bounded = merge_profiles([old, new])
    facts = [*bounded["single_slots"].values(), *[v for values in bounded["set_slots"].values() for v in values],
             *[v for k,v in bounded["identity"].items() if k != "aliases" and v], *bounded["identity"]["aliases"]]
    assert len(facts) <= 12 and sum(len(v["val"]) for v in facts) <= 200


@pytest_asyncio.fixture
async def store(tmp_path):
    value = ConversationStore(str(tmp_path / "conversations.db"))
    await value.initialize()
    yield value
    await value.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("clear_scope", [NEW, OLD])
async def test_profile_inheritance_epoch_vector_and_unlink(store, clear_scope):
    await source(store, scope=OLD)
    assert await commit(store, [op()], scope=OLD)
    cfg = config()
    runtime = UserProfileRuntime(SimpleNamespace(store=store, config=cfg, enabled=True,
        authorized=AsyncMock(return_value=True), may_capture=AsyncMock(return_value=True)))
    ev = event()
    inherited = await runtime.profile_for_event(ev)
    assert inherited["set_slots"]["hobbies"][0]["val"] == "Linux"
    assert set(ev.get_extra("_lm_speaker_profile_epochs")) == set(SCOPES)
    assert not (await store.get_user_profile(NEW, "100"))["profile"]["set_slots"]
    ev.set_extra("_lm_speaker_profile", inherited)
    recall = MemoryRecall(None, cfg, None, SimpleNamespace(store=store), None, InjectionAdapter())
    req = SimpleNamespace(extra_user_content_parts=[])
    recall._inject_clean_context(ev, req, [], "")
    assert "Linux" not in req.extra_user_content_parts[0].text
    await store.clear_archive(clear_scope)
    req.extra_user_content_parts.clear()
    recall._inject_clean_context(ev, req, [], "")
    assert "[当前会话]" in req.extra_user_content_parts[0].text
    unlinked = UserProfileRuntime(SimpleNamespace(store=store, config=config(""), enabled=True,
        authorized=AsyncMock(return_value=True)))
    assert not (await unlinked.profile_for_event(event()))["set_slots"]


def test_dialogue_buffer_stays_physical_after_linking():
    buffer = DialogueBuffer()
    buffer.add_message(OLD, "old", "100", "old-only")
    buffer.add_message(NEW, "new", "100", "new-only")
    assert resolve_memory_read_scopes(config(), event()) == SCOPES
    assert "old-only" not in buffer.format_for_context(NEW)
    assert "new-only" not in buffer.format_for_context(OLD)


@pytest.mark.asyncio
async def test_long_term_union_routes_physical_writes_cache_and_unlink(adapter_engine):
    engine = adapter_engine
    cfg = config()
    ids = {}
    for scope in (*SCOPES, OTHER, "other:GroupMessage:909660891"):
        with adapter_access(cfg, ADAPTER, write=True):
            ids[scope] = await engine.add_memory("coffee preference " + scope,
                session_id=scope, persona_id="p", metadata={"topics": ["coffee"], "key_facts": ["coffee preference"]},
                atoms=[MemoryAtom(parent_memory_id=0, content="coffee preference", expires_at=time.time()+86400)])
    expected = {ids[scope] for scope in SCOPES}
    with adapter_access(cfg, ADAPTER):
        for scope in SCOPES:
            scopes = resolve_memory_read_scope(cfg, event(scope))
            hits = await engine.search_memories("coffee", 10, scopes, "p")
            assert {h.doc_id for h in hits} == expected
            assert {h.metadata["session_id"] for h in hits} == set(SCOPES)
        assert engine._search_cache_key("coffee", 10, SCOPES, "p") == engine._search_cache_key("coffee", 10, list(reversed(SCOPES)), "p")
        assert {h.doc_id for h in await engine.bm25_retriever.search("coffee", 10, SCOPES, "p")} == expected
        assert {h.doc_id for h in await engine.vector_retriever.search("coffee", 10, SCOPES, "p")} == expected
        assert {h.parent_memory_id for h in await engine.atom_retriever.search("coffee", 10, SCOPES, "p")} == expected
        assert {h.doc_id for h in await engine.graph_vector_retriever.search("coffee", 10, SCOPES, "p")} == expected
        assert {h.doc_id for h in await engine._get_recent_memory_results(10, SCOPES, "p")} == expected
        assert {h.doc_id for h in await engine.search_memories("coffee", 10, NEW, "p")} == {ids[NEW]}
        assert {h.doc_id for h in await engine.search_memories("coffee", 10, (), "p")} == set()
        outcome = await engine.search_memories_agentic(query="coffee", k=10, session_id=SCOPES, persona_id="p")
        assert {h.doc_id for h in outcome.results} == expected
        duplicate = await engine.find_similar_memory("coffee preference", session_id=NEW, persona_id="p", threshold=0)
        assert duplicate["id"] == ids[NEW]


@pytest.mark.asyncio
async def test_archive_union_neighbors_snapshots_and_unlink(store):
    now = time.time()
    await store.clear_archive(NEW)  # different epochs per physical group
    for uid, scope, body in [("anchor", OLD, "coffee evidence"), ("reply", OLD, "answer"),
                             ("unrelated", NEW, "do not include as neighbor")]:
        await store.archive_record(ArchiveMessage(uid=uid, native_key=uid, conversation=scope,
            sender_id="100", nickname="name", platform="qq", text=body, sent_at=now-100,
            epoch=store.accepted_epoch(scope)))
    service = ArchiveQueryService(store)
    result = await service.query(conversation=NEW, read_scopes=SCOPES, requester="100", question="coffee", keywords=["coffee"], now=now)
    assert {m["source_uid"] for m in result["messages"]} == {"anchor", "reply"}
    assert all(m["conversation"] == OLD for m in result["messages"])
    assert set(result["resolved_query"]["scope_watermarks"]) == set(SCOPES)
    handle = result["evidence_handle"]
    unlinked = await service.query(conversation=NEW, requester="100", mode="read", evidence_handle=handle)
    assert unlinked["status"] == "unavailable"
    await store.clear_archive(OLD)
    invalid = await service.query(conversation=NEW, read_scopes=SCOPES, requester="100", mode="read", evidence_handle=handle)
    assert invalid["status"] == "invalidated"


@pytest.mark.asyncio
async def test_profile_newer_fact_beats_more_recent_database_update(store):
    # Old group's original fact is newer, although the new group's row is written last.
    await source(store, uid="old", scope=OLD, text="我主玩Minecraft", at=2000)
    assert await commit(store, [op(uid="old", val="Minecraft", quote="我主玩Minecraft", slot="current_main_game", action="set")], scope=OLD)
    await source(store, uid="new", scope=NEW, text="我主玩Terraria", at=1000)
    assert await commit(store, [op(uid="new", val="Terraria", quote="我主玩Terraria", slot="current_main_game", action="set")], scope=NEW)
    runtime = UserProfileRuntime(SimpleNamespace(store=store, config=config(), enabled=True,
        authorized=AsyncMock(return_value=True)))
    for scope in SCOPES:
        merged = await runtime.profile_for_event(event(scope))
        assert merged["single_slots"]["current_main_game"]["val"] == "Minecraft"
    assert (await store.get_user_profile(NEW, "100"))["profile"]["single_slots"]["current_main_game"]["val"] == "Terraria"


@pytest.mark.asyncio
async def test_legacy_vector_host_never_receives_tuple_filters():
    from astrbot_plugin_livingmemory.core.retrieval.vector_search import retrieve_vectors
    seen = []
    async def retrieve(**kwargs):
        seen.append(kwargs["metadata_filters"])
        return [SimpleNamespace(similarity=0.9, data={"id": i, "text": "coffee", "metadata": {"session_id": scope}})
                for i, scope in enumerate([NEW, OLD, OTHER], 1)]
    db = SimpleNamespace(retrieve=retrieve)
    hits = await retrieve_vectors(db, "coffee", 2, {"session_id": SCOPES})
    assert {hit.data["metadata"]["session_id"] for hit in hits} == set(SCOPES)
    assert seen == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,use_session", [("legacy", True), ("legacy", False), ("session", True), ("global", True), ("user", True)])
async def test_group_forget_never_crosses_physical_origin(mode, use_session):
    from astrbot_plugin_livingmemory.core.tools.memory_forget_tool import MemoryForgetTool
    from unittest.mock import Mock
    cfg = config(memory_scope_mode=mode, use_session_filtering=use_session, use_persona_filtering=False)
    engine = Mock(get_memory=AsyncMock(return_value={"metadata": {"session_id": OLD, "status": "active"}}),
                  archive_memories=AsyncMock(return_value=1))
    ev = event()
    ev.is_admin = lambda: True
    tool = MemoryForgetTool(context=Mock(), config_manager=cfg, memory_engine=engine)
    result = json.loads(await tool.call(SimpleNamespace(context=SimpleNamespace(event=ev)), [1]))
    assert result["forgotten"] is False
    assert result["invalid_ids"] == [1]
    engine.archive_memories.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_alias_removal_masks_old_scope_binding(store):
    from astrbot_plugin_livingmemory.core.user_profile import resolve_member
    from tests.test_user_profile_identity import identity_op
    await source(store, uid="alias-old", scope=OLD, text="叫我 x", at=1000)
    assert await commit(store, [identity_op("add_alias", "x", "叫我 x", "alias-old")], scope=OLD)
    await source(store, uid="alias-remove", scope=NEW, text="别叫我 x", at=2000)
    assert await commit(store, [identity_op("remove_alias", "x", "别叫我 x", "alias-remove")], scope=NEW)
    # A historically observed nickname must not resurrect the removed alias.
    await store.archive_record(ArchiveMessage(uid="old-nickname", conversation=OLD,
        sender_id="100", nickname="x", platform="qq", text="hello", sent_at=500))
    assert await resolve_member(SCOPES, "x", store) is None
    assert await resolve_member(OLD, "x", store) == "100"  # physical rows stay unchanged


@pytest.mark.asyncio
async def test_shared_alias_reassignment_has_one_latest_owner_and_profile(store):
    from astrbot_plugin_livingmemory.core.user_profile import resolve_member
    from tests.test_user_profile_identity import identity_op
    await source(store, uid="alias-old", scope=OLD, text="叫我 x", at=1000)
    assert await commit(store, [identity_op("add_alias", "x", "叫我 x", "alias-old")], scope=OLD)
    await source(store, uid="alias-reassign", scope=NEW, text="不对，叫我 x", user="200", at=2000)
    assert await commit(store, [identity_op("reassign_alias", "x", "不对，叫我 x", "alias-reassign")], scope=NEW, user="200")
    assert await resolve_member(SCOPES, "x", store) == "200"
    runtime = UserProfileRuntime(SimpleNamespace(store=store, config=config(), enabled=True,
        authorized=AsyncMock(return_value=True)))
    assert (await runtime.profile_for_event(event()))["identity"]["aliases"] == []
    other = event()
    other.get_sender_id = lambda: "200"
    assert (await runtime.profile_for_event(other))["identity"]["aliases"][0]["val"] == "x"
    assert await resolve_member(OLD, "x", store) == "100"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["global", "user"])
async def test_synthetic_storage_mode_reads_its_own_writes_with_linked_groups(adapter_engine, mode):
    from astrbot_plugin_livingmemory.core.memory_scope import resolve_group_read_scopes
    cfg = config(memory_scope_mode=mode)
    ev = event()
    write_scope = resolve_memory_scope(cfg, ev)
    assert resolve_memory_read_scopes(cfg, ev) == (write_scope,)
    assert resolve_memory_read_scope(cfg, ev) == write_scope
    assert resolve_group_read_scopes(cfg, ev) == SCOPES  # raw/profile data never moves
    mid = await adapter_engine.add_memory("coffee global preference", session_id=write_scope)
    results = await adapter_engine.search_memories("coffee", 10, resolve_memory_read_scope(cfg, ev))
    assert mid in {row.doc_id for row in results}
    isolated = config(memory_scope_mode=mode, isolated_sessions=NEW)
    assert resolve_memory_read_scopes(isolated, ev) == (NEW,)


@pytest.mark.asyncio
async def test_archive_person_lookup_never_resurrects_retired_alias(store):
    from tests.test_user_profile_identity import identity_op
    await source(store, uid="old-alias", scope=OLD, text="叫我 x", at=1000)
    assert await commit(store, [identity_op("add_alias", "x", "叫我 x", "old-alias")], scope=OLD)
    await source(store, uid="remove-alias", scope=NEW, text="别叫我 x", at=2000)
    assert await commit(store, [identity_op("remove_alias", "x", "别叫我 x", "remove-alias")], scope=NEW)
    await store.archive_record(ArchiveMessage(uid="history-x", conversation=OLD, sender_id="100",
        nickname="x", platform="qq", text="retained historical nickname", sent_at=500))
    service = ArchiveQueryService(store, identity_aliases="100=x")
    service.alias_resolver = AsyncMock(return_value=[{"sender_id": "100", "aliases": ["x"]}])
    result = await service.query(conversation=NEW, read_scopes=SCOPES, requester="100",
        question="x说过什么", person="x", person_role="speaker", mode="time_read",
        start="1970-01-01T00:00:00+00:00", end="2030-01-01T00:00:00+00:00", now=5000)
    assert result["status"] == "ambiguous"
    assert result["resolved_query"]["accounts"] == []
    assert result["resolved_query"]["identity_hint"] == "explicitly_retired_alias"
    service.alias_resolver.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh_with_add", [False, True])
async def test_new_owner_removal_is_global_barrier_against_old_owner(store, refresh_with_add):
    from tests.test_user_profile_identity import identity_op
    from astrbot_plugin_livingmemory.core.user_profile import resolve_member
    await source(store, uid="old-add", scope=OLD, text="叫我 x", at=1000)
    assert await commit(store, [identity_op("add_alias", "x", "叫我 x", "old-add")], scope=OLD)
    await source(store, uid="new-reassign", scope=NEW, user="200", text="不对，叫我 x", at=2000)
    assert await commit(store, [identity_op("reassign_alias", "x", "不对，叫我 x", "new-reassign")], scope=NEW, user="200")
    assert await resolve_member(SCOPES, "x", store) == "200"
    if refresh_with_add:
        await source(store, uid="new-refresh", scope=NEW, user="200", text="叫我 x", at=2500)
        assert await commit(store, [identity_op("add_alias", "x", "叫我 x", "new-refresh")], scope=NEW, user="200")
        assert await resolve_member(SCOPES, "x", store) == "200"
    await source(store, uid="new-remove", scope=NEW, user="200", text="别叫我 x", at=3000)
    assert await commit(store, [identity_op("remove_alias", "x", "别叫我 x", "new-remove")], scope=NEW, user="200")
    assert await resolve_member(SCOPES, "x", store) is None
    runtime = UserProfileRuntime(SimpleNamespace(store=store, config=config(), enabled=True,
        authorized=AsyncMock(return_value=True)))
    assert (await runtime.profile_for_event(event()))["identity"]["aliases"] == []
    assert await resolve_member(OLD, "x", store) == "100"


@pytest.mark.asyncio
async def test_independent_same_alias_additions_remain_ambiguous(store):
    from tests.test_user_profile_identity import identity_op
    from astrbot_plugin_livingmemory.core.user_profile import resolve_member
    for uid, scope, user, at in [("old-add", OLD, "100", 1000), ("new-add", NEW, "200", 2000)]:
        await source(store, uid=uid, scope=scope, user=user, text="叫我 x", at=at)
        assert await commit(store, [identity_op("add_alias", "x", "叫我 x", uid)], scope=scope, user=user)
    assert await resolve_member(SCOPES, "x", store) == ["100", "200"]
    service = ArchiveQueryService(store)
    result = await service.query(conversation=NEW, read_scopes=SCOPES, requester="100",
        question="x说过什么", person="x", mode="time_read", start="1970-01-01T00:00:00+00:00",
        end="2030-01-01T00:00:00+00:00", now=5000)
    assert result["status"] == "ambiguous"
    assert {row["account_id"] for row in result["resolved_query"]["accounts"]} == {"100", "200"}


@pytest.mark.asyncio
async def test_same_group_transfer_tombstone_does_not_delete_new_shared_owner(store):
    from tests.test_user_profile_identity import identity_op
    from astrbot_plugin_livingmemory.core.user_profile import resolve_member
    await source(store, uid="first", scope=OLD, text="叫我 x", at=1000)
    assert await commit(store, [identity_op("add_alias", "x", "叫我 x", "first")], scope=OLD)
    await source(store, uid="transfer", scope=OLD, user="200", text="不对，叫我 x", at=2000)
    assert await commit(store, [identity_op("reassign_alias", "x", "不对，叫我 x", "transfer")], scope=OLD, user="200")
    assert await resolve_member(SCOPES, "x", store) == "200"
    # A genuine remove at the exact binding timestamp still wins.
    await source(store, uid="remove", scope=OLD, user="200", text="别叫我 x", at=2000)
    assert await commit(store, [identity_op("remove_alias", "x", "别叫我 x", "remove")], scope=OLD, user="200")
    assert await resolve_member(SCOPES, "x", store) is None


@pytest.mark.asyncio
async def test_late_remove_does_not_change_newer_transfer_tombstone_kind(store):
    from tests.test_user_profile_identity import identity_op
    from astrbot_plugin_livingmemory.core.user_profile import resolve_member
    await source(store, uid="first", scope=OLD, text="叫我 x", at=1000)
    assert await commit(store, [identity_op("add_alias", "x", "叫我 x", "first")], scope=OLD)
    await source(store, uid="transfer", scope=OLD, user="200", text="不对，叫我 x", at=2000)
    assert await commit(store, [identity_op("reassign_alias", "x", "不对，叫我 x", "transfer")], scope=OLD, user="200")
    await source(store, uid="late", scope=OLD, text="别叫我 x", at=1500)
    assert await commit(store, [identity_op("remove_alias", "x", "别叫我 x", "late")], scope=OLD)
    removals = (await store.get_user_profile(OLD, "100"))["profile"]["removals"]
    assert removals["alias:x"] == removals["alias-transfer:x"] == 2000
    assert await resolve_member(SCOPES, "x", store) == "200"


@pytest.mark.asyncio
async def test_tombstone_budget_keeps_transfer_time_and_kind_as_one_entry(store):
    from tests.test_user_profile_identity import identity_op
    from astrbot_plugin_livingmemory.core.user_profile import resolve_member, apply_operations
    await source(store, uid="first", scope=OLD, text="叫我 x", at=1000)
    assert await commit(store, [identity_op("add_alias", "x", "叫我 x", "first")], scope=OLD)
    await source(store, uid="transfer", scope=OLD, user="200", text="不对，叫我 x", at=2000)
    assert await commit(store, [identity_op("reassign_alias", "x", "不对，叫我 x", "transfer")], scope=OLD, user="200")
    for index in range(19):
        uid, val = "remove-" + str(index), "alias" + str(index)
        text = "别叫我 " + val
        await source(store, uid=uid, scope=OLD, text=text, at=3000+index)
        assert await commit(store, [identity_op("remove_alias", val, text, uid)], scope=OLD)
    profile = (await store.get_user_profile(OLD, "100"))["profile"]
    assert profile["removals"]["alias:x"] == profile["removals"]["alias-transfer:x"] == 2000
    assert len([key for key in profile["removals"] if key.startswith("alias:")]) == 20
    assert apply_operations(profile, [], {}, "100")["removals"] == profile["removals"]
    assert await resolve_member(SCOPES, "x", store) == "200"
    # The 21st logical entry evicts the whole oldest entry, never half of it.
    await source(store, uid="overflow", scope=OLD, text="别叫我 overflow", at=4000)
    assert await commit(store, [identity_op("remove_alias", "overflow", "别叫我 overflow", "overflow")], scope=OLD)
    trimmed = (await store.get_user_profile(OLD, "100"))["profile"]["removals"]
    assert "alias:x" not in trimmed and "alias-transfer:x" not in trimmed


@pytest.mark.parametrize("reverse", [False, True])
def test_merging_tombstones_keeps_kind_paired_with_its_timestamp(reverse):
    older, newer = empty_profile(), empty_profile()
    older["removals"] = {"alias:x": 1500}
    newer["removals"] = {"alias:x": 2000, "alias-transfer:x": 2000}
    values = [older, newer]
    merged = merge_profiles(list(reversed(values)) if reverse else values)
    assert merged["removals"]["alias:x"] == merged["removals"]["alias-transfer:x"] == 2000


@pytest.mark.asyncio
async def test_personal_alias_removal_never_deletes_another_same_named_user(store):
    from tests.test_user_profile_identity import identity_op
    from astrbot_plugin_livingmemory.core.user_profile import resolve_member
    for uid, scope, user, at in [("old-add", OLD, "100", 1000), ("new-add", NEW, "200", 2000)]:
        await source(store, uid=uid, scope=scope, user=user, text="叫我 x", at=at)
        assert await commit(store, [identity_op("add_alias", "x", "叫我 x", uid)], scope=scope, user=user)
    assert await resolve_member(SCOPES, "x", store) == ["100", "200"]
    await source(store, uid="personal-remove", scope=OLD, text="别叫我 x", at=3000)
    assert await commit(store, [identity_op("remove_alias", "x", "别叫我 x", "personal-remove")], scope=OLD)
    assert await resolve_member(SCOPES, "x", store) == "200"
    runtime = UserProfileRuntime(SimpleNamespace(store=store, config=config(), enabled=True,
        authorized=AsyncMock(return_value=True)))
    other = event()
    other.get_sender_id = lambda: "200"
    profile = await runtime.profile_for_event(other)
    assert profile["identity"]["aliases"][0]["val"] == "x"
    assert "alias:x" not in profile["removals"]
    service = ArchiveQueryService(store)
    result = await service.query(conversation=NEW, read_scopes=SCOPES, requester="100",
        question="x说过什么", person="x", person_role="speaker", mode="time_read",
        start="1970-01-01T00:00:00+00:00", end="2030-01-01T00:00:00+00:00", now=5000)
    assert [row["account_id"] for row in result["resolved_query"]["accounts"]] == ["200"]


@pytest.mark.asyncio
async def test_personal_alias_removal_does_not_block_other_users_archive_nickname(store):
    from tests.test_user_profile_identity import identity_op
    from astrbot_plugin_livingmemory.core.user_profile import resolve_member
    await source(store, uid="personal-remove", scope=OLD, text="别叫我 x", at=3000)
    assert await commit(store, [identity_op("remove_alias", "x", "别叫我 x", "personal-remove")], scope=OLD)
    for uid in ("100", "200"):
        await store.archive_record(ArchiveMessage(uid="nickname-" + uid, conversation=NEW, sender_id=uid,
            nickname="x", platform="qq", text="observed nickname", sent_at=1000))
    assert await resolve_member(SCOPES, "x", store) == "200"
    service = ArchiveQueryService(store)
    service.alias_resolver = AsyncMock(return_value=[{"sender_id": "100", "aliases": ["x"]}, {"sender_id": "200", "aliases": ["x"]}])
    result = await service.query(conversation=NEW, read_scopes=SCOPES, requester="100", question="x说过什么",
        person="x", person_role="speaker", mode="time_read", start="1970-01-01T00:00:00+00:00",
        end="2030-01-01T00:00:00+00:00", now=5000)
    assert [row["account_id"] for row in result["resolved_query"]["accounts"]] == ["200"]
