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
            sections.append("[长期记忆 / 群友印象]\n" + "\n".join(rows))
        if isinstance(corrections, dict) and corrections:
            import json
            sections.append("[本轮称呼更正]\n" + json.dumps(corrections, ensure_ascii=False)
                            + "\n这是用户本轮明确更正的称呼，本轮优先按此理解；若成员仍有歧义，简短自然确认。"
                            "叫错后像朋友一样自然改口，可以轻松幽默，不长篇道歉，也不固执沿用旧称呼。")
        if sections and speaker:
            sections.append("[本轮说话人]\n" + speaker)
        return "\n\n".join(sections)
