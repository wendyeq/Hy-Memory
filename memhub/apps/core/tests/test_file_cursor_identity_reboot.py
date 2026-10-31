"""File cursors must survive an APFS volume renumber, the way root identity does."""

import os
import sqlite3
from pathlib import Path

from one_memory.imports.access import (
    AuthorizedSourceRoot,
    _changed_ns,
    cursor_identities_same_file,
)
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
    """A birth time is enough. Linux has no birth time here, so the device stays.

    The old expectation was ``ino:<inode>:<birth>`` on every platform. This
    machine reports no birth time, and ``file_cursor_identity`` then keeps
    ``st_dev`` so an inode recycled on another mount is not the same file.
    """
    source, path = _grant(tmp_path)
    details = os.stat(path)
    birth = _changed_ns(details)
    if birth == 0:
        expected = f"ino:{details.st_ino}:0:{details.st_dev}"
    else:
        expected = f"ino:{details.st_ino}:{birth}"

    assert source.file_identity == expected
    assert not source.file_identity.startswith(f"{details.st_dev}:")


def test_a_cursor_recorded_before_reboot_still_resumes(tmp_path: Path) -> None:
    """APFS renumber keeps the inode and changes ``st_dev``.

    Resume at the old offset. Do not treat that legacy identity as proof the
    file is unchanged: another volume can reuse the inode, and skipping it
    would drop the replacement. Missing a read is acceptable; a permanent
    skip is not.
    """
    source, path = _grant(tmp_path)
    details = os.stat(path)
    previous_boot = f"{details.st_dev + 1}:{details.st_ino}"
    cursor = _cursor(source, previous_boot)

    assert resume_checkpoint(source, ScanOptions(cursors={source.safe_id: cursor})) == "byte:4"
    assert is_unchanged(source, {source.safe_id: cursor}) is False


def test_cross_device_legacy_inode_is_not_skipped_as_unchanged(tmp_path: Path) -> None:
    source, path = _grant(tmp_path)
    details = os.stat(path)
    legacy = _cursor(source, f"{details.st_dev + 9}:{details.st_ino}")
    migrated = _cursor(source, f"ino:{details.st_ino}")
    birthless = _cursor(source, f"ino:{details.st_ino}:0:{details.st_dev + 9}")

    for cursor in (legacy, migrated, birthless):
        assert is_unchanged(source, {source.safe_id: cursor}) is False
        assert resume_checkpoint(source, ScanOptions(cursors={source.safe_id: cursor})) == "byte:4"
    assert cursor_identities_same_file(
        f"ino:{details.st_ino}:0:{details.st_dev}",
        f"ino:{details.st_ino}:0:{details.st_dev + 9}",
    ) is False


def test_a_complete_identity_can_still_skip_an_unchanged_file(tmp_path: Path) -> None:
    source, _path = _grant(tmp_path)
    cursor = _cursor(source, source.file_identity)

    assert is_unchanged(source, {source.safe_id: cursor}) is True
    assert resume_checkpoint(source, ScanOptions(cursors={source.safe_id: cursor})) == "byte:4"


def test_a_different_inode_still_starts_over(tmp_path: Path) -> None:
    source, path = _grant(tmp_path)
    details = os.stat(path)
    cursor = _cursor(source, f"{details.st_dev}:{details.st_ino + 1}")

    assert resume_checkpoint(source, ScanOptions(cursors={source.safe_id: cursor})) is None
    assert is_unchanged(source, {source.safe_id: cursor}) is False


def test_a_different_birth_does_not_resume(tmp_path: Path) -> None:
    """Two real birth times must not share an offset.

    With no birth time on the file, a stored birth is only an inode match.
    Resume may try the old offset. The content anchor still has to match.
    """
    source, path = _grant(tmp_path)
    details = os.stat(path)
    birth = _changed_ns(details)
    cursor = _cursor(source, f"ino:{details.st_ino}:{birth + 1}")
    checkpoint = resume_checkpoint(source, ScanOptions(cursors={source.safe_id: cursor}))

    if birth == 0:
        assert checkpoint == "byte:4"
    else:
        assert checkpoint is None


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


