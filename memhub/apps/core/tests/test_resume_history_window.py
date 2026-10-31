"""Startup and sync resume must not open a history import."""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from one_memory.api import create_app, resume_history_after
from one_memory.config import Settings
from one_memory.imports.grants import SourceGrantStore
from one_memory.imports.jobs import ImportJobStore
from one_memory.imports.sources.registry import RootCandidateKind, build_source_catalog
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
    """An unfinished automatic catch-up must not import the whole old file."""
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
            finished = await asyncio.wait_for(app.state.import_supervisor.wait(job.job_id), 30)
            assert finished.stored == 1
            assert _OLD not in _texts(app)
            assert _TODAY in _texts(app)

    asyncio.run(scenario())


def test_startup_keeps_an_explicit_full_import(tmp_path, monkeypatch) -> None:
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
            finished = await asyncio.wait_for(app.state.import_supervisor.wait(job.job_id), 30)
            assert finished.stored == 2
            assert _OLD in _texts(app)
            assert _TODAY in _texts(app)

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
