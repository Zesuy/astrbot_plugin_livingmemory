"""The only model-visible entry to precise archive queries."""
from __future__ import annotations

import asyncio
import json
from dataclasses import field
from typing import Any

from pydantic.dataclasses import dataclass
from astrbot.api import logger
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext

from ..memory_scope import is_event_memory_allowed, adapter_access
from ..passive_group_capture import (PassiveGroupCaptureFilter,is_session_enabled,
                                    is_plugin_enabled_for_session)


@dataclass
class ChatEventsTool(FunctionTool[AstrAgentContext]):
    __pydantic_config__ = {"arbitrary_types_allowed":True}
    runtime: Any = None
    name: str = "query_chat_events"
    description: str = (
        "按关键词 + 时间段 + 人物（可选）检索当前会话的真实原始聊天记录。问某人刚才/昨天说了什么、昨天群里聊了什么、之前如何讨论某技术时，必须先调用本工具查证，不能凭空编造或用模糊长期记忆代替原文。回顾一段时间的群聊（日报、周报、补看错过的讨论）时，先用overview发现话题；近期上下文和别人的群报不能代替该时段原始讨论。"
        "要模仿具体群友的说话方式、替其拟回复时，先用style_sample取得本人发言样本，再据此模仿，不拿单条旧梗当整个人的风格。普通闲聊无需查询。"
        "Query retained original messages in this current group/private conversation. "
        "Use when you decide a precise historical name, quote, date, person's statement or sequence needs checking; "
        "compressed long-term memories are only hints. Nothing is searched until you call this tool. "
        "For open-ended daily reports, catching up or what happened while someone was quiet, use mode=overview with the original question and time_range/start/end; do not guess topic keywords first. "
        "overview samples contiguous discussions across the whole window and returns density; supply person=我 for the requester's no-post activity timeline. This is evidence scouting, not all-topic coverage. "
        "For imitating a specific member's speech use mode=style_sample with person=their account/nickname (person=我 binds the current requester); it returns diverse author_plain examples with quotes separated. "
        "Generic fictional or occupational styles do not require a person lookup. "
        "lookup checks known topics; time_read reads a chosen chronological window after overview when more context is needed. "
        "person_role=speaker means messages authored by that account; about means discussed person and must not filter by author. "
        "Supply the original question; focused keywords apply to lookup only. overview/style_sample do not need guessed keywords. For follow-ups include the resolved previous topic. "
        "If the user corrects an earlier answer, keep their requested distinction and search that missing detail; "
        "do not restrict the search to your earlier guessed answer. person_context_candidate is only a related-account lead, not topic confirmation. "
        "Yesterday evening/昨晚 means Asia/Shanghai yesterday18:00 to today06:00. "
        "Use returned cursor for further pages, or mode=read with a message evidence_handle to expand originals; follow next_text_handle for long-message remaining text. "
        "Stop when evidence answers the question; do not read every page mechanically. Never present samples as exhaustive statistics. "
        "Preserve the requested day after midnight: 今天 means this calendar day in Asia/Shanghai, not yesterday. State the range or clarify an ambiguous absence. "
        "No-post intervals do not prove offline status, unread messages or a specific absence; posts also do not prove continuous presence. Inspect activity and choose a stated interval rather than silently picking the longest gap. "
        "An unresolved nickname may return literal mentions only, which must not be promoted to a confirmed account identity. "
        "Report missing/partial evidence honestly. Bot's earlier guesses, mentions and reply relationships do not prove facts."
    )
    parameters: dict = field(default_factory=lambda:{"type":"object","properties":{
        "query":{"type":"string","description":"用户原始历史问题；保留人物、时间及具体话题。"},
        "mode":{"type":"string","enum":["lookup","time_read","read","overview","style_sample"],"default":"lookup","description":"overview=时段群聊概览，未知话题先取样；style_sample=模仿具体群友前获取本人语料；lookup=按关键词查话题；time_read=按时间顺着上下文读某段讨论（可按作者筛选）；read=展开原文。"},
        "person":{"type":"string","description":"可选人物昵称或账号ID；我=当前提问者，你=机器人。查询某人说的话须同时指定 person_role=speaker；省略则查全会话。style_sample必填；overview只查看该人物活动时间线，不过滤作者。"},
        "person_role":{"type":"string","enum":["speaker","about","any"],"default":"about","description":"speaker=该人亲口说的；about=提到或讨论该人的，含他人发言；any=相关人物线索。问某人说了什么用speaker。"},
        "keywords":{"type":"array","items":{"type":"string"},"description":"可选关键词列表，如 [跑团,角色卡]；用于lookup搜索讨论话题或词汇，不把人物昵称当话题。无关键词查某人发言可用time_read。"},
        "time_range":{"type":"string","description":"可选时间范围：今天、昨天、昨晚、前天、最近3天、最近一周、刚才（最近30分钟）或2026-10-04。北京时间，最近N天含今天从首日零点至现在，昨晚为昨日18点至今日6点（不超过现在）。overview须指定时段；style_sample默认最近30天。"},
        "start":{"type":"string","description":"可选精确ISO8601起点（含），须带时区如2026-10-04T18:00:00+08:00；与end成对，不能同时传time_range。"},
        "end":{"type":"string","description":"可选精确ISO8601终点（不含），须带时区；超过当前时间自动截至现在。"},
        "cursor":{"type":"string","description":"Opaque returned cursor; continuation uses its fixed original query and scope."},
        "evidence_handle":{"type":"string","description":"Returned handle for mode=read."},
        "page_size":{"type":"integer","default":40,"minimum":1,"maximum":100}},"required":["query"]})

    async def call(self, context: ContextWrapper[AstrAgentContext], query: str,
                   mode="lookup", person="", person_role="about", keywords=None,
                   time_range="", start="", end="", cursor="", evidence_handle="",
                   page_size=40) -> ToolExecResult:
        try:
            runtime = self.runtime
            if runtime is None or not runtime.ready or not runtime.enabled or not runtime.config.get("archive.query_enabled",True):
                return json.dumps({"status":"unavailable","reason":"archive_tool_not_ready"})
            event = context.context.event
            scope = str(event.unified_msg_origin)
            if not await runtime.authorized(event):
                return json.dumps({"status":"unavailable","reason":"access_denied"})
            if person in {'我','本人','提问者'}:
                person = str(event.get_sender_id())
            elif person in {'你','机器人自己'}:
                person = str(event.get_self_id())
            captured = event.get_extra('_lm_archive_record',None) if hasattr(event,'get_extra') else None
            exclude_uid = getattr(captured,'uid','')
            exclude_uid = exclude_uid if isinstance(exclude_uid,str) else ''
            with adapter_access(runtime.config, event):
                result = await asyncio.wait_for(runtime.service.query(conversation=scope,
                    requester=str(event.get_sender_id()),question=query,mode=mode,person=person,
                    person_role=person_role,keywords=keywords,time_range=time_range,start=start,end=end,
                    cursor=cursor,evidence_handle=evidence_handle,page_size=page_size,
                    exclude_uid=exclude_uid),timeout=20)
            result.setdefault("archive_coverage",{}).update({"known_capture_failures":runtime.gaps})
            logger.info("[ArchiveQuery] tool-only mode=%s status=%s messages=%s",mode,result.get("status"),len(result.get("messages",[])))
            return json.dumps(result,ensure_ascii=False)
        except asyncio.CancelledError:
            raise
        except (TypeError,ValueError) as exc:
            return json.dumps({"status":"error","reason":str(exc)},ensure_ascii=False)
        except Exception:
            logger.exception("[ArchiveQuery] 工具执行失败")
            return json.dumps({"status":"error","reason":"archive_query_failed"})
