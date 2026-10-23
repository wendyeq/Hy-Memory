"""Startup and sync resume must not open an unbounded history import."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from one_memory.api import create_app
from one_memory.config import Settings
from one_memory.imports.grants import SourceGrantStore
from one_memory.imports.sources.registry import RootCandidateKind, build_source_catalog
from one_memory.secrets import InMemorySecretStore
from tests.source_consent import authorized_request


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


def test_core_startup_creates_a_one_day_history_window(tmp_path, monkeypatch) -> None:
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
    started = datetime.now(UTC)
    app = create_app(
        _settings(tmp_path),
        source_catalog=build_source_catalog(home=home, environment={}),
        secret_store=InMemorySecretStore(),
    )

    async def scenario():
        async with app.router.lifespan_context(app):
            jobs = [
                job
                for job in app.state.import_job_store.list_jobs()
                if job.source_id == "claude-code" and job.operation == "history"
            ]
            assert len(jobs) == 1
            _assert_one_day(jobs[0].history_after, started=started)

    asyncio.run(scenario())


def test_sync_permission_resume_creates_a_one_day_history_window(tmp_path, monkeypatch) -> None:
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
            authorized = await client.post(
                "/api/v1/sources/claude-code/authorize",
                json=await authorized_request(client, "claude-code"),
            )
            assert authorized.status_code == 200
            started = datetime.now(UTC)
            enabled = await client.post(
                "/api/v1/sources/claude-code/sync-permission",
                json={"enabled": True},
            )
            assert enabled.status_code == 200
            jobs = [
                job
                for job in app.state.import_job_store.list_jobs()
                if job.source_id == "claude-code" and job.operation == "history"
            ]
            assert len(jobs) == 1
            _assert_one_day(jobs[0].history_after, started=started)

    asyncio.run(scenario())
