import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress

import pytest

from one_memory.imports.adapters import (
    ScanEvent,
    ScannedEvidence,
    ScanOptions,
    SourceCapabilities,
    SourceCursor,
    SourceDescriptor,
    StorageKind,
)
from one_memory.imports.jobs import ImportJobStore, ImportStatus, InvalidJobTransitionError
from one_memory.imports.models import (
    EvidenceInput,
    EvidenceOrigin,
    OriginMetadata,
    WrittenBy,
)
from one_memory.imports.storage import EvidenceStore
from one_memory.imports.supervisor import ImportSupervisor
from one_memory.service_levels import ResourceLimit, ServiceLevelScheduler, ServiceResource


def scanned(message_id: str, *, source_id: str = "fixture") -> ScannedEvidence:
    return ScannedEvidence(
        evidence=EvidenceInput(
            source_id=source_id,
            conversation_id="conversation",
            message_id=message_id,
            role="user",
            written_by=WrittenBy.PERSON,
            verbatim=message_id,
            origin_meta={EvidenceOrigin.FILE: OriginMetadata()},
        ),
        cursor=SourceCursor(
            file_key="a" * 64,
            file_identity="fixture:1",
            checkpoint=f"line:{message_id}",
            observed_size=100,
            observed_modified_ns=1,
        ),
    )


class ControlledAdapter:
    def __init__(
        self,
        events: list[ScanEvent],
        gate: asyncio.Event | None = None,
        source_id: str = "fixture",
    ) -> None:
        self.descriptor = SourceDescriptor(
            source_id=source_id,
            display_name=source_id,
            storage_kinds=frozenset({StorageKind.JSONL}),
            capabilities=SourceCapabilities(
                can_append=True,
                can_observe_edits=False,
                can_observe_deletes=False,
                can_reconcile_complete_set=False,
            ),
        )
        self.events = events
        self.gate = gate

    async def detect(self) -> bool:
        return True

    async def scan(self, _: ScanOptions) -> AsyncIterator[ScanEvent]:
        if self.gate is not None:
            await self.gate.wait()
        for event in self.events:
            yield event


class CursorAwareAdapter(ControlledAdapter):
    async def scan(self, options: ScanOptions) -> AsyncIterator[ScanEvent]:
        checkpoint = options.cursors.get("a" * 64)
        completed = int(checkpoint.checkpoint.removeprefix("line:")) if checkpoint else 0
        for event in self.events[completed:]:
            await asyncio.sleep(0)
            yield event


class FailOnceCursorAwareAdapter(CursorAwareAdapter):
    def __init__(self, events: list[ScanEvent], *, source_id: str = "fixture") -> None:
        super().__init__(events, source_id=source_id)
        self.scans = 0

    async def scan(self, options: ScanOptions) -> AsyncIterator[ScanEvent]:
        self.scans += 1
        async for event in super().scan(options):
            yield event
            if self.scans == 1:
                raise ValueError("transient history reader failure")


def stores(tmp_path):
    database = tmp_path / "one-memory.sqlite3"
    return EvidenceStore(database), ImportJobStore(database)


def test_supervisor_starts_all_granted_sources_together(tmp_path) -> None:
    async def scenario() -> None:
        evidence_store, job_store = stores(tmp_path)
        gates = {source_id: asyncio.Event() for source_id in ("alpha", "beta", "gamma")}
        supervisor = ImportSupervisor(
            adapters={
                source_id: ControlledAdapter([scanned(f"{source_id}-1")], gate, source_id=source_id)
                for source_id, gate in gates.items()
            },
            evidence_store=evidence_store,
            job_store=job_store,
            max_concurrent_jobs=8,
        )
        jobs = [job_store.create(user_id="local", source_id=source_id) for source_id in gates]
        for job in jobs:
            supervisor.enqueue(job.job_id)
        for _ in range(40):
            if all(job_store.require(job.job_id).status is ImportStatus.RUNNING for job in jobs):
                break
            await asyncio.sleep(0)
        assert {job_store.require(job.job_id).status for job in jobs} == {ImportStatus.RUNNING}
        for gate in gates.values():
            gate.set()
        for job in jobs:
            completed = await supervisor.wait(job.job_id)
            assert completed.status is ImportStatus.COMPLETED

    asyncio.run(scenario())


def test_supervisor_runs_enqueued_job_in_background(tmp_path) -> None:
    async def scenario() -> None:
        evidence_store, job_store = stores(tmp_path)
        supervisor = ImportSupervisor(
            adapters={"fixture": ControlledAdapter([scanned("1")])},
            evidence_store=evidence_store,
            job_store=job_store,
        )
        job = job_store.create(user_id="local", source_id="fixture")

        supervisor.enqueue(job.job_id)
        completed = await supervisor.wait(job.job_id)

        assert completed.status is ImportStatus.COMPLETED
        assert evidence_store.count() == 1

    asyncio.run(scenario())


def test_harness_freeze_keeps_pending_job_and_thaw_runs_the_same_row(tmp_path) -> None:
    async def scenario() -> None:
        evidence_store, job_store = stores(tmp_path)
        enabled = False
        supervisor = ImportSupervisor(
            adapters={"fixture": ControlledAdapter([scanned("1")])},
            evidence_store=evidence_store,
            job_store=job_store,
            source_enabled=lambda _source_id: enabled,
        )
        job = job_store.create(user_id="local", source_id="fixture")

        supervisor.enqueue(job.job_id)
        await asyncio.sleep(0)
        frozen = job_store.require(job.job_id)
        assert frozen.status is ImportStatus.PENDING
        assert supervisor.active_job_ids() == frozenset()
        assert evidence_store.count() == 0

        enabled = True
        supervisor.enqueue(job.job_id)
        completed = await supervisor.wait(job.job_id)
        assert completed.job_id == job.job_id
        assert completed.status is ImportStatus.COMPLETED
        assert evidence_store.count() == 1

    asyncio.run(scenario())


def test_harness_freeze_does_not_cancel_a_job_already_running(tmp_path) -> None:
    async def scenario() -> None:
        evidence_store, job_store = stores(tmp_path)
        gate = asyncio.Event()
        enabled = True
        supervisor = ImportSupervisor(
            adapters={"fixture": ControlledAdapter([scanned("1")], gate)},
            evidence_store=evidence_store,
            job_store=job_store,
            source_enabled=lambda _source_id: enabled,
        )
        job = job_store.create(user_id="local", source_id="fixture")
        supervisor.enqueue(job.job_id)
        for _ in range(40):
            if job_store.require(job.job_id).status is ImportStatus.RUNNING:
                break
            await asyncio.sleep(0)
        assert job_store.require(job.job_id).status is ImportStatus.RUNNING

        enabled = False
        gate.set()
        completed = await supervisor.wait(job.job_id)
        assert completed.status is ImportStatus.COMPLETED
        assert evidence_store.count() == 1

    asyncio.run(scenario())


def test_history_failure_retries_same_job_from_checkpoint(tmp_path) -> None:
    async def scenario() -> None:
        evidence_store, job_store = stores(tmp_path)
        adapter = FailOnceCursorAwareAdapter([scanned("1"), scanned("2")])
        supervisor = ImportSupervisor(
            adapters={"fixture": adapter},
            evidence_store=evidence_store,
            job_store=job_store,
            retry_base_seconds=0.01,
        )
        job = supervisor.create_job("fixture")

        supervisor.enqueue(job.job_id)
        completed = await asyncio.wait_for(supervisor.wait(job.job_id), timeout=2)

        assert completed.status is ImportStatus.COMPLETED
        assert completed.stored == 2
        assert evidence_store.count() == 2
        assert adapter.scans == 2

    asyncio.run(scenario())


def test_shutdown_marks_running_job_interrupted_and_resume_finishes(tmp_path) -> None:
    async def scenario() -> None:
        evidence_store, job_store = stores(tmp_path)
        gate = asyncio.Event()
        first = ImportSupervisor(
            adapters={"fixture": ControlledAdapter([scanned("1")], gate)},
            evidence_store=evidence_store,
            job_store=job_store,
        )
        job = job_store.create(user_id="local", source_id="fixture")
        first.enqueue(job.job_id)
        async with asyncio.timeout(2):
            while job_store.require(job.job_id).status is not ImportStatus.RUNNING:
                await asyncio.sleep(0.01)

        await first.shutdown()

        assert job_store.require(job.job_id).status is ImportStatus.INTERRUPTED

        second = ImportSupervisor(
            adapters={"fixture": ControlledAdapter([scanned("1")])},
            evidence_store=evidence_store,
            job_store=job_store,
        )
        queued = second.resume(job.job_id)
        assert queued.status is ImportStatus.PENDING
        completed = await second.wait(job.job_id)

        assert completed.status is ImportStatus.COMPLETED
        assert evidence_store.count() == 1

    asyncio.run(scenario())


def test_pending_workbuddy_runs_before_long_codex_history_finishes(tmp_path) -> None:
    async def scenario() -> None:
        evidence_store, job_store = stores(tmp_path)
        delivered: list[str] = []
        supervisor = ImportSupervisor(
            adapters={
                "codex": CursorAwareAdapter(
                    [scanned(str(index), source_id="codex") for index in range(1, 258)],
                    source_id="codex",
                ),
                "workbuddy": CursorAwareAdapter(
                    [scanned("1", source_id="workbuddy")], source_id="workbuddy"
                ),
            },
            evidence_store=evidence_store,
            job_store=job_store,
            max_concurrent_jobs=1,
            on_evidence=lambda record: delivered.append(record.source_id),
        )
        codex = supervisor.create_job("codex")
        workbuddy = supervisor.create_job("workbuddy")

        supervisor.enqueue(codex.job_id)
        supervisor.enqueue(workbuddy.job_id)
        completed_workbuddy = await asyncio.wait_for(supervisor.wait(workbuddy.job_id), timeout=2)
        completed_codex = await asyncio.wait_for(supervisor.wait(codex.job_id), timeout=2)

        assert completed_workbuddy.status is ImportStatus.COMPLETED
        assert completed_codex.status is ImportStatus.COMPLETED
        assert delivered.index("workbuddy") < len(delivered) - 1

    asyncio.run(scenario())


def test_startup_recovers_interrupted_sync_before_pending_sync(tmp_path) -> None:
    async def scenario(root) -> None:
        evidence_store, job_store = stores(root)
        delivered: list[str] = []
        workbuddy = job_store.create(user_id="local", source_id="workbuddy", operation="sync")
        codex = job_store.create(user_id="local", source_id="codex", operation="sync")
        history = job_store.create(user_id="local", source_id="codex", operation="history")
        _, token = job_store.claim(codex.job_id)
        job_store.interrupt_running(codex.job_id, runner_token=token)
        # Sync is realtime. The default limit is max_concurrent_jobs + 1, so
        # two sync jobs both run and their first records race. One realtime
        # slot admits the interrupted job first and holds the pending one.
        supervisor = ImportSupervisor(
            adapters={
                "workbuddy": CursorAwareAdapter(
                    [scanned(str(index), source_id="workbuddy") for index in range(1, 5)],
                    source_id="workbuddy",
                ),
                "codex": CursorAwareAdapter([scanned("1", source_id="codex")], source_id="codex"),
            },
            evidence_store=evidence_store,
            job_store=job_store,
            max_work_events_per_slice=1,
            on_evidence=lambda record: delivered.append(record.source_id),
            service_levels=ServiceLevelScheduler(
                {ServiceResource.IMPORT: ResourceLimit(capacity=1, background_capacity=1)}
            ),
        )

        supervisor.resume_interrupted()
        recovered = await asyncio.wait_for(supervisor.wait(codex.job_id), timeout=2)
        pending = await asyncio.wait_for(supervisor.wait(workbuddy.job_id), timeout=2)

        assert recovered.status is ImportStatus.COMPLETED
        assert pending.status is ImportStatus.COMPLETED
        assert delivered[0] == "codex"
        assert job_store.require(history.job_id).status is ImportStatus.PENDING

    for index in range(20):
        asyncio.run(scenario(tmp_path / str(index)))


def test_single_worker_rotates_across_sources(tmp_path) -> None:
    async def scenario() -> None:
        evidence_store, job_store = stores(tmp_path)
        delivered: list[str] = []
        source_ids = ("codex", "workbuddy", "cursor")
        supervisor = ImportSupervisor(
            adapters={
                source_id: CursorAwareAdapter(
                    [scanned(str(index), source_id=source_id) for index in range(1, 4)],
                    source_id=source_id,
                )
                for source_id in source_ids
            },
            evidence_store=evidence_store,
            job_store=job_store,
            max_concurrent_jobs=1,
            max_work_events_per_slice=1,
            on_evidence=lambda record: delivered.append(record.source_id),
        )
        jobs = [supervisor.create_job(source_id) for source_id in source_ids]

        for job in jobs:
            supervisor.enqueue(job.job_id)
        for job in jobs:
            completed = await asyncio.wait_for(supervisor.wait(job.job_id), timeout=2)
            assert completed.status is ImportStatus.COMPLETED

        assert delivered[:3] == list(source_ids)

    asyncio.run(scenario())


def test_cancelling_waiter_does_not_cancel_supervisor_owned_job(tmp_path) -> None:
    async def scenario() -> None:
        evidence_store, job_store = stores(tmp_path)
        gate = asyncio.Event()
        supervisor = ImportSupervisor(
            adapters={"fixture": ControlledAdapter([scanned("1")], gate)},
            evidence_store=evidence_store,
            job_store=job_store,
        )
        job = job_store.create(user_id="local", source_id="fixture")
        supervisor.enqueue(job.job_id)
        waiter = asyncio.create_task(supervisor.wait(job.job_id))
        for _ in range(50):
            if job_store.require(job.job_id).status is ImportStatus.RUNNING:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("job did not start")

        waiter.cancel()
        with suppress(asyncio.CancelledError):
            await waiter

        assert job_store.require(job.job_id).status is ImportStatus.RUNNING
        gate.set()
        completed = await supervisor.wait(job.job_id)
        assert completed.status is ImportStatus.COMPLETED

    asyncio.run(scenario())


@pytest.mark.parametrize("terminal", [ImportStatus.COMPLETED, ImportStatus.FAILED])
def test_shutdown_preserves_a_job_that_finished_after_the_running_snapshot(
    tmp_path, monkeypatch, terminal
) -> None:
    async def scenario() -> None:
        evidence, jobs = stores(tmp_path)
        supervisor = ImportSupervisor(adapters={}, evidence_store=evidence, job_store=jobs)
        job = jobs.create(user_id="local", source_id="fixture")
        job, token = jobs.claim(job.job_id)
        stopped = asyncio.Event()

        async def waiting() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        task = asyncio.create_task(waiting())
        supervisor._tasks[job.job_id] = task
        await asyncio.sleep(0)
        original = jobs.find_jobs

        def finish_after_snapshot(*args, **kwargs):
            found = original(*args, **kwargs)
            if tuple(kwargs.get("statuses") or ()) == (ImportStatus.RUNNING,):
                jobs.finish_owned(job.job_id, runner_token=token, target=terminal)
            return found

        monkeypatch.setattr(jobs, "find_jobs", finish_after_snapshot)
        try:
            await supervisor.shutdown()
            assert jobs.require(job.job_id).status is terminal
            assert task.done() and stopped.is_set()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_abandon_makes_its_database_calls_off_the_event_loop(tmp_path, monkeypatch) -> None:
    """Live sync abandons from its poll; a writer wait there froze the whole Core."""
    import threading

    async def scenario() -> None:
        evidence, jobs = stores(tmp_path)
        supervisor = ImportSupervisor(adapters={}, evidence_store=evidence, job_store=jobs)
        job = jobs.create(user_id="local", source_id="fixture", operation="sync")
        job, _token = jobs.claim(job.job_id)
        loop_thread = threading.current_thread()
        calls: list[tuple[str, threading.Thread]] = []
        for name in ("require", "interrupt_running"):
            original = getattr(jobs, name)

            def recorded(*args, _name=name, _original=original, **kwargs):
                calls.append((_name, threading.current_thread()))
                return _original(*args, **kwargs)

            monkeypatch.setattr(jobs, name, recorded)

        async def wedged() -> None:
            # Ignores the first cancel, like a runner stuck in a thread.
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0.2)

        task = asyncio.create_task(wedged())
        supervisor._tasks[job.job_id] = task
        await asyncio.sleep(0)
        try:
            assert await supervisor.abandon(job.job_id, timeout=0.01) is False
        finally:
            await asyncio.gather(task, return_exceptions=True)

        assert {name for name, _thread in calls} == {"require", "interrupt_running"}
        assert all(thread is not loop_thread for _name, thread in calls)
        assert jobs.require(job.job_id).status is ImportStatus.INTERRUPTED

    asyncio.run(scenario())


def test_queue_for_resume_keeps_a_sync_another_worker_already_moved(tmp_path) -> None:
    """A second resume must not raise after the first one claimed or finished the sync."""
    _evidence, jobs = stores(tmp_path)
    claimed = jobs.create(user_id="local", source_id="claimed", operation="sync")
    jobs.claim(claimed.job_id)
    kept = jobs.queue_for_resume(claimed.job_id)
    assert kept.job_id == claimed.job_id
    assert kept.status is ImportStatus.RUNNING

    finished = jobs.create(user_id="local", source_id="finished", operation="sync")
    jobs.claim(finished.job_id)
    jobs.transition(finished.job_id, ImportStatus.COMPLETED)
    kept_finished = jobs.queue_for_resume(finished.job_id)
    assert kept_finished.job_id == finished.job_id
    assert kept_finished.status is ImportStatus.COMPLETED


def test_queue_for_resume_survives_a_concurrent_claim(tmp_path) -> None:
    """Claim and queue meet on one interrupted sync. Queue must not raise."""
    import threading

    _evidence, jobs = stores(tmp_path)
    failures: list[str] = []
    for index in range(40):
        job = jobs.create(user_id="local", source_id=f"source-{index}", operation="sync")
        jobs.claim(job.job_id)
        jobs.interrupt_running(job.job_id)
        barrier = threading.Barrier(2)

        def claim() -> None:
            barrier.wait()
            try:
                jobs.claim(job.job_id)
            except InvalidJobTransitionError:
                return

        def queue() -> None:
            barrier.wait()
            try:
                jobs.queue_for_resume(job.job_id)
            except InvalidJobTransitionError as error:
                failures.append(str(error))

        threads = (
            threading.Thread(target=claim),
            threading.Thread(target=queue),
        )
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert jobs.require(job.job_id).status in {ImportStatus.PENDING, ImportStatus.RUNNING}
    assert failures == []


def test_resume_off_loop_restarts_an_ownerless_running_sync(tmp_path) -> None:
    """A running sync whose task already exited is started again. History is not."""

    async def scenario() -> None:
        evidence, jobs = stores(tmp_path)
        supervisor = ImportSupervisor(
            adapters={"fixture": ControlledAdapter([scanned("sync-row")])},
            evidence_store=evidence,
            job_store=jobs,
        )
        sync = jobs.create(user_id="local", source_id="fixture", operation="sync")
        history = jobs.create(user_id="local", source_id="fixture", operation="history")
        jobs.claim(sync.job_id)

        await supervisor.resume_off_loop(sync.job_id)
        finished = await asyncio.wait_for(supervisor.wait(sync.job_id), 5)

        assert finished.job_id == sync.job_id
        assert finished.status is ImportStatus.COMPLETED
        assert finished.stored == 1
        assert jobs.require(history.job_id).status is ImportStatus.PENDING
        assert history.job_id not in supervisor.active_job_ids()
        await supervisor.shutdown()

    asyncio.run(scenario())


def test_resume_off_loop_leaves_a_live_sync_running(tmp_path) -> None:
    async def scenario() -> None:
        evidence, jobs = stores(tmp_path)
        gate = asyncio.Event()
        supervisor = ImportSupervisor(
            adapters={"fixture": ControlledAdapter([scanned("sync-row")], gate)},
            evidence_store=evidence,
            job_store=jobs,
        )
        sync = jobs.create(user_id="local", source_id="fixture", operation="sync")
        history = jobs.create(user_id="local", source_id="fixture", operation="history")
        supervisor.enqueue(sync.job_id)
        for _ in range(100):
            if jobs.require(sync.job_id).status is ImportStatus.RUNNING:
                break
            await asyncio.sleep(0.01)
        assert jobs.require(sync.job_id).status is ImportStatus.RUNNING

        resumed = await supervisor.resume_off_loop(sync.job_id)

        assert resumed.job_id == sync.job_id
        assert resumed.status is ImportStatus.RUNNING
        assert sync.job_id in supervisor.active_job_ids()
        assert jobs.require(history.job_id).status is ImportStatus.PENDING
        gate.set()
        finished = await asyncio.wait_for(supervisor.wait(sync.job_id), 5)
        assert finished.status is ImportStatus.COMPLETED
        assert jobs.require(history.job_id).status is ImportStatus.PENDING
        await supervisor.shutdown()

    asyncio.run(scenario())
