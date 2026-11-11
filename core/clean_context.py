"""Natural request-local context, using already filtered recall results."""

from .dialogue_buffer import sanitize_display_name
from .utils.formatting import _memory_injection_content, safe_parse_metadata


def _format_scope_label(scope: str, current_scope: str) -> str:
    """Convert internal UMO to human-readable group label."""
    s = str(scope or "").strip()
    curr = str(current_scope or "").strip()
    is_current = (s == curr) or (s and curr and s.split(":")[-1] == curr.split(":")[-1])
    group_id = s.split(":")[-1] if ":" in s else s
    
    # 针对两个已知核心熟人群的可读命名
    if group_id == "909660891":
        name = "老群"
    elif group_id == "530216971":
        name = "新群"
    else:
        name = f"群聊{group_id}"
        
    if is_current:
        return f"本群({name} · {group_id})"
    return f"隔壁群({name} · {group_id})"


class CleanContextInjector:
    @staticmethod
    def format_context(
        dialogue: str = "",
        memories=(),
        speaker: str = "",
        profile=None,
        corrections=None,
        *,
        self_id="",
        current_scope="",
        permitted_scopes=(),
        speaker_id="",
        cross_group_context: str = "",
    ) -> str:
        sections = [dialogue] if dialogue else []

        # 1. 隔壁群实时串门与热聊动态感知
        if cross_group_context and cross_group_context.strip():
            sections.append(cross_group_context.strip())

        # 2. 长期记忆与历史事件（附带来源群出处）
        rows = []
        budget = 6000
        for memory in memories:
            metadata = safe_parse_metadata(memory.get("metadata", {}))
            content = _memory_injection_content(memory.get("content", ""), metadata)
            if content:
                source_scope = metadata.get("source_session_id") or metadata.get("session_id") or ""
                origin_label = ""
                if source_scope:
                    origin_label = f"[{_format_scope_label(source_scope, current_scope)}] "
                row = f"- {origin_label}{content[:800]}"
                if len(row) > budget:
                    break
                rows.append(row)
                budget -= len(row) + 1
        if rows:
            sections.append("[长期记忆与历史事件]\n" + "\n".join(rows))

        # 3. 称呼更正
        if isinstance(corrections, dict) and corrections:
            import json
            safe_corrections = {
                sanitize_display_name(name): sanitize_display_name(target)
                for name, target in corrections.items()
            }
            sections.append(
                "[本轮称呼更正]\n"
                + json.dumps(safe_corrections, ensure_ascii=False)
                + "\n这是用户本轮明确更正的称呼，本轮优先按此理解；若成员仍有歧义，简短自然确认。"
                "叫错后像朋友一样自然改口，可以轻松幽默，不长篇道歉，也不固执沿用旧称呼。"
            )

        # 4. 发言人称呼
        if isinstance(profile, dict):
            from .user_profile import format_profile
            speaker = format_profile(profile, speaker) or speaker
        speaker = sanitize_display_name(speaker)

        # 5. 格式化自身定位与会话锚点
        all_scopes = list(permitted_scopes) if permitted_scopes else ([current_scope] if current_scope else [])
        scope_labels = [_format_scope_label(s, current_scope) for s in all_scopes]
        scopes_text = "、".join(scope_labels) if scope_labels else "当前群聊"
        current_label = _format_scope_label(current_scope, current_scope) if current_scope else "本群"

        permitted_text = ", ".join(permitted_scopes) if permitted_scopes else current_scope
        sections.append(
            "[当前会话]\n"
            f"你以账号 {self_id} 在会话 {current_scope} 发言。历史中属于该账号的发言为你过去所说；其他作者各自独立。\n"
            f"你同时在以下互通群聊中活动：{scopes_text}，在两边保持稳定、统一、熟络的人格。\n"
            f"本轮允许回忆范围：{permitted_text}。\n"
            f"当前交互对象：{speaker}(ID:{speaker_id})。"
        )

        # 6. 交互事实与回忆认知契约
        sections.append(
            "【交互事实与回忆认知契约】\n"
            "1. 根据发言者 ID 区分人员与指向。群聊记录是某人在某时说的话，不代表你的运行状态或客观现实；涉及当前系统状态时，必须以受控探针结果为准。\n"
            "2. 历史中的自身回答仅代表过去的表述，绝不能作为证明外部事实的依据；他人对你的评价或调侃，不改变你的真实设定。\n"
            "3. 当群友询问过往共同经历、人物印象或是否记得某人时，若当前材料不足，在工具可用时先使用记忆检索工具查证真实历史，再据实自然作答；样本少则客观说明，绝不凭空编造经历。\n"
            "4. 共享历史不代表当前共同在线，各会话有自己的当下话题；跨会话获知的信息保留出处，不擅自当成当前现场已知。\n"
            "5. 标记为【修仙游戏】或包含指令回执的记录属于群内文字游戏。命令证明当时有人发起操作；回执证明游戏机器人报告了数值结果。可以据此回答游戏进度，但不得当成现实生活事实、职业或身体状态。"
        )
        return "\n\n".join(sections)
