"""Export snapshot and response-lifetime regressions on synthetic WAL vaults."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from starlette.requests import Request

from afair.mcp import export_route
from afair.substrate import open_db
from afair.substrate.events import write_event
from tests.test_export_route import _build_app


def test_export_uses_one_snapshot_for_events_operational_rows_and_blobs(tmp_path):
    conn = open_db(tmp_path)
    write_event(conn, kind="remember", origin="agent", payload={"text": "before"})
    conn.execute(
        "INSERT INTO proposed_ontology_revisions "
        "(id,action,subject_slug,detail,evidence,confidence,detected_by,detected_at) "
        "VALUES ('ont_test','add','test','{}','[]',0.8,'test','2026-01-01')"
    )
    conn.commit()
    stream = export_route._iter_export(tmp_path, include_blobs=True)
    first = json.loads(next(stream))  # Snapshot starts before concurrent commit.
    assert first["payload"]["text"] == "before"
    from afair.substrate.objects import write_object

    blob = write_object(tmp_path, b"after snapshot")
    event = write_event(conn, kind="remember", origin="agent", payload={"blob_hash": blob})
    conn.execute(
        "INSERT INTO interpretations VALUES (?, ?, ?, 1, '2026-01-01', 'test', '{}')",
        ("interp_late", event.id, event.content_hash),
    )
    conn.execute("UPDATE proposed_ontology_revisions SET status='rejected'")
    conn.commit()
    records = [first, *(json.loads(line) for line in stream)]
    assert [r["id"] for r in records if r["kind"] == "event"] == [first["id"]]
    assert not any(r["kind"] in ("blob", "interpretation") for r in records)
    assert (
        next(r for r in records if r["kind"] == "proposed_ontology_revision")["status"]
        == "proposed"
    )
    assert records[-1]["kind"] == "manifest"
    conn.close()


@pytest.mark.parametrize("failure", ["disconnect", "send_error", "cancel"])
def test_response_closes_snapshot_on_aborted_transport(tmp_path, monkeypatch, failure):
    conn = open_db(tmp_path)
    write_event(conn, kind="remember", origin="agent", payload={"text": "before"})
    conn.close()
    opened = []
    original_open = export_route.open_db_readonly

    def tracked_open(path):
        reader = original_open(path)
        opened.append(reader)
        return reader

    monkeypatch.setattr(export_route, "open_db_readonly", tracked_open)

    async def exercise():
        # 2.0 uses the disconnect listener; 2.4 reports a send exception.
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/internal/export",
            "query_string": b"",
            "headers": [(b"authorization", b"Bearer test-token")],
            "app": _build_app(tmp_path),
            "asgi": {"spec_version": "2.0" if failure == "disconnect" else "2.4"},
        }
        response = await export_route.export_endpoint(Request(scope))
        sent_body = asyncio.Event()

        async def send(message):
            if message["type"] == "http.response.body":
                sent_body.set()
                if failure == "send_error":
                    raise OSError("client gone")
                if failure == "cancel":
                    raise asyncio.CancelledError
                await asyncio.Future()  # Cancel while sending first chunk.

        async def receive():
            await sent_body.wait()
            return {"type": "http.disconnect"}

        try:
            await response(scope, receive, send)
        except (Exception, asyncio.CancelledError):
            if failure == "disconnect":
                raise

    asyncio.run(exercise())
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
    # No abandoned snapshot retains WAL pages after disconnect.
    conn = open_db(tmp_path)
    write_event(conn, kind="remember", origin="agent", payload={"text": "after"})
    conn.execute("PRAGMA busy_timeout=0")
    assert conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    conn.close()


def test_export_missing_vault_does_not_create_database(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(sqlite3.OperationalError):
        list(export_route._iter_export(missing, include_blobs=False))
    assert not missing.exists()


def test_readonly_setup_failure_closes_connection(tmp_path, monkeypatch):
    from afair.substrate import db

    open_db(tmp_path).close()
    opened = []
    original_open = db._open_connection

    def tracked_open(*args, **kwargs):
        conn = original_open(*args, **kwargs)
        opened.append(conn)
        return conn

    def fail_load(conn):
        raise RuntimeError("extension loading failed")

    monkeypatch.setattr(db, "_open_connection", tracked_open)
    monkeypatch.setattr(db.sqlite_vec, "load", fail_load)
    with pytest.raises(RuntimeError, match="extension loading failed"):
        db.open_db_readonly(tmp_path)
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
