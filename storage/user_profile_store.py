"""One-table profile persistence on the archive's existing connection."""
import json
import time

from ..core.user_profile import apply_operations, empty_profile

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
            profile = apply_operations(profile, operations, sources, user_id)
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
