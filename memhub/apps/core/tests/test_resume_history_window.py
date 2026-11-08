"""Startup and sync resume must not open or continue a history import."""

import asyncio
import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from one_memory.api import create_app, resume_history_after
from one_memory.config import Settings
from one_memory.imports.access import file_cursor_identity
from one_memory.imports.grants import SourceGrantStore
from one_memory.imports.jobs import ImportJobStore, ImportStatus
from one_memory.imports.live_sync import PollingLiveSyncSupervisor
from one_memory.imports.sources.registry import RootCandidateKind, build_source_catalog
from one_memory.imports.supervisor import ImportSupervisor
from one_memory.secrets import InMemorySecretStore
from tests.source_consent import authorized_request

_OLD = "restored-30-day-history"
_TODAY = "today-window"


def _settings(tmp_path):
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        source_home=tmp_path / "home",
        api_token=SecretStr("test"),
        imports_enabled=True,
        automatic_memory_processing=False,
        live_sync_enabled=False,
        live_sync_watch_enabled=False,
        live_sync_interval_seconds=3600,
    )


def _assert_one_day(history_after: datetime | None, *, started: datetime) -> None:
    assert history_after is not None
    if history_after.tzinfo is None:
        history_after = history_after.replace(tzinfo=UTC)
    delta = started - history_after.astimezone(UTC)
    assert timedelta(hours=23) < delta < timedelta(hours=25)


async def _finish_recovery(app) -> None:
    task = app.state.recovery_task
    if task is not None:
        await task
    assert app.state.recovery == "done"
    assert app.state.recovery_failed_steps == ()


def test_core_startup_does_not_create_a_history_job(tmp_path, monkeypatch) -> None:
    """Startup recovery used to open an automatic history job. It no longer does."""
    monkeypatch.setattr("one_memory.model_presets._preset_data", lambda: None)
    home = tmp_path / "home"
    root = home / ".claude" / "projects"
    root.mkdir(parents=True)
    (root / "session.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    SourceGrantStore(data / "one-memory.sqlite3", home=home).grant(
        user_id="local",
        source_id="claude-code",
        root=root,
        candidate_kind=RootCandidateKind.DEFAULT,
    )
    (data / "config.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sources": {"claude-code": {"sync_enabled": True}},
                "sedimentation_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    app = create_app(
        _settings(tmp_path),
        source_catalog=build_source_catalog(home=home, environment={}),
        secret_store=InMemorySecretStore(),
    )

    async def scenario():
        async with app.router.lifespan_context(app):
            await _finish_recovery(app)
            assert [
                job.operation
                for job in app.state.import_job_store.list_jobs()
                if job.source_id == "claude-code"
            ] == []

    asyncio.run(scenario())


def test_sync_permission_resume_does_not_create_a_history_job(tmp_path, monkeypatch) -> None:
    """Enabling sync anchors cursors and does not open history.

    A later sync request still opens a sync job. A history job still opens
    when the caller asks for one.
    """
    monkeypatch.setattr("one_memory.model_presets._preset_data", lambda: None)
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    app = create_app(
        _settings(tmp_path),
        source_catalog=build_source_catalog(home=home, environment={}),
        secret_store=InMemorySecretStore(),
    )

    async def scenario():
        async with (
            app.router.lifespan_context(app),
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": "Bearer test"},
            ) as client,
        ):
            await _finish_recovery(app)
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
            assert [
                job.operation
                for job in app.state.import_job_store.list_jobs()
                if job.source_id == "claude-code" and job.operation == "history"
            ] == []
            synced = await client.post("/api/v1/sources/claude-code/sync")
            assert synced.status_code == 200
            assert synced.json()["operation"] == "sync"
            manual = await client.post(
                "/api/v1/import-jobs",
                json={"source_id": "claude-code", "operation": "history"},
            )
            assert manual.status_code == 200
            assert manual.json()["operation"] == "history"
            history = [
                job
                for job in app.state.import_job_store.list_jobs()
                if job.source_id == "claude-code" and job.operation == "history"
            ]
            assert [job.job_id for job in history] == [manual.json()["job_id"]]

    asyncio.run(scenario())


def _session(home, *, old: str, today: str) -> None:
    root = home / ".claude" / "projects"
    root.mkdir(parents=True)
    old_at = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    today_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def line(uuid: str, text: str, when: str) -> str:
        return json.dumps(
            {
                "type": "user",
                "sessionId": "session",
                "uuid": uuid,
                "timestamp": when,
                "message": {"content": [{"type": "text", "text": text}]},
            }
        )

    (root / "session.jsonl").write_text(
        line("old", old, old_at) + "\n" + line("today", today, today_at) + "\n",
        encoding="utf-8",
    )


def _grant_claude(tmp_path, home) -> None:
    data = tmp_path / "data"
    data.mkdir()
    SourceGrantStore(data / "one-memory.sqlite3", home=home).grant(
        user_id="local",
        source_id="claude-code",
        root=home / ".claude" / "projects",
        candidate_kind=RootCandidateKind.DEFAULT,
    )


def _preset_history(tmp_path, *, status: str, intent: str | None) -> None:
    database = tmp_path / "data" / "one-memory.sqlite3"
    ImportJobStore(database)
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO import_jobs (
                job_id, user_id, source_id, status, created_at, updated_at,
                operation, history_after, history_intent
            ) VALUES (?, ?, ?, ?, ?, ?, 'history', NULL, ?)
            """,
            ("preset-history", "local", "claude-code", status, now, now, intent),
        )


def _app(tmp_path, home):
    return create_app(
        _settings(tmp_path),
        source_catalog=build_source_catalog(home=home, environment={}),
        secret_store=InMemorySecretStore(),
    )


def _texts(app) -> list[str]:
    return [record.verbatim for record in app.state.evidence_store.list_extractable()]


def test_startup_bounds_a_windowless_pending_history(tmp_path, monkeypatch) -> None:
    """An unfinished automatic catch-up is tightened and not started."""
    monkeypatch.setattr("one_memory.model_presets._preset_data", lambda: None)
    home = tmp_path / "home"
    _session(home, old=_OLD, today=_TODAY)
    _grant_claude(tmp_path, home)
    _preset_history(tmp_path, status="pending", intent=None)
    (tmp_path / "data" / "config.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sources": {"claude-code": {"sync_enabled": True}},
                "sedimentation_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    app = _app(tmp_path, home)

    async def scenario():
        async with app.router.lifespan_context(app):
            await _finish_recovery(app)
            job = next(
                job
                for job in app.state.import_job_store.list_jobs()
                if job.job_id == "preset-history"
            )
            _assert_one_day(job.history_after, started=datetime.now(UTC))
            assert job.status is ImportStatus.PENDING
            assert job.stored == 0
            assert _texts(app) == []
            await app.state.import_supervisor.resume_off_loop(job.job_id)
            finished = await asyncio.wait_for(app.state.import_supervisor.wait(job.job_id), 30)
            assert finished.stored == 1
            assert _OLD not in _texts(app)
            assert _TODAY in _texts(app)

    asyncio.run(scenario())


def test_reenabling_sync_bounds_a_windowless_paused_history(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("one_memory.model_presets._preset_data", lambda: None)
    home = tmp_path / "home"
    _session(home, old=_OLD, today=_TODAY)
    _grant_claude(tmp_path, home)
    _preset_history(tmp_path, status="paused", intent=None)
    (tmp_path / "data" / "config.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sources": {"claude-code": {"sync_enabled": False}},
                "sedimentation_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    app = _app(tmp_path, home)

    async def scenario():
        async with (
            app.router.lifespan_context(app),
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": "Bearer test"},
            ) as client,
        ):
            enabled = await client.post(
                "/api/v1/sources/claude-code/sync-permission",
                json={"enabled": True},
            )
            assert enabled.status_code == 200
            job = app.state.import_job_store.require("preset-history")
            _assert_one_day(job.history_after, started=datetime.now(UTC))
            assert job.status is ImportStatus.PAUSED
            assert job.stored == 0
            assert _texts(app) == []

    asyncio.run(scenario())


def test_startup_keeps_an_explicit_full_import(tmp_path, monkeypatch) -> None:
    """Explicit history keeps its window and is not started on startup."""
    monkeypatch.setattr("one_memory.model_presets._preset_data", lambda: None)
    home = tmp_path / "home"
    _session(home, old=_OLD, today=_TODAY)
    _grant_claude(tmp_path, home)
    _preset_history(tmp_path, status="pending", intent="explicit")
    (tmp_path / "data" / "config.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sources": {"claude-code": {"sync_enabled": True}},
                "sedimentation_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    app = _app(tmp_path, home)

    async def scenario():
        async with app.router.lifespan_context(app):
            await _finish_recovery(app)
            job = app.state.import_job_store.require("preset-history")
            assert job.history_after is None
            assert job.status is ImportStatus.PENDING
            assert job.stored == 0
            assert _texts(app) == []

    asyncio.run(scenario())


def test_automatic_reuse_does_not_narrow_an_explicit_full_import(tmp_path) -> None:
    store = ImportJobStore(tmp_path / "jobs.sqlite3")
    explicit = store.create(user_id="local", source_id="claude-code", history_intent="explicit")
    reused = store.create(
        user_id="local",
        source_id="claude-code",
        history_after=resume_history_after(),
        history_intent="automatic",
    )

    assert reused.job_id == explicit.job_id
    assert reused.history_after is None


def test_automatic_reuse_bounds_a_windowless_catchup(tmp_path) -> None:
    store = ImportJobStore(tmp_path / "jobs.sqlite3")
    store.create(user_id="local", source_id="claude-code")
    reused = store.create(
        user_id="local",
        source_id="claude-code",
        history_after=resume_history_after(),
        history_intent="automatic",
    )

    assert reused.history_after is not None
    _assert_one_day(reused.history_after, started=datetime.now(UTC))


def test_automatic_reuse_bounds_a_cutoff_older_than_one_day(tmp_path) -> None:
    store = ImportJobStore(tmp_path / "jobs.sqlite3")
    store.create(
        user_id="local",
        source_id="claude-code",
        history_after=datetime.now(UTC) - timedelta(days=30),
        history_intent="automatic",
    )
    reused = store.create(
        user_id="local",
        source_id="claude-code",
        history_after=resume_history_after(),
        history_intent="automatic",
    )

    _assert_one_day(reused.history_after, started=datetime.now(UTC))


_NEW = "appended-after-cursor"
_HISTORY_CHECKPOINT = "byte:7"


def _claude_line(uuid: str, text: str, when: datetime) -> bytes:
    return (
        json.dumps(
            {
                "type": "user",
                "sessionId": "session",
                "uuid": uuid,
                "timestamp": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "message": {"content": [{"type": "text", "text": text}]},
            },
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _insert_job(
    database,
    *,
    job_id: str,
    operation: str,
    status: str,
    intent: str | None,
    heartbeat_at: str | None = None,
    checkpoint: str | None = _HISTORY_CHECKPOINT,
) -> None:
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO import_jobs (
                job_id, user_id, source_id, status, heartbeat_at, checkpoint,
                created_at, updated_at, operation, history_after, history_intent
            ) VALUES (?, 'local', 'claude-code', ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (job_id, status, heartbeat_at, checkpoint, now, now, operation, intent),
        )


def _plant_anchored_append(home, database) -> None:
    """A live cursor at the old line, then one new line after it."""
    root = home / ".claude" / "projects"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "session.jsonl"
    old = _claude_line("old", _OLD, datetime.now(UTC) - timedelta(days=30))
    path.write_bytes(old)
    details = os.lstat(path)
    file_key = hashlib.sha256(b"claude-code\0session.jsonl").hexdigest()
    identity = file_cursor_identity(details)
    anchor = hashlib.sha256(old).hexdigest()
    now = datetime.now(UTC).isoformat()
    with path.open("ab") as handle:
        handle.write(_claude_line("new", _NEW, datetime.now(UTC)))
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO live_source_cursors (
                user_id, source_id, file_key, file_identity, checkpoint,
                checkpoint_anchor, observed_size, observed_modified_ns,
                updated_at, read_to_end
            ) VALUES ('local', 'claude-code', ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                file_key,
                identity,
                f"byte:{details.st_size}",
                anchor,
                details.st_size,
                details.st_mtime_ns,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO source_cursors (
                user_id, source_id, file_key, file_identity, checkpoint,
                checkpoint_anchor, observed_size, observed_modified_ns,
                updated_at, read_to_end
            ) VALUES ('local', 'claude-code', ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                file_key,
                identity,
                _HISTORY_CHECKPOINT,
                anchor,
                details.st_size,
                details.st_mtime_ns,
                now,
            ),
        )


def test_cold_start_resumes_sync_and_leaves_history(tmp_path, monkeypatch) -> None:
    """Each unfinished history status stays put while an interrupted sync resumes.

    One source can hold only one unfinished history job, so each status and
    both intents get their own cold start. A stale running row is interrupted
    by the existing heartbeat recovery and still is not queued.
    """
    monkeypatch.setattr("one_memory.model_presets._preset_data", lambda: None)
    stale = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    cases = (
        ("pending", None, None),
        ("pending", "explicit", None),
        ("paused", None, None),
        ("paused", "explicit", None),
        ("interrupted", None, None),
        ("interrupted", "explicit", None),
        ("failed", None, None),
        ("failed", "explicit", None),
        ("running", None, stale),
        ("running", "explicit", stale),
        ("running", None, None),
        ("running", "explicit", None),
    )
    for status, intent, heartbeat in cases:
        root = tmp_path / f"{status}-{intent or 'null'}-{heartbeat is not None}"
        _assert_cold_start_skips_history(
            root,
            status=status,
            intent=intent,
            heartbeat_at=heartbeat,
        )


def _assert_cold_start_skips_history(
    tmp_path,
    *,
    status: str,
    intent: str | None,
    heartbeat_at: str | None,
    sync_status: str = "interrupted",
) -> None:
    tmp_path.mkdir(parents=True)
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    _grant_claude(tmp_path, home)
    database = tmp_path / "data" / "one-memory.sqlite3"
    ImportJobStore(database)
    _plant_anchored_append(home, database)
    _insert_job(
        database,
        job_id="preset-history",
        operation="history",
        status=status,
        intent=intent,
        heartbeat_at=heartbeat_at,
    )
    _insert_job(
        database,
        job_id="preset-sync",
        operation="sync",
        status=sync_status,
        intent=None,
        checkpoint=None,
    )
    (tmp_path / "data" / "config.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sources": {"claude-code": {"sync_enabled": True}},
                "sedimentation_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    app = _app(tmp_path, home)

    async def scenario():
        async with app.router.lifespan_context(app):
            await _finish_recovery(app)
            finished = await asyncio.wait_for(app.state.import_supervisor.wait("preset-sync"), 30)
            assert finished.status is ImportStatus.COMPLETED
            assert finished.stored == 1
            assert _texts(app) == [_NEW]
            job = app.state.import_job_store.require("preset-history")
            if status == "running" and heartbeat_at is None:
                expected = ImportStatus.RUNNING
            elif status == "running":
                expected = ImportStatus.INTERRUPTED
            else:
                expected = ImportStatus(status)
            assert job.status is expected
            assert job.stored == 0
            assert job.checkpoint == _HISTORY_CHECKPOINT
            assert job.job_id not in app.state.import_supervisor.active_job_ids()
            if intent is None:
                _assert_one_day(job.history_after, started=datetime.now(UTC))
            else:
                assert job.history_after is None
            with sqlite3.connect(database) as connection:
                stored_intent = connection.execute(
                    "SELECT history_intent FROM import_jobs WHERE job_id = 'preset-history'"
                ).fetchone()[0]
                history_cursor = connection.execute(
                    "SELECT checkpoint FROM source_cursors"
                ).fetchone()[0]
            # Null intent is the pre-intent catch-up. The existing startup
            # tighten writes `automatic` when it bounds that window. Explicit
            # stays explicit, and neither row is queued.
            assert stored_intent == ("automatic" if intent is None else intent)
            assert history_cursor == _HISTORY_CHECKPOINT

    asyncio.run(scenario())


def test_paused_and_failed_sync_still_resume(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("one_memory.model_presets._preset_data", lambda: None)
    for index in range(8):
        for sync_status in ("paused", "failed"):
            _assert_cold_start_skips_history(
                tmp_path / f"{index}-{sync_status}",
                status="pending",
                intent="explicit",
                heartbeat_at=None,
                sync_status=sync_status,
            )


def test_reenabling_sync_does_not_resume_history(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("one_memory.model_presets._preset_data", lambda: None)
    home = tmp_path / "home"
    _session(home, old=_OLD, today=_TODAY)
    _grant_claude(tmp_path, home)
    database = tmp_path / "data" / "one-memory.sqlite3"
    ImportJobStore(database)
    _insert_job(
        database,
        job_id="history-paused-explicit",
        operation="history",
        status="paused",
        intent="explicit",
    )
    (tmp_path / "data" / "config.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sources": {"claude-code": {"sync_enabled": False}},
                "sedimentation_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    app = _app(tmp_path, home)

    async def scenario():
        async with (
            app.router.lifespan_context(app),
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": "Bearer test"},
            ) as client,
        ):
            await _finish_recovery(app)
            enabled = await client.post(
                "/api/v1/sources/claude-code/sync-permission",
                json={"enabled": True},
            )
            assert enabled.status_code == 200
            paused = app.state.import_job_store.require("history-paused-explicit")
            assert paused.status is ImportStatus.PAUSED
            assert paused.history_after is None
            assert paused.stored == 0
            assert paused.checkpoint == _HISTORY_CHECKPOINT
            assert _OLD not in _texts(app)
            assert _TODAY not in _texts(app)

    asyncio.run(scenario())


def test_manual_resume_still_runs_a_preset_history(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("one_memory.model_presets._preset_data", lambda: None)
    home = tmp_path / "home"
    _session(home, old=_OLD, today=_TODAY)
    _grant_claude(tmp_path, home)
    _preset_history(tmp_path, status="interrupted", intent="explicit")
    (tmp_path / "data" / "config.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sources": {"claude-code": {"sync_enabled": True}},
                "sedimentation_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    app = _app(tmp_path, home)

    async def scenario():
        async with (
            app.router.lifespan_context(app),
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": "Bearer test"},
            ) as client,
        ):
            await _finish_recovery(app)
            parked = app.state.import_job_store.require("preset-history")
            assert parked.status is ImportStatus.INTERRUPTED
            assert parked.history_after is None
            assert parked.stored == 0
            assert _texts(app) == []
            resumed = await client.post("/api/v1/import-jobs/preset-history/resume")
            assert resumed.status_code == 200
            finished = await asyncio.wait_for(
                app.state.import_supervisor.wait("preset-history"), 30
            )
            assert finished.stored == 2
            assert _OLD in _texts(app)
            assert _TODAY in _texts(app)

    asyncio.run(scenario())


def test_startup_cursor_rebase_failure_does_not_resume(tmp_path, monkeypatch) -> None:
    """A thrown cursor rebase during startup must not call import resume.

    The recovery step runner records the failure and continues. Resume reads
    the stored live cursor, so it has to stay uncalled when the rebase did not
    finish.
    """
    monkeypatch.setattr("one_memory.model_presets._preset_data", lambda: None)
    home = tmp_path / "home"
    root = home / ".claude" / "projects"
    root.mkdir(parents=True)
    (root / "session.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    SourceGrantStore(data / "one-memory.sqlite3", home=home).grant(
        user_id="local",
        source_id="claude-code",
        root=root,
        candidate_kind=RootCandidateKind.DEFAULT,
    )
    (data / "config.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sources": {"claude-code": {"sync_enabled": True}},
                "sedimentation_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    calls = {"initialize": 0, "resume": 0}

    async def fail_initialize(*_args, **_kwargs):
        calls["initialize"] += 1
        raise RuntimeError("synthetic cursor rebase failed")

    async def record_resume(*_args, **_kwargs):
        calls["resume"] += 1

    # Both objects are built inside the lifespan, after create_app returns.
    monkeypatch.setattr(PollingLiveSyncSupervisor, "initialize_source", fail_initialize)
    monkeypatch.setattr(ImportSupervisor, "resume_interrupted_off_loop", record_resume)
    app = create_app(
        _settings(tmp_path),
        source_catalog=build_source_catalog(home=home, environment={}),
        secret_store=InMemorySecretStore(),
    )

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            await app.state.recovery_task

    asyncio.run(scenario())

    assert calls["initialize"] >= 1
    assert calls["resume"] == 0
    assert "anchor_live_cursors" in app.state.recovery_failed_steps
    assert "import_resume" in app.state.recovery_failed_steps
    assert [
        job for job in app.state.import_job_store.list_jobs() if job.operation == "history"
    ] == []
