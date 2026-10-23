"""File cursors must survive an APFS volume renumber, the way root identity does."""

import os
import sqlite3
from pathlib import Path

from one_memory.imports.access import AuthorizedSourceRoot, _changed_ns
from one_memory.imports.adapters import ScanOptions, SourceCursor
from one_memory.imports.jobs import ImportJobStore
from one_memory.imports.sources.common import is_unchanged, resume_checkpoint


def _grant(tmp_path: Path, name: str = "session.jsonl"):
    root = tmp_path / "source"
    root.mkdir()
    path = root / name
    path.write_text('{"turn":1}\n', encoding="utf-8")
    grant = AuthorizedSourceRoot.grant(
        source_id="fixture",
        root=root,
        user_confirmed=True,
        home=tmp_path / "home",
    )
    return grant.authorize_file(path), path


def _cursor(source, identity: str, *, checkpoint: str = "byte:4") -> SourceCursor:
    return SourceCursor(
        file_key=source.safe_id,
        file_identity=identity,
        checkpoint=checkpoint,
        observed_size=source.size,
        observed_modified_ns=source.modified_ns,
        read_to_end=True,
    )


def test_listed_file_identity_does_not_include_the_device_number(tmp_path: Path) -> None:
    source, path = _grant(tmp_path)
    details = os.stat(path)

    assert source.file_identity == f"ino:{details.st_ino}:{_changed_ns(details)}"
    assert not source.file_identity.startswith(f"{details.st_dev}:")


def test_a_cursor_recorded_before_reboot_still_resumes(tmp_path: Path) -> None:
    source, path = _grant(tmp_path)
    details = os.stat(path)
    previous_boot = f"{details.st_dev + 1}:{details.st_ino}"
    cursor = _cursor(source, previous_boot)

    assert resume_checkpoint(source, ScanOptions(cursors={source.safe_id: cursor})) == "byte:4"
    assert is_unchanged(source, {source.safe_id: cursor}) is True


def test_a_different_inode_still_starts_over(tmp_path: Path) -> None:
    source, path = _grant(tmp_path)
    details = os.stat(path)
    cursor = _cursor(source, f"{details.st_dev}:{details.st_ino + 1}")

    assert resume_checkpoint(source, ScanOptions(cursors={source.safe_id: cursor})) is None
    assert is_unchanged(source, {source.safe_id: cursor}) is False


def test_a_different_birth_does_not_resume(tmp_path: Path) -> None:
    source, path = _grant(tmp_path)
    details = os.stat(path)
    cursor = _cursor(source, f"ino:{details.st_ino}:{_changed_ns(details) + 1}")

    assert resume_checkpoint(source, ScanOptions(cursors={source.safe_id: cursor})) is None


def test_reopening_the_job_store_rewrites_legacy_cursor_identity(tmp_path: Path) -> None:
    source, path = _grant(tmp_path)
    details = os.stat(path)
    database = tmp_path / "jobs.sqlite3"
    ImportJobStore(database)
    legacy = f"{details.st_dev + 3}:{details.st_ino}"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO source_cursors ("
            "user_id, source_id, file_key, file_identity, checkpoint, "
            "observed_size, observed_modified_ns, updated_at, read_to_end"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "local",
                "fixture",
                source.safe_id,
                legacy,
                "byte:4",
                source.size,
                source.modified_ns,
                "2026-10-06T00:00:00+00:00",
                1,
            ),
        )
        connection.execute(
            "INSERT INTO live_source_cursors ("
            "user_id, source_id, file_key, file_identity, checkpoint, "
            "observed_size, observed_modified_ns, updated_at, read_to_end"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "local",
                "fixture",
                source.safe_id,
                "not-a-device-inode",
                "byte:4",
                source.size,
                source.modified_ns,
                "2026-10-06T00:00:00+00:00",
                1,
            ),
        )

    reopened = ImportJobStore(database)
    stored = reopened.get_source_cursors(
        user_id="local", source_id="fixture", operation="history"
    )[source.safe_id]
    live = reopened.get_source_cursors(
        user_id="local", source_id="fixture", operation="sync"
    )[source.safe_id]

    assert stored.file_identity == f"ino:{details.st_ino}"
    assert stored.checkpoint == "byte:4"
    assert live.file_identity == "not-a-device-inode"
    assert (
        resume_checkpoint(source, ScanOptions(cursors={source.safe_id: stored})) == "byte:4"
    )
    again = ImportJobStore(database)
    assert (
        again.get_source_cursors(user_id="local", source_id="fixture", operation="history")[
            source.safe_id
        ].file_identity
        == stored.file_identity
    )


