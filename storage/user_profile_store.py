"""One-table profile persistence on the archive's existing connection."""
import json
import time

from ..core.user_profile import (apply_operations, empty_profile, IDENTITY_ACTIONS,
                                 plain_evidence, name_in_text, resolve_member, FIRST_PERSON,
                                 normalize_value)

PROFILE_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_profiles (
 scope_id TEXT NOT NULL, user_id TEXT NOT NULL, profile_json TEXT NOT NULL,
 revision INTEGER DEFAULT 1, cursor_id INTEGER DEFAULT 0,
 updated_at INTEGER NOT NULL, PRIMARY KEY(scope_id,user_id)
);
"""


class UserProfileStoreMixin:
    async def get_user_profile(self, scope_id, user_id):
        rows = await self.read_connection.execute_fetchall(
            "SELECT * FROM user_profiles WHERE scope_id=? AND user_id=?", (scope_id, user_id))
        if not rows:
            return {"profile": empty_profile(), "revision": 0, "cursor_id": 0, "updated_at": 0}
        row = dict(rows[0])
        row["profile"] = json.loads(row.pop("profile_json"))
        return row

    async def profile_source_batch(self, scope_id, user_id, cursor_id):
        rows = await self.read_connection.execute_fetchall(
            "SELECT * FROM archive_messages WHERE conversation=? AND sender_id=? "
            "AND id>? AND epoch=? ORDER BY id LIMIT 40",
            (scope_id, user_id, cursor_id, self.accepted_epoch(scope_id)))
        return [dict(row) for row in rows]

    async def profile_neighbor_context(self, scope_id, sources):
        """Bounded nearby Plain context; never admitted as profile evidence."""
        found = {}
        for source in sources:
            rows = await self.read_connection.execute_fetchall(
                "SELECT * FROM archive_messages WHERE conversation=? AND epoch=? "
                "AND id BETWEEN ? AND ? AND ABS(sent_at-?)<=300 ORDER BY id",
                (scope_id, self.accepted_epoch(scope_id), source["id"] - 2,
                 source["id"] + 2, source["sent_at"]))
            for row in rows:
                found[row["id"]] = dict(row)
        return [found[key] for key in sorted(found)][:80]

    async def commit_user_profile(self, scope_id, user_id, operations, *,
                                  expected_revision, cursor_id, epoch, allowed_uids):
        """CAS and source revalidation in a short transaction, after model I/O."""
        async with self.transaction():
            epochs = await self.connection.execute_fetchall(
                "SELECT epoch FROM archive_scopes WHERE conversation=?", (scope_id,))
            if (epochs[0][0] if epochs else 0) != epoch:
                return False
            rows = await self.connection.execute_fetchall(
                "SELECT * FROM user_profiles WHERE scope_id=? AND user_id=?", (scope_id, user_id))
            revision = rows[0]["revision"] if rows else 0
            if revision != expected_revision:
                return False
            current_cursor = rows[0]["cursor_id"] if rows else 0
            if cursor_id <= current_cursor:
                return False
            evidence = await self.connection.execute_fetchall(
                "SELECT * FROM archive_messages WHERE conversation=? AND sender_id=? "
                "AND epoch=? AND id>? AND id<=? ORDER BY id LIMIT 40",
                (scope_id, user_id, epoch, current_cursor, cursor_id))
            sources = {r["uid"]: dict(r) for r in evidence if r["uid"] in allowed_uids}
            if not evidence or evidence[-1]["id"] != cursor_id:
                return False
            profile = json.loads(rows[0]["profile_json"]) if rows else empty_profile()
            profiles = {user_id: profile}
            bound = {}
            for original in operations[:30]:
                if not isinstance(original, dict):
                    continue
                op = dict(original)
                op.pop("_identity_bound", None)
                target = user_id
                if op.get("op") in IDENTITY_ACTIONS:
                    if not all(isinstance(op.get(key), str) for key in ("source_uid", "quote", "val")):
                        continue
                    row = sources.get(op.get("source_uid"))
                    if row is None:
                        continue
                    token = op.get("target", "我")
                    if token in ("我", "本人"):
                        if not FIRST_PERSON.search(op.get("quote", "")):
                            continue
                    elif (isinstance(token, str) and token.isdecimal()
                          and token in json.loads(row.get("mentions") or "[]")):
                        target = token
                    elif isinstance(token, str) and name_in_text(token, op.get("quote", "")):
                        target = await resolve_member(scope_id, token, self.connection)
                        if not isinstance(target, str):
                            continue
                    else:
                        continue
                    if op.get("target_user_id", target) != target:
                        continue
                    op["target_user_id"], op["_identity_bound"] = target, True
                    if target not in profiles:
                        other = await self.connection.execute_fetchall(
                            "SELECT profile_json FROM user_profiles WHERE scope_id=? AND user_id=?", (scope_id, target))
                        profiles[target] = json.loads(other[0][0]) if other else empty_profile()
                    # Validate before touching another member or transferring aliases.
                    changed = apply_operations(profiles[target], [op], sources, target)
                    if changed == profiles[target]:
                        continue
                    if op["op"] == "reassign_alias":
                        owners = await self.connection.execute_fetchall(
                            "SELECT user_id,profile_json FROM user_profiles WHERE scope_id=?", (scope_id,))
                        norm = normalize_value(op["val"])
                        # An old correction must not create a second owner alongside
                        # a binding or removal learned from a newer message.
                        owners = {owner: profiles.get(owner, json.loads(payload)) for owner, payload in owners}
                        latest = [stamp for previous in owners.values() for stamp in
                                  [previous.get("removals", {}).get("alias:" + norm, 0),
                                   *[f["at"] for f in previous.get("identity", {}).get("aliases", [])
                                     if normalize_value(f["val"]) == norm]]]
                        if latest and max(latest) > row["sent_at"]:
                            continue
                        for owner, previous in owners.items():
                            if owner == target:
                                continue
                            removal = dict(op, op="remove_alias", target_user_id=owner)
                            profiles[owner] = apply_operations(previous, [removal], sources, owner)
                bound.setdefault(target, []).append(op)
            for target, ops in bound.items():
                profiles[target] = apply_operations(profiles[target], ops, sources, target)
            profile = profiles.pop(user_id)
            for target, other_profile in profiles.items():
                await self.connection.execute(
                    "INSERT INTO user_profiles VALUES(?,?,?,1,0,?) ON CONFLICT(scope_id,user_id) DO UPDATE SET "
                    "profile_json=excluded.profile_json,revision=user_profiles.revision+1,updated_at=excluded.updated_at",
                    (scope_id, target, json.dumps(other_profile, ensure_ascii=False), int(time.time())))
            payload = json.dumps(profile, ensure_ascii=False)
            now = int(time.time())
            if revision:
                cursor = await self.connection.execute(
                    "UPDATE user_profiles SET profile_json=?,revision=revision+1,cursor_id=?,updated_at=? "
                    "WHERE scope_id=? AND user_id=? AND revision=?",
                    (payload, cursor_id, now, scope_id, user_id, expected_revision))
            else:
                cursor = await self.connection.execute(
                    "INSERT OR IGNORE INTO user_profiles VALUES(?,?,?,1,?,?)",
                    (scope_id, user_id, payload, cursor_id, now))
            return cursor.rowcount == 1
