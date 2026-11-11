"""
Tests for MemoryProcessor.
"""

import json
import tempfile
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_livingmemory.core.models.conversation_models import Message
from astrbot_plugin_livingmemory.core.processors.memory_processor import MemoryProcessor
from astrbot_plugin_livingmemory.core.prompts.prompt_manager import (
    get_prompt_manager,
    init_prompt_manager,
)


class _DummyLLMProvider:
    def __init__(self, completion_text: str):
        self._completion_text = completion_text
        self.text_chat = AsyncMock(side_effect=self._chat)

    async def _chat(self, prompt: str, system_prompt: str):
        return SimpleNamespace(completion_text=self._completion_text)


def _make_messages():
    return [
        Message(
            id=1,
            session_id="s1",
            role="user",
            content="明天下午三点开会",
            sender_id="u1",
            sender_name="张三",
            group_id=None,
            platform="test",
            metadata={},
        ),
        Message(
            id=2,
            session_id="s1",
            role="assistant",
            content="收到，我会提醒你",
            sender_id="bot",
            sender_name="Bot",
            group_id=None,
            platform="test",
            metadata={"is_bot_message": True},
        ),
    ]


@pytest.mark.asyncio
async def test_process_conversation_success():
    llm = _DummyLLMProvider(
        """{
            "summary":"张三明天下午三点要开会呀，我已经认真记下来啦！",
            "canonical_summary":"张三明天下午三点开会，Bot 已确认提醒",
            "topics":["会议提醒"],
            "key_facts":["张三明天下午三点开会"],
            "sentiment":"neutral",
            "importance":0.8
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None, config={"summary_mode": "persona"})

    content, metadata, importance = await processor.process_conversation(
        messages=_make_messages(),
        is_group_chat=False,
        persona_id=None,
    )

    assert "张三" in content
    assert metadata["interaction_type"] == "private_chat"
    assert "会议提醒" in metadata["topics"]
    assert importance == 0.8


@pytest.mark.asyncio
async def test_process_conversation_discards_non_json_response():
    llm = _DummyLLMProvider("summary=测试, importance=0.6")
    processor = MemoryProcessor(llm_provider=llm, context=None, config={"summary_mode": "persona"})

    content, metadata, importance = await processor.process_conversation(
        messages=_make_messages(),
        is_group_chat=False,
        persona_id=None,
    )

    assert content == ""
    assert metadata["processing_status"] == "processed_no_memory"
    assert importance == 0


class TestPromptLiveReload:
    """验证 WebUI 保存后 MemoryProcessor 立即使用新 prompt（不依赖实例字段缓存）。"""

    def test_get_chat_prompt_reads_from_prompt_manager(self):
        custom_text = "自定义私聊 prompt 内容 [{conversation}]"
        with tempfile.TemporaryDirectory() as tmpdir:
            init_prompt_manager(tmpdir)
            get_prompt_manager().update_prompt("private_chat_prompt", custom_text)

            llm = _DummyLLMProvider("{}")
            processor = MemoryProcessor(llm_provider=llm, context=None, config={"summary_mode": "persona"})

            live = processor._get_chat_prompt(is_group_chat=False)
            assert live == custom_text

            # 清理：重置为默认，避免影响其他测试
            get_prompt_manager().reset_prompt("private_chat_prompt")

    def test_get_chat_prompt_returns_valid_content(self):
        llm = _DummyLLMProvider("{}")
        processor = MemoryProcessor(llm_provider=llm, context=None, config={"summary_mode": "persona"})
        live = processor._get_chat_prompt(is_group_chat=False)
        assert isinstance(live, str) and len(live) > 50
        assert "{conversation}" in live


@pytest.mark.asyncio
async def test_persona_prompt_is_included_when_available():
    llm = _DummyLLMProvider(
        """{
            "summary":"我愉快地记录了这次交流",
            "topics":["闲聊"],
            "key_facts":["用户问候"],
            "sentiment":"positive",
            "importance":0.5
        }"""
    )
    context = Mock()
    context.persona_manager = Mock()
    context.persona_manager.get_persona = AsyncMock(
        return_value=SimpleNamespace(system_prompt="你是活泼助手")
    )

    processor = MemoryProcessor(llm_provider=llm, context=context, config={"summary_mode": "persona"})

    system_prompt = await processor._build_system_prompt_with_persona("persona_1")
    assert "人格设定" in system_prompt
    assert "活泼助手" in system_prompt


# ── New tests for dual-channel summary and quality validator ──────────────────


@pytest.mark.asyncio
async def test_dual_channel_summary_stores_canonical_and_persona():
    """
    process_conversation 应在 metadata 中同时存储
    canonical_summary（供图抽取等中性消费方）和 persona_summary（人格风格展示用），
    且检索内容 content 恒为 summary + key_facts 的富文本。
    """
    llm = _DummyLLMProvider(
        """{
            "summary":"张三明天下午三点要开会呀，我已经认真记下来啦！",
            "canonical_summary":"张三明天下午三点开会，Bot 已确认提醒",
            "topics":["会议提醒"],
            "key_facts":["张三明天下午三点开会"],
            "sentiment":"neutral",
            "importance":0.8
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None, config={"summary_mode": "persona"})

    content, metadata, importance = await processor.process_conversation(
        messages=_make_messages(),
        is_group_chat=False,
        persona_id=None,
    )

    # canonical_summary 应保留 LLM 输出（自定义提示词兼容）
    assert "canonical_summary" in metadata
    assert "呀" not in metadata["canonical_summary"]
    assert metadata["canonical_summary"] == "张三明天下午三点开会，Bot 已确认提醒"

    # persona_summary 应存在（等于原始 LLM summary）
    assert "persona_summary" in metadata
    assert "张三" in metadata["persona_summary"]
    assert "呀" in metadata["persona_summary"]

    # content 应为 summary + key_facts 富文本（检索语料）
    assert (
        content
        == "张三明天下午三点要开会呀，我已经认真记下来啦！ | 张三明天下午三点开会"
    )

    # schema 版本标记
    assert metadata.get("summary_schema_version") == "v2"


@pytest.mark.asyncio
async def test_source_time_tags_come_from_message_timestamps_without_rewriting_summary():
    llm = _DummyLLMProvider(
        '{"summary":"张三确认本次新版本的发布计划", "canonical_summary":"发布计划已确认", '
        '"topics":["发布"], "key_facts":["发布计划已确认"], '
        '"sentiment":"neutral", "importance":0.8}'
    )
    messages = _make_messages()
    messages[0].timestamp = datetime(2025, 5, 1, 9, 0).timestamp()
    messages[1].timestamp = datetime(2025, 5, 2, 10, 0).timestamp()
    processor = MemoryProcessor(llm_provider=llm, context=None, config={"summary_mode": "persona"})

    content, metadata, _ = await processor.process_conversation(messages)

    assert content == "张三确认本次新版本的发布计划 | 发布计划已确认"
    assert metadata["canonical_summary"] == "发布计划已确认"
    assert metadata["time_tags"] == ["2025-05-01", "2025-05-02"]
    assert metadata["source_time_label"] == "2025-05-01 - 2025-05-02"


def test_atom_classification_persists_parent_memory_types():
    processor = MemoryProcessor(context=None)
    metadata = {
        "key_facts": ["明天下午发布新版本", "用户喜欢爵士乐"],
        "topics": ["发布", "音乐"],
    }

    atoms = processor.classify_atoms_from_metadata(metadata)

    assert len(atoms) == 2
    assert metadata["atom_types"] == ["planned", "preference"]


@pytest.mark.asyncio
async def test_canonical_summary_falls_back_to_rich_text():
    """旧/自定义 Prompt 缺少 canonical_summary 时应回退为 summary + key_facts 富文本。"""
    llm = _DummyLLMProvider(
        """{
            "summary":"用户提到了一个重要事项",
            "canonical_summary":null,
            "topics":["备忘"],
            "key_facts":["明天下午三点开会", "需要准备PPT"],
            "sentiment":"neutral",
            "importance":0.7
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None, config={"summary_mode": "persona"})

    content, metadata, _ = await processor.process_conversation(
        messages=_make_messages(),
        is_group_chat=False,
        persona_id=None,
    )

    # 回退应同时包含 summary 与 key_facts，保证检索语料信息密度
    assert "明天下午三点开会" in metadata["canonical_summary"]
    assert "需要准备PPT" in metadata["canonical_summary"]
    assert "用户提到了一个重要事项" in metadata["canonical_summary"]
    assert content == metadata["canonical_summary"]


@pytest.mark.asyncio
async def test_summary_quality_normal_for_valid_response():
    """有效的 LLM 响应应标记为 summary_quality=normal。"""
    llm = _DummyLLMProvider(
        """{
            "summary":"用户告知明天下午三点有重要会议需要参加",
            "topics":["会议"],
            "key_facts":["明天下午三点开会"],
            "sentiment":"neutral",
            "importance":0.8
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None, config={"summary_mode": "persona"})

    _, metadata, _ = await processor.process_conversation(
        messages=_make_messages(),
        is_group_chat=False,
        persona_id=None,
    )

    assert metadata.get("summary_quality") == "normal"


@pytest.mark.asyncio
async def test_summary_quality_low_for_empty_summary():
    """summary 为空时应标记为 summary_quality=low。"""
    llm = _DummyLLMProvider(
        """{
            "summary":"",
            "topics":["闲聊"],
            "key_facts":["用户问候"],
            "sentiment":"neutral",
            "importance":0.5
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None, config={"summary_mode": "persona"})

    _, metadata, _ = await processor.process_conversation(
        messages=_make_messages(),
        is_group_chat=False,
        persona_id=None,
    )

    assert metadata.get("summary_quality") == "low"


@pytest.mark.asyncio
async def test_summary_quality_low_for_missing_key_facts():
    """key_facts 为空时应标记为 summary_quality=low。"""
    llm = _DummyLLMProvider(
        """{
            "summary":"用户进行了一次普通对话",
            "topics":["闲聊"],
            "key_facts":[],
            "sentiment":"neutral",
            "importance":0.5
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None, config={"summary_mode": "persona"})

    _, metadata, _ = await processor.process_conversation(
        messages=_make_messages(),
        is_group_chat=False,
        persona_id=None,
    )

    assert metadata.get("summary_quality") == "low"


@pytest.mark.asyncio
async def test_summary_quality_low_for_generic_terms():
    """summary 包含泛化词（某用户、有人等）时应标记为 summary_quality=low。"""
    llm = _DummyLLMProvider(
        """{
            "summary":"某用户提到了一些事情",
            "topics":["闲聊"],
            "key_facts":["某用户说了话"],
            "sentiment":"neutral",
            "importance":0.5
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None, config={"summary_mode": "persona"})

    _, metadata, _ = await processor.process_conversation(
        messages=_make_messages(),
        is_group_chat=False,
        persona_id=None,
    )

    assert metadata.get("summary_quality") == "low"


def test_validate_summary_quality_directly():
    """直接测试 _validate_summary_quality 的各种边界情况。"""
    from unittest.mock import MagicMock

    processor = MemoryProcessor(llm_provider=MagicMock(), context=None)

    # 正常情况
    assert (
        processor._validate_summary_quality(
            {
                "summary": "用户明确表示喜欢吃寿司",
                "key_facts": ["用户喜欢寿司"],
                "importance": 0.7,
            }
        )
        == "normal"
    )

    # summary 过短
    assert (
        processor._validate_summary_quality(
            {
                "summary": "短",
                "key_facts": ["fact"],
                "importance": 0.5,
            }
        )
        == "low"
    )

    # importance 超出范围
    assert (
        processor._validate_summary_quality(
            {
                "summary": "用户明确表示喜欢吃寿司",
                "key_facts": ["用户喜欢寿司"],
                "importance": 1.5,
            }
        )
        == "low"
    )

    # 泛化词检测
    assert (
        processor._validate_summary_quality(
            {
                "summary": "有人提到了一些事情",
                "key_facts": ["有人说话"],
                "importance": 0.5,
            }
        )
        == "low"
    )


def test_build_memory_from_structured_data_uses_standard_storage_format():
    processor = MemoryProcessor(llm_provider=Mock(), context=None)

    content, metadata, importance = processor.build_memory_from_structured_data(
        {
            "summary": "用户希望主动记忆工具复用自动总结格式",
            "topics": ["LivingMemory", "主动记忆"],
            "key_facts": ["主动记忆应复用 MemoryProcessor 格式化流程"],
            "sentiment": "neutral",
            "importance": 0.8,
        },
        is_group_chat=False,
        fallback_excerpt="fallback",
    )

    assert content == metadata["canonical_summary"]
    assert metadata["persona_summary"] == "用户希望主动记忆工具复用自动总结格式"
    assert metadata["topics"] == ["LivingMemory", "主动记忆"]
    assert metadata["key_facts"] == ["主动记忆应复用 MemoryProcessor 格式化流程"]
    assert metadata["sentiment"] == "neutral"
    assert metadata["interaction_type"] == "private_chat"
    assert metadata["summary_schema_version"] == "v2"
    assert metadata["summary_quality"] == "normal"
    assert importance == 0.8


def test_build_memory_from_structured_data_flags_low_quality_for_out_of_range_importance():
    """与自动总结路径一致：原始 importance 越界时应判为 low quality。"""
    processor = MemoryProcessor(llm_provider=Mock(), context=None)

    _, metadata, importance = processor.build_memory_from_structured_data(
        {
            "summary": "用户希望主动记忆工具复用自动总结格式",
            "topics": ["测试"],
            "key_facts": ["importance 越界"],
            "sentiment": "neutral",
            "importance": 1.5,
        },
        is_group_chat=False,
        fallback_excerpt="fallback",
    )

    assert metadata["summary_quality"] == "low"
    assert importance == 1.0


# ── 群聊路径测试 ──────────────────────────────────────────────────────────────


def _make_group_messages():
    """构造一组群聊消息（含 group_id）"""
    return [
        Message(
            id=1,
            session_id="aiocqhttp:GroupMessage:88888",
            role="user",
            content="大家觉得 AI 工具怎么样？",
            sender_id="10001",
            sender_name="张三",
            group_id="88888",
            platform="aiocqhttp",
            metadata={},
        ),
        Message(
            id=2,
            session_id="aiocqhttp:GroupMessage:88888",
            role="user",
            content="我觉得 ChatGPT 写代码效率提升了 30%",
            sender_id="10002",
            sender_name="李四",
            group_id="88888",
            platform="aiocqhttp",
            metadata={},
        ),
        Message(
            id=3,
            session_id="aiocqhttp:GroupMessage:88888",
            role="assistant",
            content="AI 工具确实能提升效率，但需要仔细审查生成的代码",
            sender_id="bot",
            sender_name="Bot",
            group_id="88888",
            platform="aiocqhttp",
            metadata={"is_bot_message": True},
        ),
    ]


@pytest.mark.asyncio
async def test_process_group_chat_sets_interaction_type():
    """群聊路径应将 interaction_type 设置为 group_chat。"""
    llm = _DummyLLMProvider(
        """{
            "summary":"群聊讨论了 AI 工具的使用效果",
            "topics":["AI工具","工作效率"],
            "key_facts":["张三认为 ChatGPT 效率提升 30%","需要仔细审查 AI 生成代码"],
            "participants":["张三","李四"],
            "sentiment":"positive",
            "importance":0.75
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None, config={"summary_mode": "persona"})

    content, metadata, importance = await processor.process_conversation(
        messages=_make_group_messages(),
        is_group_chat=True,
        persona_id=None,
    )

    assert metadata["interaction_type"] == "group_chat"
    assert importance == 0.75


@pytest.mark.asyncio
async def test_process_group_chat_extracts_participants():
    """群聊路径应正确提取 participants 字段。"""
    llm = _DummyLLMProvider(
        """{
            "summary":"群聊讨论了 AI 工具的使用效果",
            "topics":["AI工具"],
            "key_facts":["张三认为 ChatGPT 效率提升 30%"],
            "participants":["张三","李四","王五"],
            "sentiment":"positive",
            "importance":0.7
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None, config={"summary_mode": "persona"})

    _, metadata, _ = await processor.process_conversation(
        messages=_make_group_messages(),
        is_group_chat=True,
        persona_id=None,
    )

    assert "participants" in metadata
    assert "张三" in metadata["participants"]
    assert "李四" in metadata["participants"]
    assert "王五" in metadata["participants"]


@pytest.mark.asyncio
async def test_process_group_chat_dual_channel_summary():
    """群聊路径也应生成双通道摘要（canonical_summary + persona_summary）。"""
    llm = _DummyLLMProvider(
        """{
            "summary":"群聊讨论了 AI 工具的使用效果，建议内部部署私有化 LLM",
            "topics":["AI工具","数据安全"],
            "key_facts":["建议公司内部部署私有化 LLM","注意数据安全"],
            "participants":["张三","李四"],
            "sentiment":"positive",
            "importance":0.8
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None, config={"summary_mode": "persona"})

    content, metadata, _ = await processor.process_conversation(
        messages=_make_group_messages(),
        is_group_chat=True,
        persona_id=None,
    )

    assert "canonical_summary" in metadata
    assert "persona_summary" in metadata
    assert metadata.get("summary_schema_version") == "v2"
    # canonical_summary 应包含 key_facts
    assert "私有化 LLM" in metadata["canonical_summary"]
    # content 应等于 canonical_summary
    assert content == metadata["canonical_summary"]


@pytest.mark.asyncio
async def test_process_group_chat_missing_participants_uses_default():
    """群聊 LLM 响应缺少 participants 字段时，应使用空列表默认值。"""
    llm = _DummyLLMProvider(
        """{
            "summary":"张三与李四确认以后每周五发布新版本",
            "topics":["闲聊"],
            "key_facts":["张三与李四约定每周五发布"],
            "sentiment":"neutral",
            "importance":0.5
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None, config={"summary_mode": "persona"})

    _, metadata, _ = await processor.process_conversation(
        messages=_make_group_messages(),
        is_group_chat=True,
        persona_id=None,
    )

    # 缺少 participants 时应补充默认空列表
    assert "participants" in metadata
    assert isinstance(metadata["participants"], list)


@pytest.mark.asyncio
async def test_process_private_chat_no_participants_field():
    """私聊路径不应在 metadata 中包含 participants 字段。"""
    llm = _DummyLLMProvider(
        """{
            "summary":"用户告知明天下午三点有重要会议",
            "topics":["会议"],
            "key_facts":["明天下午三点开会"],
            "sentiment":"neutral",
            "importance":0.8
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None, config={"summary_mode": "persona"})

    _, metadata, _ = await processor.process_conversation(
        messages=_make_messages(),
        is_group_chat=False,
        persona_id=None,
    )

    assert "participants" not in metadata
    assert metadata["interaction_type"] == "private_chat"


@pytest.mark.asyncio
async def test_process_group_chat_long_content():
    """群聊长内容（多条消息）应正常处理，不崩溃。"""
    long_messages = []
    for i in range(20):
        long_messages.append(
            Message(
                id=i + 1,
                session_id="aiocqhttp:GroupMessage:99999",
                role="user",
                content=f"成员{i % 5} 说：这是第 {i + 1} 条消息，内容比较详细，包含了很多信息。"
                * 3,
                sender_id=str(10000 + i % 5),
                sender_name=f"成员{i % 5}",
                group_id="99999",
                platform="aiocqhttp",
                metadata={},
            )
        )

    llm = _DummyLLMProvider(
        """{
            "summary":"群聊成员进行了多轮讨论，涉及多个话题",
            "topics":["群聊","讨论"],
            "key_facts":["多名成员参与讨论","讨论内容丰富"],
            "participants":["成员0","成员1","成员2","成员3","成员4"],
            "sentiment":"neutral",
            "importance":0.6
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None, config={"summary_mode": "persona"})

    content, metadata, importance = await processor.process_conversation(
        messages=long_messages,
        is_group_chat=True,
        persona_id=None,
    )

    assert isinstance(content, str) and len(content) > 0
    assert metadata["interaction_type"] == "group_chat"
    assert len(metadata["participants"]) == 5
    assert 0.0 <= importance <= 1.0


@pytest.mark.asyncio
async def test_process_group_chat_quality_low_for_generic_terms():
    """群聊总结包含泛化词时，summary_quality 应为 low。"""
    llm = _DummyLLMProvider(
        """{
            "summary":"某用户在群里说了一些话",
            "topics":["闲聊"],
            "key_facts":["有人说话了"],
            "participants":["某用户"],
            "sentiment":"neutral",
            "importance":0.4
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None, config={"summary_mode": "persona"})

    _, metadata, _ = await processor.process_conversation(
        messages=_make_group_messages(),
        is_group_chat=True,
        persona_id=None,
    )

    assert metadata.get("summary_quality") == "low"


def test_format_conversation_sanitizes_multimodal_private_message():
    processor = MemoryProcessor(llm_provider=None, context=None)
    message = Message(
        id=1,
        session_id="s1",
        role="user",
        content=[
            {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
            {"type": "text", "text": "这张图里有会议安排"},
        ],
        sender_id="u1",
        sender_name="张三",
        group_id=None,
        platform="test",
        metadata={},
    )

    formatted = processor._format_conversation([message])

    assert "这张图里有会议安排" in formatted
    assert "image_url" not in formatted
    assert "example.test" not in formatted


def test_format_conversation_uses_placeholder_for_image_only_group_message():
    processor = MemoryProcessor(llm_provider=None, context=None)
    message = Message(
        id=1,
        session_id="g1",
        role="user",
        content=[
            {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}
        ],
        sender_id="u1",
        sender_name="张三",
        group_id="group1",
        platform="test",
        metadata={},
    )

    formatted = processor._format_conversation([message])

    assert "张三" in formatted
    assert "[图片消息]" in formatted
    assert "image_url" not in formatted


@pytest.mark.asyncio
async def test_process_conversation_skips_pure_bot_spam_without_llm():
    """Verify that pure bot spam segments directly return processed_no_memory without calling LLM."""
    cfg = {"filtering_settings": {"bot_ids": "2292937107, 88888"}}
    processor = MemoryProcessor(llm_provider=None, context=None, config=cfg)
    
    # 模拟全是机器人的刷屏切片
    bot_messages = [
        Message(
            id=i,
            session_id="g1",
            role="assistant" if i % 2 == 0 else "user",
            content=f"跑团报表骰点战斗日志 {i}",
            sender_id="2292937107" if i % 2 != 0 else "3658276025",
            sender_name="小入机" if i % 2 != 0 else "Bot",
            group_id="group1",
            platform="test",
            metadata={},
        )
        for i in range(10)
    ]

    content, metadata, importance = await processor.process_conversation(bot_messages, is_group_chat=True)
    assert content == ""
    assert metadata["processing_status"] == "processed_no_memory"
    assert metadata["no_memory_reason"] in ("automation_only", "bot_spam_only")
    assert importance == 0.0


@pytest.mark.asyncio
async def test_process_conversation_skips_human_bot_xiuxian_pairs():
    """Verify that human command -> bot response pairs (e.g. /修仙) are skipped without calling LLM."""
    processor = MemoryProcessor(llm_provider=None, context=None)
    
    # 模拟真实群聊中的人机修仙互刷：真人发指令 -> 机器人回面板报表
    xiuxian_messages = []
    for i in range(15):
        # 真人发送修仙指令
        xiuxian_messages.append(
            Message(
                id=i*2,
                session_id="group_xiuxian",
                role="user",
                content="/修仙 闭关" if i % 2 == 0 else "/我的灵石",
                sender_id=f"human_{i%3}",
                sender_name=f"道友{i%3}",
                group_id="group_xiuxian",
                platform="test",
                metadata={},
            )
        )
        # 机器人回复大属性报表
        xiuxian_messages.append(
            Message(
                id=i*2 + 1,
                session_id="group_xiuxian",
                role="user",
                content=f"【闭关结算】道友闭关成功，修为+{1000*i}，突破至筑基第{i}层，当前灵石: 50000",
                sender_id="2292937107",
                sender_name="修仙助手",
                group_id="group_xiuxian",
                platform="test",
                metadata={},
            )
        )

    content, metadata, importance = await processor.process_conversation(xiuxian_messages, is_group_chat=True)
    assert content == ""
    assert metadata["processing_status"] == "processed_no_memory"
    assert metadata["no_memory_reason"] == "automation_only"
    assert metadata["activity_filter"]["filtered_commands"] == 15
    assert metadata["activity_filter"]["filtered_responses"] == 15
    assert importance == 0.0


@pytest.mark.asyncio
async def test_process_conversation_filters_xiuxian_but_retains_real_human_chat():
    """Verify that xiuxian commands are filtered out while real human social chatter is retained in the prompt."""
    from unittest.mock import AsyncMock
    mock_provider = AsyncMock()
    # 模拟 LLM 正常返回结构化数据
    mock_llm_json = json.dumps({
        "summary": "张三提到他明天要去上海出差。",
            "canonical_summary": "张三提到他明天要去上海出差。",
        "events": [{"summary": "张三明天去上海出差", "kind": "plan", "source_refs": ["m0"]}],
        "key_facts": ["张三明天出差上海"],
        "quality": "normal"
    })
    mock_provider.text_chat = AsyncMock(return_value=SimpleNamespace(completion_text=mock_llm_json))

    processor = MemoryProcessor(llm_provider=mock_provider, context=None)
    processor._get_current_llm_provider = AsyncMock(return_value=mock_provider)

    # 构造混合切片：修仙指令 + 真实出差计划 + 修仙指令
    mixed_messages = [
        Message(id=1, session_id="g1", role="user", content="/修仙 突破", sender_id="u1", sender_name="张三", group_id="g1", platform="test", metadata={}),
        Message(id=2, session_id="g1", role="user", content="【天道突破】恭喜突破成功，修为+9999", sender_id="bot1", sender_name="修仙Bot", group_id="g1", platform="test", metadata={}),
        # 真实社交发言
        Message(id=3, session_id="g1", role="user", content="我明天要去上海出差，下周才回来", sender_id="u1", sender_name="张三", group_id="g1", platform="test", timestamp=1794390000.0, metadata={}),
        Message(id=4, session_id="g1", role="user", content="/我的灵石", sender_id="u2", sender_name="李四", group_id="g1", platform="test", metadata={}),
        Message(id=5, session_id="g1", role="user", content="【灵石报表】当前灵石: 8888", sender_id="bot1", sender_name="修仙Bot", group_id="g1", platform="test", metadata={}),
    ]

    content, metadata, importance = await processor.process_conversation(mixed_messages, is_group_chat=True)
    print("DEBUG RETURN: content=", repr(content), "metadata=", metadata, "importance=", importance)
    # 验证 LLM 收到的 Prompt 中只包含真人出差发言，完全不包含 /修仙 和 修为+9999
    prompt_sent = mock_provider.text_chat.call_args.kwargs.get("prompt") or mock_provider.text_chat.call_args[0][0]
    assert "我明天要去上海出差" in prompt_sent
    assert "/修仙 突破" not in prompt_sent
    assert "修为+9999" not in prompt_sent
    assert "出差" in content


def test_natural_chatter_with_xiuxian_word_is_not_falsely_filtered():
    """Verify that natural conversation containing '修仙' is not mistaken for a game command."""
    from astrbot_plugin_livingmemory.core.automation_policy import is_automated_game_transaction, is_game_command
    
    # 正常人类随口调侃
    assert is_game_command("我最近天天熬夜修仙打代码，快猝死了哈哈") is False
    assert is_automated_game_transaction("我最近天天熬夜修仙打代码，快猝死了哈哈") is False
    assert is_game_command("项目终于突破瓶颈了，太爽了") is False
    assert is_automated_game_transaction("项目终于突破瓶颈了，太爽了") is False

    # 真实的修仙指令与回执
    assert is_game_command("/修仙 闭关") is True
    assert is_game_command("！修仙 炼丹") is True
    assert is_game_command("#我的灵石") is True
    assert is_automated_game_transaction("【天道突破】修为+5000，当前突破至金丹期") is True
