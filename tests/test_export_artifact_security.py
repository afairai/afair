"""Synthetic encrypted-export crash, retry and publication regressions."""

from __future__ import annotations

import gzip
import json
import os
import stat

import pytest
from cryptography.exceptions import InvalidTag

from afair.mcp import export_job
from afair.substrate import export_jobs, open_db
from afair.substrate.encryption import decrypt_blob, encrypt_blob
from afair.substrate.events import write_event
from tests.test_export_async import _settings


@pytest.fixture
def keyed_vault(tmp_path, monkeypatch):
    conn = open_db(tmp_path)
    write_event(conn, kind="remember", origin="agent", payload={"text": "private memory"})
    conn.close()
    key = os.urandom(32)
    monkeypatch.setattr(export_job, "_blob_key_or_none", lambda: key)
    return tmp_path, key


def test_encryption_precedes_disk_and_publishes_compatible_complete_envelope(
    keyed_vault, monkeypatch
):
    vault, key = keyed_vault
    original = export_job._iter_export
    inspected = []

    def records(*args, **kwargs):
        # A sizable incompressible record forces output beyond Python's disk buffer.
        yield json.dumps({"kind": "synthetic", "text": os.urandom(100_000).hex()}) + "\n"
        files = list(export_job.exports_dir(vault).iterdir())
        assert len(files) == 1
        temp = files[0]
        assert temp.name.endswith(".tmp")
        assert stat.S_IMODE(temp.stat().st_mode) == 0o600
        ciphertext = temp.read_bytes()
        assert ciphertext.startswith(b"AF01")
        assert b"private memory" not in ciphertext
        with pytest.raises((gzip.BadGzipFile, EOFError)):
            gzip.decompress(ciphertext)
        inspected.append(temp)
        yield from original(*args, **kwargs)

    monkeypatch.setattr(export_job, "_iter_export", records)
    filename, size = export_job.generate_artifact(vault, "exp_secure", include_blobs=False)
    artifact = export_job.exports_dir(vault) / filename
    raw = artifact.read_bytes()
    compressed = decrypt_blob(raw, key)  # Existing blob reader accepts streamed AF01.
    assert len(compressed) == size
    assert json.loads(gzip.decompress(compressed).splitlines()[-1])["kind"] == "manifest"
    assert export_job.read_artifact(vault, filename) == compressed
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert inspected and not inspected[0].exists()
    assert list(export_job.exports_dir(vault).iterdir()) == [artifact]


@pytest.mark.parametrize("failure", [RuntimeError("source failed"), KeyboardInterrupt()])
def test_failed_retry_preserves_previous_artifact_and_closes_source(
    keyed_vault, monkeypatch, failure
):
    vault, _ = keyed_vault
    filename, _ = export_job.generate_artifact(vault, "exp_retry", include_blobs=False)
    path = export_job.exports_dir(vault) / filename
    previous = path.read_bytes()
    closed = []

    def broken(*args, **kwargs):
        try:
            yield '{"kind":"event","payload":"private memory"}\n'
            raise failure
        finally:
            closed.append(True)

    monkeypatch.setattr(export_job, "_iter_export", broken)
    with pytest.raises(type(failure)):
        export_job.generate_artifact(vault, "exp_retry", include_blobs=False)
    assert closed == [True]
    assert path.read_bytes() == previous
    assert list(export_job.exports_dir(vault).iterdir()) == [path]


def test_atomic_publication_failure_preserves_prior_success(keyed_vault, monkeypatch):
    from pathlib import Path

    vault, _ = keyed_vault
    filename, _ = export_job.generate_artifact(vault, "exp_retry", include_blobs=False)
    path = export_job.exports_dir(vault) / filename
    previous = path.read_bytes()

    def fail_replace(*args):
        raise OSError("volume publication failed")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="publication failed"):
        export_job.generate_artifact(vault, "exp_retry", include_blobs=False)
    assert path.read_bytes() == previous
    assert list(export_job.exports_dir(vault).iterdir()) == [path]


def test_streamed_artifacts_reject_wrong_key_tamper_and_truncation(keyed_vault, monkeypatch):
    vault, key = keyed_vault
    filename, _ = export_job.generate_artifact(vault, "exp_auth", include_blobs=False)
    path = export_job.exports_dir(vault) / filename
    raw = path.read_bytes()
    monkeypatch.setattr(export_job, "_blob_key_or_none", lambda: os.urandom(32))
    with pytest.raises(InvalidTag):
        export_job.read_artifact(vault, filename)
    monkeypatch.setattr(export_job, "_blob_key_or_none", lambda: key)
    for corrupt in (raw[:-1], raw[:25] + bytes([raw[25] ^ 1]) + raw[26:]):
        path.write_bytes(corrupt)
        with pytest.raises(InvalidTag):
            export_job.read_artifact(vault, filename)


def test_legacy_artifact_remains_readable(keyed_vault):
    vault, key = keyed_vault
    directory = export_job.exports_dir(vault)
    directory.mkdir()
    compressed = gzip.compress(b'{"kind":"manifest"}\n')
    (directory / "legacy.bin").write_bytes(encrypt_blob(compressed, key))
    assert export_job.read_artifact(vault, "legacy.bin") == compressed


def test_ready_job_retry_does_not_regenerate_or_delete_winner(keyed_vault, monkeypatch):
    vault, _ = keyed_vault
    conn = open_db(vault)
    job_id, token = export_jobs.create_job(conn)
    export_job.run_job(_settings(vault), job_id, include_blobs=False, download_token=token)
    job = export_jobs.job_by_id(conn, job_id)
    path = export_job.exports_dir(vault) / job.artifact_filename
    previous = path.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("a ready job must not regenerate")

    monkeypatch.setattr(export_job, "generate_artifact", forbidden)
    export_job.run_job(_settings(vault), job_id, include_blobs=False, download_token=token)
    assert path.read_bytes() == previous
    assert export_jobs.job_by_id(conn, job_id).status == "ready"
    conn.close()


def test_lost_race_deletes_only_its_own_attempt(keyed_vault, monkeypatch):
    vault, _ = keyed_vault
    conn = open_db(vault)
    job_id, token = export_jobs.create_job(conn)
    winner = []
    original = export_job.generate_artifact

    def concurrent_winner(*args, **kwargs):
        filename, size = original(*args, **kwargs)
        export_jobs.mark_ready(conn, job_id, artifact_filename=filename, size_bytes=size)
        winner.append(filename)
        return original(*args, **kwargs)

    monkeypatch.setattr(export_job, "generate_artifact", concurrent_winner)
    export_job.run_job(_settings(vault), job_id, include_blobs=False, download_token=token)
    assert export_jobs.job_by_id(conn, job_id).artifact_filename == winner[0]
    assert [p.name for p in export_job.exports_dir(vault).iterdir()] == winner
    export_job.purge_expired(vault, conn)
    assert [p.name for p in export_job.exports_dir(vault).iterdir()] == winner
    conn.close()


def test_abrupt_worker_exit_leaves_only_private_ciphertext_and_retry_recovers(keyed_vault):
    import subprocess
    import sys

    vault, _ = keyed_vault
    conn = open_db(vault)
    job_id, _ = export_jobs.create_job(conn)
    conn.execute("UPDATE export_jobs SET id='exp_crash' WHERE id=?", (job_id,))
    conn.commit()
    # A fresh process exits without Python finally blocks or buffered-file close,
    # approximating a killed/OOM worker rather than a catchable source error.
    script = r"""
import json, os, sys
from pathlib import Path
from afair.mcp import export_job
export_job._blob_key_or_none = lambda: b'k' * 32

def records(*args, **kwargs):
    yield json.dumps({"kind":"event", "text": os.urandom(200_000).hex()}) + "\n"
    os._exit(73)

export_job._iter_export = records
export_job.generate_artifact(Path(sys.argv[1]), "exp_crash", include_blobs=False)
"""
    result = subprocess.run([sys.executable, "-c", script, str(vault)], check=False)
    assert result.returncode == 73
    leftovers = list(export_job.exports_dir(vault).iterdir())
    assert len(leftovers) == 1 and leftovers[0].name.endswith(".tmp")
    crashed = leftovers[0]
    assert crashed.stat().st_size > 10_000
    assert stat.S_IMODE(crashed.stat().st_mode) == 0o600
    assert crashed.read_bytes().startswith(b"AF01")
    with pytest.raises(InvalidTag):
        decrypt_blob(crashed.read_bytes(), b"k" * 32)  # Crash => no final GCM tag.
    filename, size = export_job.generate_artifact(vault, "exp_crash", include_blobs=False)
    export_jobs.mark_ready(conn, "exp_crash", artifact_filename=filename, size_bytes=size)
    assert (
        json.loads(gzip.decompress(export_job.read_artifact(vault, filename)).splitlines()[-1])[
            "kind"
        ]
        == "manifest"
    )
    # The existing age-bound janitor removes only the dead attempt's ciphertext.
    os.utime(crashed, (0, 0))
    export_job._reconcile_orphans(vault, conn)
    assert not crashed.exists()
    assert (export_job.exports_dir(vault) / filename).exists()
    conn.close()


def test_fsync_failure_never_publishes_partial_artifact(keyed_vault, monkeypatch):
    vault, _ = keyed_vault

    def fail_fsync(fd):
        raise OSError("disk flush failed")

    monkeypatch.setattr(export_job.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="disk flush failed"):
        export_job.generate_artifact(vault, "exp_flush", include_blobs=False)
    assert list(export_job.exports_dir(vault).iterdir()) == []


def test_invalid_encryption_key_fails_before_creating_files(keyed_vault, monkeypatch):
    vault, _ = keyed_vault
    monkeypatch.setattr(export_job, "_blob_key_or_none", lambda: b"bad-key")
    with pytest.raises(ValueError, match="key size"):
        export_job.generate_artifact(vault, "exp_key", include_blobs=False)
    assert not export_job.exports_dir(vault).exists()


@pytest.mark.parametrize("job_id", ["../outside", "nested/file", "", "."])
def test_job_id_cannot_escape_export_directory(keyed_vault, job_id):
    vault, _ = keyed_vault
    with pytest.raises(ValueError, match="invalid export job id"):
        export_job.generate_artifact(vault, job_id, include_blobs=False)
    assert not export_job.exports_dir(vault).exists()
