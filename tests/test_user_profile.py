import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from astrbot.api.platform import MessageType
from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
from astrbot_plugin_livingmemory.core.clean_context import CleanContextInjector
from astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall import MemoryRecall
from astrbot_plugin_livingmemory.core.models.archive_models import ArchiveMessage
from astrbot_plugin_livingmemory.core.user_profile import apply_operations, empty_profile
from astrbot_plugin_livingmemory.core.user_profile_runtime import UserProfileRuntime
from astrbot_plugin_livingmemory.core.utils.injection_adapter import InjectionAdapter
from astrbot_plugin_livingmemory.storage.conversation_store import ConversationStore


@pytest_asyncio.fixture
async def store(tmp_path):
    value = ConversationStore(str(tmp_path / "conversations.db"))
    await value.initialize()
    yield value
    await value.close()


def event(scope="group:a", user="100", own="999"):
    extra = {}
    return SimpleNamespace(unified_msg_origin=scope, get_sender_id=lambda: user,
                           get_self_id=lambda: own, get_group_id=lambda: "a",
                           get_sender_name=lambda: "张三",
                           get_message_type=lambda: MessageType.GROUP_MESSAGE,
                           set_extra=lambda k, v: extra.__setitem__(k, v),
                           get_extra=lambda k, default=None: extra.get(k, default))


async def source(store, uid="one", text="我喜欢Linux", scope="group:a", user="100", at=1000, components=None, role="user"):
    record = ArchiveMessage(uid=uid, conversation=scope, sender_id=user,
                            nickname="张三", platform="aiocqhttp", text=text, sent_at=at,
                            components=components if components is not None else [{"type": "plain", "text": text}],
                            role=role, epoch=store.accepted_epoch(scope))
    await store.archive_record(record)
    return record


def op(uid="one", val="Linux", quote="我喜欢Linux", slot="hobbies", action="add"):
    return {"op": action, "slot": slot, "val": val, "source_uid": uid, "quote": quote}


async def commit(store, operations, scope="group:a", user="100", revision=None, epoch=None, uids=None):
    snapshot = await store.get_user_profile(scope, user)
    rows = await store.profile_source_batch(scope, user, snapshot["cursor_id"])
    return await store.commit_user_profile(scope, user, operations,
        expected_revision=snapshot["revision"] if revision is None else revision,
        cursor_id=rows[-1]["id"], epoch=store.accepted_epoch(scope) if epoch is None else epoch,
        allowed_uids={r["uid"] for r in rows} if uids is None else uids)


def runtime(store, **settings):
    config = ConfigManager({"user_profiles": {"enabled": True, "update_interval_seconds": 0, **settings}})
    archive = SimpleNamespace(store=store, config=config, enabled=True,
                              may_capture=AsyncMock(return_value=True), authorized=AsyncMock(return_value=True))
    return UserProfileRuntime(archive)


@pytest.mark.asyncio
async def test_scope_isolation_and_speaker_binding(store):
    await source(store)
    await source(store, "other-group", scope="group:b", text="我喜欢OpenWrt")
    await source(store, "other-person", user="200", text="我喜欢Python")
    assert await commit(store, [op(), op("other-group", "OpenWrt", "我喜欢OpenWrt"),
                                op("other-person", "Python", "我喜欢Python")])
    profile = (await store.get_user_profile("group:a", "100"))["profile"]
    assert [f["val"] for f in profile["set_slots"]["hobbies"]] == ["Linux"]
    assert (await store.get_user_profile("group:b", "100"))["revision"] == 0
    assert (await store.get_user_profile("group:a", "200"))["revision"] == 0


@pytest.mark.asyncio
async def test_single_latest_original_timestamp_wins(store):
    await source(store, "new", "我主玩游戏B", at=2000)
    await source(store, "old", "我主玩游戏A", at=1000)
    assert await commit(store, [op("new", "游戏B", "我主玩游戏B", "current_main_game", "set"),
                                op("old", "游戏A", "我主玩游戏A", "current_main_game", "set")])
    fact = (await store.get_user_profile("group:a", "100"))["profile"]["single_slots"]["current_main_game"]
    assert fact == {"val": "游戏B", "source_uid": "new", "at": 2000}


@pytest.mark.asyncio
async def test_set_dedup_and_explicit_remove(store):
    await source(store)
    assert await commit(store, [op(), op(val="linux")])
    profile = (await store.get_user_profile("group:a", "100"))["profile"]
    assert len(profile["set_slots"]["hobbies"]) == 1
    await source(store, "two", "我不再喜欢Linux", at=2000)
    assert await commit(store, [op("two", "Linux", "我不再喜欢Linux", action="remove")])
    assert not (await store.get_user_profile("group:a", "100"))["profile"]["set_slots"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [op(action="remove"), op(slot="personality"),
    op(val="性格暴躁"), op(quote="他喜欢Linux"), op(uid="missing"), op(slot="device_env", action="add")])
async def test_unproven_or_invalid_operation_is_rejected(store, operation):
    await source(store)
    assert await commit(store, [operation])
    assert (await store.get_user_profile("group:a", "100"))["profile"] == empty_profile()


@pytest.mark.asyncio
async def test_reply_and_assistant_are_not_evidence(store):
    await source(store, components=[{"type": "reply", "text": "我喜欢Linux"}, {"type": "plain", "text": "哈哈"}])
    await source(store, "bot", role="assistant")
    assert await commit(store, [op(), op("bot")])
    assert (await store.get_user_profile("group:a", "100"))["profile"] == empty_profile()


@pytest.mark.asyncio
async def test_compare_and_swap_rejects_stale_writer(store):
    await source(store)
    results = await asyncio.gather(commit(store, [op()], revision=0), commit(store, [op()], revision=0))
    assert sorted(results) == [False, True]
    await source(store, "two", "我喜欢OpenWrt", at=2000)
    assert not await commit(store, [op("two", "OpenWrt", "我喜欢OpenWrt")], revision=0)
    assert (await store.get_user_profile("group:a", "100"))["revision"] == 1


def test_hard_fact_count_and_character_budget():
    sources, operations = {}, []
    for i in range(20):
        uid, value = str(i), "游戏" + str(i)
        text = "我喜欢" + value
        sources[uid] = {"id": i, "sender_id": "100", "role": "user", "sent_at": i + 1,
                        "components": json.dumps([{"type": "plain", "text": text}])}
        operations.append(op(uid, value, text))
    profile = apply_operations(empty_profile(), operations, sources, "100")
    assert len(profile["set_slots"]["hobbies"]) == 12
    assert sum(len(f["val"]) for f in profile["set_slots"]["hobbies"]) <= 200


@pytest.mark.asyncio
async def test_extractor_only_sees_target_plain_and_runs_without_write_lock(store):
    await source(store)
    await source(store, "other-person", user="200", text="我喜欢Python")
    await source(store, "reply", text="我喜欢被引用的话", components=[{"type": "reply", "text": "我喜欢被引用的话"}])
    rt = runtime(store)
    async def extract(payload):
        assert not store._write_lock.locked()
        data = json.loads(payload)
        assert data["target_user_id"] == "100"
        assert [r["source_uid"] for r in data["other_context"]] == ["other-person"]
        assert [r["source_uid"] for r in data["target_messages"]] == ["one"]
        return json.dumps({"operations": [op()]})
    assert await rt.refresh("group:a", "100", event=event(), extractor=extract)
    assert (await store.get_user_profile("group:a", "100"))["cursor_id"] > 0


@pytest.mark.asyncio
async def test_neighbor_context_is_bounded_to_scope_time_and_not_fact_evidence(store):
    await source(store, "song-context", "接着唱儿歌", user="200")
    await source(store, "one", "我叫到贤小淘气")
    await source(store, "foreign", "私密的其他群", scope="group:b")
    await source(store, "late", "不相关的其他日期", user="200", at=9000)
    rt = runtime(store)
    async def extract(payload):
        data = json.loads(payload)
        assert [r["source_uid"] for r in data["other_context"]] == ["song-context"]
        # Even a model ignoring the instruction cannot use contextual text as evidence.
        return json.dumps({"operations": [op("song-context", "儿歌", "接着唱儿歌")]})
    assert await rt.refresh("group:a", "100", event=event(), extractor=extract)
    assert (await store.get_user_profile("group:a", "100"))["profile"] == empty_profile()


@pytest.mark.asyncio
@pytest.mark.parametrize("user,own,settings", [("999", "999", {}), ("100", "999", {"bot_ids": "100, 200"}), ("unknown", "999", {})])
async def test_known_bots_and_unstable_ids_never_extract(store, user, own, settings):
    await source(store, user=user)
    rt = runtime(store, **settings)
    extractor = AsyncMock()
    assert not await rt.refresh("group:a", user, event=event(user=user, own=own), extractor=extractor)
    extractor.assert_not_called()


@pytest.mark.asyncio
async def test_low_quality_does_not_schedule_model(store):
    rt = runtime(store)
    for i, text in enumerate(("哈哈", "[image]", "/我喜欢Linux", "666666", "今天好热啊")):
        record = await source(store, str(i), text)
        rt.submit(event(), record)
    assert rt.queue.empty() and rt.task is None


@pytest.mark.asyncio
async def test_clear_revokes_inflight_result_and_existing_profiles(store):
    await source(store)
    assert await commit(store, [op()])
    await source(store, "two", "我喜欢OpenWrt", at=2000)
    rt = runtime(store)
    async def extract(payload):
        await store.clear_archive("group:a")
        return json.dumps({"operations": [op("two", "OpenWrt", "我喜欢OpenWrt")]})
    assert not await rt.refresh("group:a", "100", event=event(), extractor=extract)
    assert (await store.get_user_profile("group:a", "100"))["revision"] == 0


@pytest.mark.asyncio
async def test_invalid_model_response_preserves_cursor_and_retry(store):
    await source(store)
    rt = runtime(store)
    with pytest.raises(ValueError):
        await rt.refresh("group:a", "100", event=event(), extractor=AsyncMock(return_value="not json"))
    assert (await store.get_user_profile("group:a", "100"))["cursor_id"] == 0
    assert await rt.refresh("group:a", "100", event=event(), extractor=AsyncMock(return_value='{"operations":[]}'))


@pytest.mark.asyncio
async def test_permission_revoked_during_model_request(store):
    await source(store)
    rt = runtime(store)
    async def extract(payload):
        rt.archive.may_capture.return_value = False
        return json.dumps({"operations": [op()]})
    assert not await rt.refresh("group:a", "100", event=event(), extractor=extract)
    assert (await store.get_user_profile("group:a", "100"))["revision"] == 0


@pytest.mark.asyncio
async def test_wakeup_injection_and_clear_before_format(store):
    await source(store)
    assert await commit(store, [op()])
    rt, ev = runtime(store), event()
    store.user_profiles = rt
    ev.set_extra("_lm_speaker_profile", await rt.profile_for_event(ev))
    recall = MemoryRecall(None, rt.config, None, SimpleNamespace(store=store), None, InjectionAdapter())
    req = SimpleNamespace(extra_user_content_parts=[])
    recall._inject_clean_context(ev, req, [{"content": "周末约好爬山"}], "[群聊近况]\n在聊设备")
    text = req.extra_user_content_parts[0].text
    assert "[说话人档案: 张三]\n- 喜好/偏好: Linux" in text
    assert "[群聊近况]" in text and "[长期记忆 / 群友印象]" in text
    assert req.extra_user_content_parts[0]._no_save
    await store.clear_archive("group:a")
    req.extra_user_content_parts.clear()
    recall._inject_clean_context(ev, req, [])
    assert not req.extra_user_content_parts


@pytest.mark.asyncio
async def test_disabled_private_or_unauthorized_profile_is_not_read(store):
    await source(store)
    assert await commit(store, [op()])
    rt = runtime(store, enabled=False)
    assert await rt.profile_for_event(event()) is None
    rt = runtime(store)
    ev = event()
    ev.get_group_id = lambda: ""
    assert await rt.profile_for_event(ev) is None
    rt.archive.authorized.return_value = False
    assert await rt.profile_for_event(event()) is None


def test_empty_profile_does_not_add_context():
    assert CleanContextInjector.format_context(profile=empty_profile(), speaker="张三") == ""


@pytest.mark.asyncio
async def test_cross_batch_out_of_order_removal_protection(store):
    # Batch 1: User says "I like Linux" at at=1000
    await source(store, uid="u1", text="我喜欢Linux", at=1000)
    assert await commit(store, [op(uid="u1", val="Linux", quote="我喜欢Linux", action="add")])
    p1 = await store.get_user_profile("group:a", "100")
    assert any(f["val"] == "Linux" for f in p1["profile"]["set_slots"]["hobbies"])

    # Batch 2: User says "I quit Linux" at at=3000
    await source(store, uid="u2", text="我不再折腾Linux了", at=3000)
    assert await commit(store, [op(uid="u2", val="Linux", quote="我不再折腾Linux了", action="remove")])
    p2 = await store.get_user_profile("group:a", "100")
    assert not any(f["val"] == "Linux" for f in p2["profile"]["set_slots"].get("hobbies", []))

    # Batch 3 (Late-arriving older evidence at at=2000): should NOT resurrect Linux!
    await source(store, uid="u3", text="我在用Linux工作", at=2000)
    assert await commit(store, [op(uid="u3", val="Linux", quote="我在用Linux工作", action="add")])
    p3 = await store.get_user_profile("group:a", "100")
    assert not any(f["val"] == "Linux" for f in p3["profile"]["set_slots"].get("hobbies", []))

    # Batch 4 (Newer evidence at at=4000): user resumes Linux -> should succeed!
    await source(store, uid="u4", text="我又开始喜欢Linux了", at=4000)
    assert await commit(store, [op(uid="u4", val="Linux", quote="我又开始喜欢Linux了", action="add")])
    p4 = await store.get_user_profile("group:a", "100")
    assert any(f["val"] == "Linux" for f in p4["profile"]["set_slots"]["hobbies"])


@pytest.mark.asyncio
async def test_extractor_handles_markdown_fenced_json(store):
    await source(store, uid="u_fenced", text="我平时主玩原神", at=5000)
    rt = runtime(store)
    store.user_profiles = rt
    ev = event(user="100")
    
    async def mock_extractor(payload):
        return '```json\n{"operations": [{"slot": "current_main_game", "op": "set", "val": "原神", "source_uid": "u_fenced", "quote": "我平时主玩原神"}]}\n```'

    success = await rt.refresh("group:a", "100", event=ev, extractor=mock_extractor)
    assert success is True
    p = await store.get_user_profile("group:a", "100")
    assert p["profile"]["single_slots"]["current_main_game"]["val"] == "原神"
