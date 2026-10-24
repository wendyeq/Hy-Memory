"""Startup must not reread a grown file from byte 0.

A compatible cursor whose anchor no longer matches, on a file that grew, is
not a legal append. The realtime lane rebases to the current tail. History
owns the rebuild. These tests drive that rule through the paths that used to
skip it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from one_memory.api import create_app
from one_memory.config import Settings
from one_memory.imports.access import AuthorizedSourceRoot
from one_memory.imports.jobs import ImportJobStore, ImportStatus
from one_memory.imports.live_sync import PollingLiveSyncSupervisor
from one_memory.imports.sources.claude_code import create_claude_code_adapter
from one_memory.imports.sources.cursor_unified import CursorUnifiedSourceAdapter
from one_memory.imports.sources.registry import build_source_catalog
from one_memory.imports.storage import EvidenceStore
from one_memory.imports.supervisor import ImportSupervisor
from tests.source_consent import authorized_request

_POISON = "startup-backfill-should-not-import"


def _claude_line(uuid: str, text: str, when: str) -> bytes:
    return (
        json.dumps(
            {
                "type": "user",
                "sessionId": "session",
                "uuid": uuid,
                "timestamp": when,
                "message": {"content": [{"type": "text", "text": text}]},
            },
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _cursor_line(text: str) -> bytes:
    return (
        json.dumps(
            {"role": "user", "message": {"content": [{"type": "text", "text": text}]}},
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _rewrite(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_RDWR)
    try:
        os.write(descriptor, payload)
        os.ftruncate(descriptor, len(payload))
    finally:
        os.close(descriptor)


def _old_stamp() -> str:
    return (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _texts(evidence: EvidenceStore) -> list[str]:
    return [record.verbatim for record in evidence.list_extractable()]


def _insert_cursor(
    database: Path,
    *,
    table: str,
    source_id: str,
    file_key: str,
    file_identity: str,
    checkpoint: str,
    anchor: str,
    observed_size: int,
    observed_modified_ns: int,
) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            f"""
            INSERT INTO {table} (
                user_id, source_id, file_key, file_identity, checkpoint,
                checkpoint_anchor, observed_size, observed_modified_ns, updated_at, read_to_end
            ) VALUES ('local', ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                source_id,
                file_key,
                file_identity,
                checkpoint,
                anchor,
                observed_size,
                observed_modified_ns,
                "2026-10-06T00:00:00+00:00",
            ),
        )


def _harness(tmp_path: Path, source_id: str, adapter):
    database = tmp_path / "one-memory.sqlite3"
    evidence = EvidenceStore(database)
    jobs = ImportJobStore(database)
    imports = ImportSupervisor(
        adapters={source_id: adapter},
        evidence_store=evidence,
        job_store=jobs,
    )
    live = PollingLiveSyncSupervisor(
        source_ids=(source_id,),
        import_supervisor=imports,
        job_store=jobs,
    )
    return database, evidence, jobs, imports, live


def test_restart_with_live_sync_and_watch_does_not_reread_grown_file_from_zero(
    tmp_path: Path, monkeypatch
) -> None:
    """Lifespan starts live sync and the watcher. The first sync must not import history."""

    monkeypatch.setattr("one_memory.model_presets._preset_data", lambda: None)

    async def scenario() -> None:
        home = tmp_path / "home"
        root = home / ".claude" / "projects"
        root.mkdir(parents=True)
        path = root / "session.jsonl"
        original = _claude_line("known", "known-turn", _old_stamp())
        path.write_bytes(original)
        inode = path.stat().st_ino
        settings = Settings(
            data_dir=tmp_path / "data",
            source_home=home,
            api_token=SecretStr("test"),
            automatic_memory_processing=False,
            live_sync_interval_seconds=3600,
            live_sync_watch_debounce_seconds=0.05,
            live_sync_watch_min_interval_seconds=0.05,
        )
        catalog = build_source_catalog(home=home, environment={})
        app = create_app(settings, source_catalog=catalog)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": "Bearer test"},
            ) as client,
        ):
            authorized = await client.post(
                "/api/v1/sources/claude-code/authorize",
                json=await authorized_request(client, "claude-code"),
            )
            assert authorized.status_code == 200
            enabled = await client.post(
                "/api/v1/sources/claude-code/sync-permission",
                json={"enabled": True},
            )
            assert enabled.status_code == 200
            assert app.state.live_sync_watch is not None
            assert "claude-code" in app.state.live_sync_watch.watching
            for job in app.state.import_job_store.list_jobs():
                await app.state.import_supervisor.wait(job.job_id)
            assert _texts(app.state.evidence_store) == []
            cursors = app.state.import_job_store.get_source_cursors(
                user_id="local", source_id="claude-code", operation="sync"
            )
            assert len(cursors) == 1
            assert next(iter(cursors.values())).checkpoint == f"byte:{len(original)}"

        rewritten = _claude_line("known", "known-turn-rewritten", _old_stamp()) + _claude_line(
            "old", _POISON, _old_stamp()
        )
        assert len(rewritten) - len(original) < 256 * 1024
        _rewrite(path, rewritten)
        assert path.stat().st_ino == inode

        from one_memory.imports.live_sync import PollingLiveSyncSupervisor
        from one_memory.imports.live_watch import FileWatchLiveSync

        # Lifespan used to queue the watcher before rebasing. Finish that
        # prime, if it was already queued, before initialize moves the cursor.
        primed: list[FileWatchLiveSync] = []
        real_start = FileWatchLiveSync.start
        real_initialize = PollingLiveSyncSupervisor.initialize_source

        def start(self, *, prime: bool = False) -> None:
            real_start(self, prime=prime)
            if prime:
                primed.append(self)

        async def initialize(self, source_id: str, *, baseline_missing_files: bool = True) -> None:
            if (
                source_id == "claude-code"
                and primed
                and not getattr(initialize, "finished_prime", False)
            ):
                initialize.finished_prime = True
                workers = [worker for worker in primed[-1]._workers.values() if not worker.done()]
                if workers:
                    await asyncio.gather(*workers)
            await real_initialize(self, source_id, baseline_missing_files=baseline_missing_files)

        monkeypatch.setattr(FileWatchLiveSync, "start", start)
        monkeypatch.setattr(PollingLiveSyncSupervisor, "initialize_source", initialize)
        restored = create_app(settings, source_catalog=catalog)
        async with restored.router.lifespan_context(restored):
            watch = restored.state.live_sync_watch
            assert watch is not None and watch.watching == ("claude-code",)
            pending = [worker for worker in watch._workers.values() if not worker.done()]
            if pending:
                await asyncio.gather(*pending)
            for job in restored.state.import_job_store.list_jobs():
                await restored.state.import_supervisor.wait(job.job_id)
            assert _POISON not in _texts(restored.state.evidence_store)
            assert "known-turn-rewritten" not in _texts(restored.state.evidence_store)

    asyncio.run(scenario())


def test_cursor_live_baseline_carries_files_so_a_bad_anchor_does_not_reread(
    tmp_path: Path,
) -> None:
    """Cursor JSONL baselines must expose the listed files to the live-cursor guard."""

    async def scenario() -> None:
        root = tmp_path / ".cursor"
        path = root / "projects" / "project" / "agent-transcripts" / "session" / "session.jsonl"
        path.parent.mkdir(parents=True)
        original = _cursor_line("known-turn")
        path.write_bytes(original)
        grant = AuthorizedSourceRoot.grant(
            source_id="cursor",
            root=root,
            user_confirmed=True,
            home=tmp_path / "home",
        )
        source = grant.authorize_file(path)
        adapter = CursorUnifiedSourceAdapter(grant=grant)
        database, evidence, jobs, imports, live = _harness(tmp_path, "cursor", adapter)
        _insert_cursor(
            database,
            table="live_source_cursors",
            source_id="cursor",
            file_key=source.safe_id,
            file_identity=source.file_identity,
            checkpoint=f"byte:{len(original)}",
            anchor=hashlib.sha256(original).hexdigest(),
            observed_size=len(original),
            observed_modified_ns=source.modified_ns,
        )
        rewritten = _cursor_line("known-turn-rewritten") + _cursor_line(_POISON)
        assert len(rewritten) - len(original) < 256 * 1024
        inode = path.stat().st_ino
        _rewrite(path, rewritten)
        assert path.stat().st_ino == inode
        try:
            await live.initialize_source("cursor")
            cursor = jobs.get_source_cursors(user_id="local", source_id="cursor", operation="sync")[
                source.safe_id
            ]
            assert cursor.checkpoint == f"byte:{path.stat().st_size}"
            synced = await live.trigger("cursor")
            assert _POISON not in _texts(evidence)
            assert synced is None or synced.stored == 0
        finally:
            await live.shutdown()
            await imports.shutdown()

    asyncio.run(scenario())


def test_empty_live_table_does_not_keep_a_history_cursor_that_reads_from_zero(
    tmp_path: Path,
) -> None:
    """No live row and no active history job must not seed the realtime lane from a dead anchor."""

    async def scenario() -> None:
        root = tmp_path / "projects"
        root.mkdir()
        path = root / "session.jsonl"
        original = _claude_line("known", "known-turn", _old_stamp())
        path.write_bytes(original)
        grant = AuthorizedSourceRoot.grant(
            source_id="claude-code",
            root=root,
            user_confirmed=True,
            home=tmp_path / "home",
        )
        source = grant.authorize_file(path)
        adapter = create_claude_code_adapter(grant)
        database, evidence, jobs, imports, live = _harness(tmp_path, "claude-code", adapter)
        _insert_cursor(
            database,
            table="source_cursors",
            source_id="claude-code",
            file_key=source.safe_id,
            file_identity=source.file_identity,
            checkpoint=f"byte:{len(original)}",
            anchor=hashlib.sha256(original).hexdigest(),
            observed_size=len(original),
            observed_modified_ns=source.modified_ns,
        )
        rewritten = _claude_line("known", "known-turn-rewritten", _old_stamp()) + _claude_line(
            "old", _POISON, _old_stamp()
        )
        assert len(rewritten) - len(original) < 256 * 1024
        inode = path.stat().st_ino
        _rewrite(path, rewritten)
        assert path.stat().st_ino == inode
        assert (
            jobs.get_source_cursors(user_id="local", source_id="claude-code", operation="sync")
            == {}
        )
        assert not any(job.operation == "history" for job in jobs.list_jobs())
        try:
            await live.initialize_source("claude-code")
            cursor = jobs.get_source_cursors(
                user_id="local", source_id="claude-code", operation="sync"
            )[source.safe_id]
            assert cursor.checkpoint == f"byte:{path.stat().st_size}"
            synced = await live.trigger("claude-code")
            assert _POISON not in _texts(evidence)
            assert synced is None or synced.stored == 0
        finally:
            await live.shutdown()
            await imports.shutdown()

    asyncio.run(scenario())


def test_empty_live_table_keeps_a_history_cursor_when_the_anchor_still_holds(
    tmp_path: Path,
) -> None:
    """A legal offline append still resumes from the history cursor when live is empty."""

    async def scenario() -> None:
        root = tmp_path / "projects"
        root.mkdir()
        path = root / "session.jsonl"
        original = _claude_line("known", "known-turn", _old_stamp())
        path.write_bytes(original)
        grant = AuthorizedSourceRoot.grant(
            source_id="claude-code",
            root=root,
            user_confirmed=True,
            home=tmp_path / "home",
        )
        source = grant.authorize_file(path)
        adapter = create_claude_code_adapter(grant)
        database, evidence, jobs, imports, live = _harness(tmp_path, "claude-code", adapter)
        _insert_cursor(
            database,
            table="source_cursors",
            source_id="claude-code",
            file_key=source.safe_id,
            file_identity=source.file_identity,
            checkpoint=f"byte:{len(original)}",
            anchor=hashlib.sha256(original).hexdigest(),
            observed_size=len(original),
            observed_modified_ns=path.stat().st_mtime_ns,
        )
        today = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with path.open("ab") as handle:
            handle.write(_claude_line("new", "offline-append", today))
        try:
            await live.initialize_source("claude-code")
            cursor = jobs.get_source_cursors(
                user_id="local", source_id="claude-code", operation="sync"
            )[source.safe_id]
            assert cursor.checkpoint == f"byte:{len(original)}"
            synced = await live.trigger("claude-code")
            assert synced is not None and synced.status is ImportStatus.COMPLETED
            assert synced.stored == 1
            assert _texts(evidence) == ["offline-append"]
        finally:
            await live.shutdown()
            await imports.shutdown()

    asyncio.run(scenario())
