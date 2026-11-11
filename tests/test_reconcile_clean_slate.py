"""Synthetic database recovery checks; no production text or credentials."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts import reconcile_clean_slate_memories as recovery
from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "memory.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, text TEXT, metadata TEXT)")
    connection.execute("CREATE TABLE memory_atoms (id INTEGER PRIMARY KEY, parent_memory_id INTEGER, metadata TEXT)")
    connection.execute("CREATE TABLE graph_entries (id INTEGER PRIMARY KEY, source_memory_id INTEGER, metadata TEXT)")
    samples = [
        (1, "决定每周备份代码", {}),
        (2, "哈哈，笑死", {"events": [{"kind": "joke", "summary": "调侃"}]}),
        (3, "Request failed: Timeout exception", {}),
        (4, "鼠鼠道歉，同时讨论部署计划", {"summary_quality": "low"}),
        (5, "林舟喜欢红茶", {"recall_exclusion_reason": "manual", "recall_excluded": 1}),
        (6, "林舟参加活动", {"events": [{"kind": "fact", "summary": "参加活动"}]}),
    ]
    for identity, text, extra in samples:
        metadata = {"recall_excluded": 1, "recall_exclusion_reason": recovery.REASON, "persona_id": "backup", **extra}
        # Deliberate whitespace ensures rollback restores original bytes.
        raw = json.dumps(metadata, ensure_ascii=False, indent=2)
        connection.execute("INSERT INTO documents VALUES (?, ?, ?)", (identity, text, raw))
        for table in ("memory_atoms", "graph_entries"):
            connection.execute(f"INSERT INTO {table} VALUES (?, ?, ?)", (identity, identity, json.dumps({"child_only": identity, "recall_excluded": 1})))
    connection.execute("INSERT INTO documents VALUES (7, 'synthetic malformed', '{broken')")
    connection.commit()
    yield connection, path
    connection.close()


def snapshot(connection):
    return {table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in ("documents", "memory_atoms", "graph_entries")}


def test_survey_counts_without_mutation_or_text(database, capsys):
    connection, path = database
    before = snapshot(connection)
    recovery.main(["--db", str(path), "--survey"])
    output = capsys.readouterr().out
    summary = json.loads(output)
    assert summary["candidates"] == 5
    assert summary["restore"] == 3
    assert summary["audit_noise"] == 2
    assert summary["malformed_metadata_skipped"] == 1
    assert "决定" not in output and "Request failed" not in output
    assert snapshot(connection) == before
    assert connection.execute("SELECT name FROM sqlite_master WHERE name LIKE 'clean_slate_recovery%'").fetchall() == []


def test_default_is_survey(database, capsys):
    _, path = database
    recovery.main(["--db", str(path)])
    assert json.loads(capsys.readouterr().out)["read_only"] is True


def test_apply_syncs_and_rollback_restores_exact_metadata(database):
    connection, _ = database
    before = snapshot(connection)
    outcome = recovery.apply(connection)
    assert outcome["processed"] == 5
    assert outcome["synced"] == {"memory_atoms": 5, "graph_entries": 5}
    for table in ("documents", "memory_atoms", "graph_entries"):
        for identity in (1, 4, 6):
            metadata = json.loads(connection.execute(f"SELECT metadata FROM {table} WHERE id=?", (identity,)).fetchone()[0])
            assert metadata["recall_excluded"] == 0
            assert "recall_exclusion_reason" not in metadata
            assert metadata.get("child_only", identity) == identity
        for identity in (2, 3):
            metadata = json.loads(connection.execute(f"SELECT metadata FROM {table} WHERE id=?", (identity,)).fetchone()[0])
            assert metadata["recall_excluded"] == 1
            assert metadata["recall_exclusion_reason"] == "audit_noise"
        assert connection.execute(f"SELECT * FROM {table} WHERE id=5").fetchone() == before[table][4]
    assert recovery.rollback(connection, outcome["batch_id"])["rolled_back"] == 5
    assert snapshot(connection) == before
    with pytest.raises(ValueError, match="Unknown"):
        recovery.rollback(connection, outcome["batch_id"])


@pytest.mark.parametrize("table", ["documents", "memory_atoms", "graph_entries"])
def test_rollback_refuses_later_changes_atomically(database, table):
    connection, _ = database
    batch = recovery.apply(connection)["batch_id"]
    connection.execute(f"UPDATE {table} SET metadata='{{}}' WHERE id=3")
    connection.commit()
    before = snapshot(connection)
    with pytest.raises(ValueError, match="changed after recovery"):
        recovery.rollback(connection, batch)
    assert snapshot(connection) == before
    assert connection.execute("SELECT COUNT(*) FROM clean_slate_recovery_backup WHERE batch_id=?", (batch,)).fetchone()[0] == 5


def test_apply_failure_rolls_back_all_tables_and_backup(database):
    connection, _ = database
    connection.execute("UPDATE graph_entries SET metadata='not-json' WHERE id=4")
    connection.commit()
    before = snapshot(connection)
    with pytest.raises(ValueError, match="Invalid metadata"):
        recovery.apply(connection)
    assert snapshot(connection) == before
    assert connection.execute("SELECT name FROM sqlite_master WHERE name LIKE 'clean_slate_recovery%'").fetchall() == []


@pytest.mark.parametrize("table", ["memory_atoms", "graph_entries"])
@pytest.mark.parametrize("reason", ["manual", "audit_noise", "dirty_memory_audit:banter"])
def test_independent_related_quarantine_is_preserved(database, table, reason):
    connection, _ = database
    # Both a restored parent and a noise parent must preserve independent reasons.
    for identity in (1, 2):
        raw = json.dumps({"recall_excluded": 1, "recall_exclusion_reason": reason}, indent=2)
        connection.execute(f"UPDATE {table} SET metadata=? WHERE id=?", (raw, identity))
    connection.commit()
    before = snapshot(connection)
    result = recovery.apply(connection)
    for identity in (1, 2):
        assert connection.execute(f"SELECT * FROM {table} WHERE id=?", (identity,)).fetchone() == before[table][identity - 1]
    assert connection.execute("SELECT COUNT(*) FROM clean_slate_recovery_related_backup WHERE batch_id=? AND table_name=?", (result["batch_id"], table)).fetchone()[0] == 3
    recovery.rollback(connection, result["batch_id"])
    assert snapshot(connection) == before


@pytest.mark.parametrize("events", [
    [None], [{}], [{"kind": "unknown"}], [{"kind": None}],
    [{"kind": "unknown", "summary": "哈哈，笑死"}],
    [{"summary": "哈哈，笑死"}], [{"kind": [], "summary": 123}],
    [{"kind": "   ", "summary": "   "}], {"kind": "fact"},
    [{"kind": "unrecognized_legacy_kind"}], [{"kind": "jok"}],
    [{"kind": "garbage", "summary": "哈哈"}],
])
def test_malformed_events_do_not_protect_pure_noise(database, events):
    connection, _ = database
    metadata = {"recall_excluded": 1, "recall_exclusion_reason": recovery.REASON, "events": events}
    connection.execute("UPDATE documents SET text='哈哈，笑死', metadata=? WHERE id=1", (json.dumps(metadata),))
    connection.commit()
    candidates, _ = recovery.scan(connection)
    assert next(item for item in candidates if item["id"] == 1)["noise"] is True
    result = recovery.apply(connection)
    updated = json.loads(connection.execute("SELECT metadata FROM documents WHERE id=1").fetchone()[0])
    assert updated["recall_excluded"] == 1
    assert updated["recall_exclusion_reason"] == "audit_noise"
    assert updated["events"] == events  # Validation never rewrites private source events.
    recovery.rollback(connection, result["batch_id"])


@pytest.mark.parametrize("event", [
    {"kind": "fact"}, {"kind": "unknown", "summary": "决定每周备份代码"},
])
def test_valid_event_or_durable_summary_still_protects_mixed_content(database, event):
    connection, _ = database
    metadata = {"recall_excluded": 1, "recall_exclusion_reason": recovery.REASON, "events": [event]}
    connection.execute("UPDATE documents SET text='哈哈，笑死', metadata=? WHERE id=1", (json.dumps(metadata),))
    connection.commit()
    candidates, _ = recovery.scan(connection)
    assert next(item for item in candidates if item["id"] == 1)["noise"] is False


@pytest.mark.parametrize("table,parent", [("memory_atoms", "parent_memory_id"), ("graph_entries", "source_memory_id")])
@pytest.mark.parametrize("dry_run", [False, True])
def test_rollback_refuses_related_parent_rebinding(database, table, parent, dry_run):
    connection, _ = database
    result = recovery.apply(connection)
    connection.execute(f"UPDATE {table} SET {parent}=999 WHERE id=3")
    connection.commit()
    before = list(connection.iterdump())
    with pytest.raises(ValueError, match="changed after recovery"):
        recovery.rollback(connection, result["batch_id"], dry_run=dry_run)
    assert list(connection.iterdump()) == before


def test_backup_inserts_use_explicit_columns(database):
    connection, _ = database
    # Existing compatible tables may have different physical column order.
    connection.execute("""CREATE TABLE clean_slate_recovery_backup (
        executed_metadata TEXT, applied_at TEXT, original_metadata TEXT,
        doc_id INTEGER, batch_id TEXT, PRIMARY KEY (batch_id, doc_id))""")
    connection.execute("""CREATE TABLE clean_slate_recovery_related_backup (
        applied_at TEXT, executed_metadata TEXT, original_metadata TEXT,
        parent_id INTEGER, row_id INTEGER, table_name TEXT, batch_id TEXT,
        PRIMARY KEY (batch_id, table_name, row_id))""")
    connection.commit()
    before = snapshot(connection)
    result = recovery.apply(connection)
    assert connection.execute("SELECT parent_id, applied_at FROM clean_slate_recovery_related_backup WHERE batch_id=? AND table_name='memory_atoms' AND row_id=1", (result["batch_id"],)).fetchone()[0] == 1
    recovery.rollback(connection, result["batch_id"])
    assert snapshot(connection) == before


@pytest.mark.parametrize("action", ["apply", "rollback"])
def test_cli_dry_run_never_changes_database(database, capsys, action):
    connection, path = database
    batch = recovery.apply(connection)["batch_id"] if action == "rollback" else None
    before = list(connection.iterdump())
    args = ["--db", str(path), "--dry-run", "--" + action]
    if batch:
        args.append(batch)
    recovery.main(args)
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert list(connection.iterdump()) == before


@pytest.mark.parametrize("table", ["memory_atoms", "graph_entries"])
def test_apply_dry_run_validates_related_metadata(database, table):
    connection, path = database
    connection.execute(f"UPDATE {table} SET metadata='broken-json' WHERE id=4")
    connection.commit()
    before = list(connection.iterdump())
    with pytest.raises(ValueError, match=f"Invalid metadata in {table} row 4"):
        recovery.main(["--db", str(path), "--apply", "--dry-run"])
    assert list(connection.iterdump()) == before


def test_apply_dry_run_reports_related_validation_counts(database, capsys):
    connection, path = database
    before = list(connection.iterdump())
    recovery.main(["--db", str(path), "--apply", "--dry-run"])
    result = json.loads(capsys.readouterr().out)
    assert result["synced"] == {"memory_atoms": 5, "graph_entries": 5}
    assert result["processed"] == 5
    assert list(connection.iterdump()) == before


def test_missing_optional_tables_and_unknown_batch(database):
    connection, _ = database
    connection.execute("DROP TABLE memory_atoms")
    connection.execute("DROP TABLE graph_entries")
    connection.commit()
    outcome = recovery.apply(connection)
    assert outcome["synced"] == {}
    with pytest.raises(ValueError, match="Unknown"):
        recovery.rollback(connection, "not-a-batch")
    assert recovery.rollback(connection, outcome["batch_id"])["rolled_back"] == 5


def test_mistyped_database_does_not_create_file(tmp_path):
    path = tmp_path / "absent.db"
    with pytest.raises(sqlite3.OperationalError):
        recovery.main(["--db", str(path), "--apply"])
    assert not path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("container", [tuple, list, set])
async def test_collection_sessions_each_schedule_legacy_migration(tmp_path, container):
    engine = MemoryEngine(str(tmp_path / "engine.db"), SimpleNamespace(),
                          config={"search_cache_enabled": False, "recent_memory_count": 0})
    engine._migrate_session_data_if_needed = AsyncMock()
    engine.hybrid_retriever = SimpleNamespace(search=AsyncMock(return_value=[]))
    sessions = container(["qq:GroupMessage:old", "qq:GroupMessage:new", "livingmemory:global", "plain", ""])
    assert await engine.search_memories("synthetic", session_id=sessions) == []
    await asyncio.gather(*engine._pending_tasks)
    assert {call.args[0] for call in engine._migrate_session_data_if_needed.await_args_list} == {"qq:GroupMessage:old", "qq:GroupMessage:new"}
