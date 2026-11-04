"""Bounded, evidence-backed slots. No inferred personality is persisted."""
import copy
import json
import re

SINGLE_SLOTS = {"current_main_game": "最近主玩", "device_env": "设备环境"}
SET_SLOTS = {"hobbies": "喜好/偏好", "key_milestones": "关键经历"}
FIRST_PERSON = re.compile(r"我|咱|本人|\bI\b|\bmy\b", re.I)
NEGATION = re.compile(r"不再|不喜欢|不爱|没兴趣|放弃|退坑|不用|停止|no longer|don't|do not|quit", re.I)


def plain_evidence(row):
    """Only native Plain components; never quoted replies or legacy text."""
    try:
        components = json.loads(row["components"])
    except (TypeError, ValueError):
        return ""
    if not isinstance(components, list):
        return ""
    return " ".join(c.get("text", "") for c in components
                    if isinstance(c, dict) and c.get("type") == "plain"
                    and isinstance(c.get("text"), str)).strip()


def eligible_text(text):
    return (len(text) >= 6 and len(text) <= 2000
            and not text.startswith(("/", "!", "#", ">"))
            and bool(FIRST_PERSON.search(text)))


def empty_profile():
    return {"single_slots": {}, "set_slots": {}, "removals": {}}


def normalize_value(value):
    return re.sub(r"\s+", "", value).casefold()


def apply_operations(profile, operations, sources, user_id):
    """Apply only operations anchored to the target's own literal self-report."""
    result = copy.deepcopy(profile)
    single = result.setdefault("single_slots", {})
    sets = result.setdefault("set_slots", {})
    removals = result.setdefault("removals", {})
    validated = []
    for op in operations[:30]:
        if not isinstance(op, dict):
            continue
        slot, action = op.get("slot"), op.get("op")
        uid, val, quote = op.get("source_uid"), op.get("val"), op.get("quote")
        if not all(isinstance(v, str) for v in (uid, val, quote)):
            continue
        val, quote = val.strip(), quote.strip()
        row = sources.get(uid)
        if row is None or row["sender_id"] != user_id or row["role"] != "user":
            continue
        text = plain_evidence(row)
        if (not eligible_text(text) or not 1 <= len(val) <= 40 or not quote
                or quote not in text or not FIRST_PERSON.search(quote)
                or normalize_value(val) not in normalize_value(quote)):
            continue
        if not isinstance(slot, str) or not isinstance(action, str):
            continue
        if not ((slot in SINGLE_SLOTS and action == "set")
                or (slot in SET_SLOTS and action in ("add", "remove"))):
            continue
        if action == "remove" and not NEGATION.search(quote):
            continue
        at = row["sent_at"]
        if not isinstance(at, (int, float)) or at <= 0:
            continue
        validated.append((at, row["id"], action, slot,
                          {"val": val, "source_uid": uid, "at": at}))
    for _, _, action, slot, fact in sorted(validated, key=lambda item: item[:2]):
        if action == "set":
            if single.get(slot, {}).get("at", 0) <= fact["at"]:
                single[slot] = fact
        else:
            val_norm = normalize_value(fact["val"])
            items = sets.setdefault(slot, [])
            same = [f for f in items if normalize_value(f["val"]) == val_norm]
            if same and max(f["at"] for f in same) > fact["at"]:
                continue
            if action == "remove":
                removals[val_norm] = max(removals.get(val_norm, 0), fact["at"])
                items[:] = [f for f in items if normalize_value(f["val"]) != val_norm]
            elif action == "add":
                if removals.get(val_norm, 0) >= fact["at"]:
                    continue
                removals.pop(val_norm, None)
                items[:] = [f for f in items if normalize_value(f["val"]) != val_norm]
                items.append(fact)
    # Keep the most recent 12 facts and at most 200 content characters.
    facts = [(v["at"], "single_slots", k, v) for k, v in single.items()]
    facts += [(v["at"], "set_slots", k, v) for k, values in sets.items() for v in values]
    bounded, chars, count = empty_profile(), 0, 0
    for _, section, slot, fact in sorted(facts, key=lambda item: item[0], reverse=True):
        if count >= 12 or chars + len(fact["val"]) > 200:
            continue
        if section == "single_slots":
            bounded[section][slot] = fact
        else:
            bounded[section].setdefault(slot, []).append(fact)
        chars += len(fact["val"])
        count += 1
    sorted_removals = sorted(removals.items(), key=lambda kv: kv[1], reverse=True)[:20]
    bounded["removals"] = dict(sorted_removals)
    return bounded


def format_profile(profile, speaker):
    rows = []
    for slot, label in {**SINGLE_SLOTS, **SET_SLOTS}.items():
        value = profile.get("single_slots", {}).get(slot)
        items = [value] if value else profile.get("set_slots", {}).get(slot, [])
        if items:
            values = [" ".join(f["val"].split()) for f in items]
            rows.append(f"- {label}: " + ", ".join(values))
    name = " ".join(str(speaker).split())[:60]
    return f"[说话人档案: {name}]\n" + "\n".join(rows) if rows else ""
