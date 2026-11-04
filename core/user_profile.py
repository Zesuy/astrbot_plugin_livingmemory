"""Bounded, evidence-backed slots. No inferred personality is persisted."""
import copy
import json
import re

SINGLE_SLOTS = {"current_main_game": "最近主玩", "device_env": "设备环境"}
SET_SLOTS = {"hobbies": "喜好/偏好", "key_milestones": "关键经历"}
FIRST_PERSON = re.compile(r"我|咱|本人|\bI\b|\bmy\b", re.I)
NEGATION = re.compile(r"不再|不喜欢|不爱|没兴趣|放弃|退坑|不用|停止|no longer|don't|do not|quit", re.I)
IDENTITY_ACTIONS = {"set_preferred_name", "set_real_name", "add_alias", "remove_alias", "reassign_alias"}
IDENTITY_INTENT = re.compile(r"真名|叫我|别叫|不要叫|外号|别名|代指|简称|指的是|是指|就是|不是.+是|是.+不是|不对.+是|@")


def identity_text(text):
    return (2 <= len(text) <= 2000 and not text.startswith(("/", "!", "#", ">"))
            and bool(IDENTITY_INTENT.search(text)))


def identity_evidence(row):
    text = plain_evidence(row)
    mentions = row.get("mentions", [])
    if isinstance(mentions, str):
        mentions = json.loads(mentions)
    return identity_text(text) or (2 <= len(text) <= 2000
        and not text.startswith(("/", "!", "#", ">"))
        and bool(mentions) and bool(re.search(r"叫|是", text)))


def name_in_text(name, text):
    """ASCII abbreviations must be independent tokens, including beside Chinese."""
    if re.fullmatch(r"[a-zA-Z0-9_]+", name):
        return bool(re.search(r"(?<![a-zA-Z0-9_])" + re.escape(name) + r"(?![a-zA-Z0-9_])", text, re.I))
    return normalize_value(name) in normalize_value(text)


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
    return {"identity": {"preferred_name": None, "real_name": None, "aliases": []},
            "single_slots": {}, "set_slots": {}, "removals": {}}


def normalize_value(value):
    return re.sub(r"\s+", "", value).casefold()


def apply_operations(profile, operations, sources, user_id):
    """Apply self-reported facts and explicitly bound, literal identity evidence."""
    result = copy.deepcopy(profile)
    single = result.setdefault("single_slots", {})
    sets = result.setdefault("set_slots", {})
    removals = result.setdefault("removals", {})
    identity = result.setdefault("identity", empty_profile()["identity"])
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
        if row is None or row["role"] != "user":
            continue
        text = plain_evidence(row)
        if action in IDENTITY_ACTIONS:
            # Cross-person targets are resolved against archived names by the store;
            # this pure reducer requires its explicit, validated target binding.
            target = op.get("target_user_id", row["sender_id"])
            if (target != user_id or not identity_evidence(row) or not quote or quote not in text
                    or not 1 <= len(val) <= 40 or not name_in_text(val, quote)
                    or (row["sender_id"] != user_id and not op.get("_identity_bound"))):
                continue
            if action in ("remove_alias", "reassign_alias") and not re.search(r"不对|不是|别叫|不要叫|叫错|搞错|纠正|不再", quote):
                continue
            at = row["sent_at"]
            if not isinstance(at, (int, float)) or at <= 0:
                continue
            fact = {"val": val, "source_uid": uid, "at": at, "author_id": row["sender_id"]}
            validated.append((at, row["id"], action, "identity", fact))
            continue
        if row["sender_id"] != user_id:
            continue
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
        if slot == "identity":
            if action.startswith("set_"):
                key = action[4:]
                if (identity.get(key) or {}).get("at", 0) <= fact["at"]:
                    identity[key] = fact
            else:
                norm = normalize_value(fact["val"])
                key = "alias:" + norm
                items = identity.setdefault("aliases", [])
                if any(normalize_value(f["val"]) == norm and f["at"] > fact["at"] for f in items):
                    continue
                if action == "remove_alias":
                    removals[key] = max(removals.get(key, 0), fact["at"])
                    items[:] = [f for f in items if normalize_value(f["val"]) != norm]
                elif removals.get(key, 0) < fact["at"]:
                    items[:] = [f for f in items if normalize_value(f["val"]) != norm]
                    items.append(fact)
            continue
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
    facts += [(v["at"], "identity", k, v) for k, v in identity.items() if k != "aliases" and v]
    facts += [(v["at"], "identity", "aliases", v) for v in identity.get("aliases", [])]
    bounded, chars, count = empty_profile(), 0, 0
    for _, section, slot, fact in sorted(facts, key=lambda item: item[0], reverse=True):
        if count >= 12 or chars + len(fact["val"]) > 200:
            continue
        if section == "identity":
            if slot == "aliases":
                bounded[section][slot].append(fact)
            else:
                bounded[section][slot] = fact
        elif section == "single_slots":
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
    identity = profile.get("identity", {})
    preferred, real = identity.get("preferred_name"), identity.get("real_name")
    details = []
    if preferred:
        name = preferred["val"]
    if real:
        details.append("真名: " + real["val"])
    aliases = identity.get("aliases", [])
    if aliases:
        details.append("别称: " + ", ".join(f["val"] for f in aliases))
    if details:
        name += " (" + ", ".join(details) + ")"
    return f"[说话人档案: {name}]\n" + "\n".join(rows) if rows or preferred or details else ""


async def resolve_member(scope_id, token, store, *, include_archive=True):
    """Return a unique UID, an ambiguity list, or None; never merge identities."""
    conn = getattr(store, "read_connection", store)
    rows = await conn.execute_fetchall("SELECT user_id,profile_json FROM user_profiles WHERE scope_id=?", (scope_id,))
    found = set()
    for uid, payload in rows:
        identity = json.loads(payload).get("identity", {})
        names = [identity.get("preferred_name"), identity.get("real_name"), *identity.get("aliases", [])]
        if any(f and normalize_value(f["val"]) == normalize_value(token) for f in names):
            found.add(uid)
    if not found and include_archive:
        rows = await conn.execute_fetchall("SELECT DISTINCT sender_id,nickname FROM archive_names WHERE conversation=?", (scope_id,))
        found = {uid for uid, name in rows if normalize_value(token) in (normalize_value(name), uid)}
    ordered = sorted(found)
    return ordered[0] if len(ordered) == 1 else ordered or None


def correction_aliases(text):
    """Small request-local grammar; unresolved corrections remain user assertions."""
    name = r"([A-Za-z0-9_\u4e00-\u9fff]+?)"
    name_target = r"([A-Za-z0-9_\u4e00-\u9fff]{1,20})"
    patterns = [
        r"(?:不对|搞错了|纠正一下)[，,：:\s]*" + name + r"\s*(?:是指|指的是|代指|就是|是)\s*" + name_target,
        name + r"\s*(?:指的)?不是\s*[A-Za-z0-9_\u4e00-\u9fff]+?[，,\s]+(?:而是|是)\s*" + name_target
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return {match[1]: match[2]}
    return {}
