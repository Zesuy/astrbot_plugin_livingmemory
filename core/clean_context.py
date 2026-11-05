"""Natural request-local context, using already filtered recall results."""

from .utils.formatting import _memory_injection_content, safe_parse_metadata


class CleanContextInjector:
    @staticmethod
    def format_context(dialogue: str = "", memories=(), speaker: str = "", profile=None, corrections=None) -> str:
        sections = [dialogue] if dialogue else []
        if isinstance(profile, dict) and profile:
            from .user_profile import format_profile
            dossier = format_profile(profile, speaker)
            if dossier:
                sections.append(dossier)
        rows = []
        budget = 6000
        for memory in memories:
            metadata = safe_parse_metadata(memory.get("metadata", {}))
            content = _memory_injection_content(memory.get("content", ""), metadata)
            if content:
                row = "- " + content[:800]
                if len(row) > budget:
                    break
                rows.append(row)
                budget -= len(row) + 1
        if rows:
            sections.append("[长期记忆与历史事件]\n" + "\n".join(rows))
        if isinstance(corrections, dict) and corrections:
            import json
            sections.append("[本轮称呼更正]\n" + json.dumps(corrections, ensure_ascii=False)
                            + "\n这是用户本轮明确更正的称呼，本轮优先按此理解；若成员仍有歧义，简短自然确认。"
                            "叫错后像朋友一样自然改口，可以轻松幽默，不长篇道歉，也不固执沿用旧称呼。")
        if sections and speaker:
            sections.append("[本轮说话人]\n" + speaker)
        if sections:
            sections.append(
                "【证据边界与回答准则】\n"
                "1. 评价某人的长期印象或特征时，必须以【说话人档案】与【长期记忆与历史事件】为依据；\n"
                "2. 对【群聊近况】中刚才聊到的话题，只能表述为‘你刚才/今天说……’，严禁将其擅自脑补夸大为‘每天/总是/一贯’等长期固定习惯！\n"
                "3. 若长期依据不足，简短客观说明目前了解的范围（例如：‘就这几次聊天看……’），有多少依据说多少，绝不凭空编造，严禁为了凑字数而强行写长篇当堂观察！\n"
                "4. 【说话人档案】与记忆列表是底层参考资料，回答时用自己的自然群聊口语作答，严禁照搬括号、短横线与档案报告排版。"
            )
        return "\n\n".join(sections)
