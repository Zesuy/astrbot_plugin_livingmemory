import json
from types import SimpleNamespace

import pytest

from astrbot_plugin_livingmemory.core.clean_context import CleanContextInjector
from astrbot_plugin_livingmemory.core.user_profile import (
    apply_operations, correction_aliases, empty_profile, name_in_text, resolve_member,
)
from astrbot_plugin_livingmemory.core.managers.archive_query_service import ArchiveQueryService
from astrbot_plugin_livingmemory.core.models.archive_models import ArchiveMessage
from astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall import MemoryRecall
from astrbot_plugin_livingmemory.core.utils.injection_adapter import InjectionAdapter
from .test_user_profile import store, source, commit, runtime, event


def identity_op(action, value, quote, uid="one", target="我"):
    return {"op": action, "val": value, "quote": quote, "source_uid": uid, "target": target}


@pytest.mark.asyncio
async def test_real_name_and_preferred_name_latest_timestamp(store):
    await source(store, text="我的真名是张伟，叫我强哥")
    assert await commit(store, [identity_op("set_real_name", "张伟", "我的真名是张伟"),
                                identity_op("set_preferred_name", "强哥", "叫我强哥")])
    await source(store, "new", "以后叫我老张", at=3000)
    assert await commit(store, [identity_op("set_preferred_name", "老张", "以后叫我老张", "new")])
    await source(store, "old", "以前叫我小张", at=2000)
    assert await commit(store, [identity_op("set_preferred_name", "小张", "以前叫我小张", "old")])
    profile = (await store.get_user_profile("group:a", "100"))["profile"]
    assert profile["identity"]["preferred_name"]["val"] == "老张"
    assert profile["identity"]["real_name"]["author_id"] == "100"
    assert "老张 （真名： 张伟）" in CleanContextInjector.format_context(profile=profile)


@pytest.mark.asyncio
async def test_third_party_introduction_and_atomic_transfer(store):
    await source(store, "presence", user="200", text="路过")
    await source(store, text="张三的真名是张伟")
    # Both members initially share an archived nickname: do not guess.
    assert await commit(store, [identity_op("set_real_name", "张伟", "张三的真名是张伟", target="张三")])
    assert (await store.get_user_profile("group:a", "200"))["revision"] == 0
    await source(store, "bind", "我的真名是老王，叫我王哥", user="200", at=2000)
    assert await commit(store, [identity_op("set_real_name", "老王", "我的真名是老王", "bind")], user="200")
    await source(store, "alias", "叫我 x", at=2500)
    assert await commit(store, [identity_op("add_alias", "x", "叫我 x", "alias")])
    await source(store, "correct", "x 不是小李，是老王", at=3000)
    assert await commit(store, [identity_op("reassign_alias", "x", "x 不是小李，是老王", "correct", "老王")])
    assert await resolve_member("group:a", "x", store) == "200"
    previous = (await store.get_user_profile("group:a", "100"))["profile"]
    assert previous["removals"]["alias:x"] == 3000
    await source(store, "late", "叫我 x", at=2600)
    assert await commit(store, [identity_op("add_alias", "x", "叫我 x", "late")])
    assert await resolve_member("group:a", "x", store) == "200"
    await source(store, "intro", "老王的真名是王强", at=4000)
    assert await commit(store, [identity_op("set_real_name", "王强", "老王的真名是王强", "intro", "老王")])
    assert await resolve_member("group:a", "王强", store) == "200"
    result = await ArchiveQueryService(store).query(conversation="group:a", requester="100",
        question="x 说过什么", person="x", mode="time_read", start="1970-01-01T00:00:00+00:00",
        end="2030-01-01T00:00:00+00:00", now=5000)
    assert result["resolved_query"]["accounts"][0]["account_id"] == "200"


@pytest.mark.asyncio
async def test_alias_resolution_scope_ambiguity_and_tokens(store):
    for user in ("100", "200"):
        uid = "alias-" + user
        await source(store, uid, "叫我 x", user=user)
        assert await commit(store, [identity_op("add_alias", "x", "叫我 x", uid)], user=user)
    assert await resolve_member("group:a", "x", store) == ["100", "200"]
    assert await resolve_member("group:b", "x", store) is None
    assert await resolve_member("group:a", "Xbox", store) is None
    assert name_in_text("x", "x 是谁") and name_in_text("x", "问问x吧")
    assert not name_in_text("x", "Xbox exercise xx")


def test_identity_exception_does_not_open_hard_fact_slots():
    text = "张三就是主玩Linux的人"
    row = {"sender_id": "200", "role": "user", "id": 1, "sent_at": 1000,
           "components": json.dumps([{"type": "plain", "text": text}])}
    result = apply_operations(empty_profile(), [{"op": "set", "slot": "device_env", "val": "Linux",
        "quote": text, "source_uid": "one", "target_user_id": "100"}], {"one": row}, "100")
    assert result == empty_profile()


@pytest.mark.asyncio
async def test_runtime_learns_non_first_person_identity(store):
    await source(store, "self", "我的真名是老王", user="200")
    assert await commit(store, [identity_op("set_real_name", "老王", "我的真名是老王", "self")], user="200")
    await source(store, text="x 指的是老王", at=2000)
    worker = runtime(store)
    async def extract(payload):
        assert "x 指的是老王" in payload
        return json.dumps({"operations": [identity_op("add_alias", "x", "x 指的是老王", target="老王")]})
    assert await worker.refresh("group:a", "100", event=event(), extractor=extract)
    assert await resolve_member("group:a", "x", store) == "200"


@pytest.mark.asyncio
async def test_native_mention_identity_and_explicit_removal(store):
    record = ArchiveMessage(uid="one", conversation="group:a", sender_id="100", nickname="张三",
        platform="qq", text="叫老王", sent_at=1000, mentions=["200"],
        components=[{"type": "at", "qq": "200"}, {"type": "plain", "text": "叫老王"}])
    await store.archive_record(record)
    assert await commit(store, [identity_op("add_alias", "老王", "叫老王", target="200")])
    assert await resolve_member("group:a", "老王", store) == "200"
    await source(store, "remove", "别叫我老王", user="200", at=3000)
    assert await commit(store, [identity_op("remove_alias", "老王", "别叫我老王", "remove")], user="200")
    assert await resolve_member("group:a", "老王", store) is None
    await source(store, "older", "叫我老王", user="200", at=2000)
    assert await commit(store, [identity_op("add_alias", "老王", "叫我老王", "older")], user="200")
    assert await resolve_member("group:a", "老王", store) is None


@pytest.mark.parametrize("text", ["不对，x 是老王", "x 不是小李，是老王"])
def test_request_local_correction_and_natural_response(text):
    mapping = correction_aliases(text)
    assert mapping == {"x": "老王"}
    context = CleanContextInjector.format_context(speaker="群友", corrections=mapping)
    assert '"x": "老王"' in context and "自然改口" in context
    assert "本轮称呼更正" not in CleanContextInjector.format_context(speaker="群友")


@pytest.mark.asyncio
async def test_correction_injected_as_temporary_context_without_waiting_for_extractor(store):
    worker, ev = runtime(store), event()
    ev.set_extra("_lm_alias_corrections", correction_aliases("不对，x 是老王"))
    recall = MemoryRecall(None, worker.config, None, SimpleNamespace(store=store), None, InjectionAdapter())
    request = SimpleNamespace(extra_user_content_parts=[])
    recall._inject_clean_context(ev, request, [], "")
    part = request.extra_user_content_parts[0]
    assert "本轮称呼更正" in part.text and part._no_save
    assert (await store.get_user_profile("group:a", "100"))["revision"] == 0


def test_identity_text_supports_daizhi_and_shizhi():
    from astrbot_plugin_livingmemory.core.user_profile import identity_text
    assert identity_text("以后用小路代指四格蝶") is True
    assert identity_text("小路是指四格蝶") is True


def test_correction_aliases_handles_zhideshi_without_greed():
    from astrbot_plugin_livingmemory.core.user_profile import correction_aliases
    assert correction_aliases("不对，小路指的是幽默老蜀，不是四格蝶！") == {"小路": "幽默老蜀"}
    assert correction_aliases("不对，x是指老王") == {"x": "老王"}
    assert correction_aliases("小路不是四格蝶，是幽默老蜀") == {"小路": "幽默老蜀"}
