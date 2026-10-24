"""Offline public-step controls for the opt-in durable async controller."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import threading
import json
from pathlib import Path

import pytest
import httpx
import jev_factorio.checkpoint_io as checkpoint_io

from jev_factorio.backends.mock import MockBackend
from jev_factorio.controller import AsyncControllerBusy, HierarchicalLoop
from jev_factorio.jev_client import (
    AsyncJevClient, AsyncMockJevClient, MockJevClient, make_result,
    request_payload_sha256,
)


SOURCE = {"commit": "a" * 40, "source_sha256": "b" * 64}


class CountingAsyncMock(AsyncMockJevClient):
    def __init__(self):
        self.calls = 0

    async def evaluate(self, *args, **kwargs):
        self.calls += 1
        return await super().evaluate(*args, **kwargs)


def _controller(tmp_path, *, backend=None, client=None, resume=False):
    backend = backend or MockBackend()
    client = client or CountingAsyncMock()
    loop = HierarchicalLoop(
        backend, jev=client, policy="jev", target="bootstrap_mining",
        checkpoint=str(tmp_path / "campaign.json"), resume_controller=resume,
        tick_seconds=0, async_decisions=True,
    )
    # The async archive intentionally refuses unattributed source revisions.
    loop.provenance["code_revision"] = SOURCE
    return loop, backend, client


class SemanticMockBackend(MockBackend):
    def __init__(self):
        super().__init__()
        self.decision_deadline_tick = 100
        self.completed_batches = 0

    def observe(self):
        snapshot = super().observe()
        snapshot.factory["decision_deadline_tick"] = self.decision_deadline_tick
        snapshot.factory["completed_batches"] = self.completed_batches
        return snapshot


class IdentifiedMockFLEBackend(MockBackend):
    def __init__(self):
        super().__init__()
        self.actor_unit = 41
        self.surface_index = 2
        self.force_index = 3
        self._native_attachment = {
            "qualified": True, "session_id": self.session_id,
            "actor_unit": self.actor_unit,
        }

    def observe(self):
        snapshot = super().observe()
        snapshot.world_kind = "fle"
        snapshot.factory["acceptance_runtime"] = {
            "session_id": snapshot.session_id, "actor_unit": self.actor_unit,
            "surface_index": self.surface_index, "force_index": self.force_index,
        }
        return snapshot


class OfflineFLEAsyncMock(CountingAsyncMock):
    # Local deterministic answers exercise the identified-FLE identity gate;
    # no provider transport or native backend is present in this fixture.
    is_mock = False


def test_public_async_step_consumes_one_exact_response_then_verifies_one_action(tmp_path):
    loop, backend, client = _controller(tmp_path)

    result = asyncio.run(loop.step_async())

    assert client.calls == 1
    assert result["action"] == "walk_to_coal"
    assert result["verified"] is True
    assert backend.tick == 1
    assert loop.memory.async_decision is None
    assert loop.memory.pending is None
    assert loop.memory.attempt is None
    assert loop.memory.active_plan is not None
    assert loop.memory.attempt_outcomes[-1]["outcome"] == "verified"
    assert any(row.get("kind") == "async_decision_settled" for row in loop.memory.history)
    assert all(row.get("kind") != "async_decision_settled"
               for row in loop._model_history())
    archive_dir = tmp_path / "campaign.json.safety" / "decision-archives"
    archived = [json.loads(path.read_text()) for path in archive_dir.glob("*.json")]
    assert len(archived) == 1
    assert loop._async_lease_from_record(archived[0]).inspect().state == "consumed"
    assert loop.jev.state["decision_outcome"]["state"] == "consumed"


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_unselected_checkpoint_roundtrips_and_starts_new_async_archive(tmp_path, version):
    from jev_factorio.memory import CampaignMemory

    backend = MockBackend()
    client = CountingAsyncMock()
    checkpoint = tmp_path / "campaign.json"
    legacy = CampaignMemory(backend.session_id, "bootstrap_mining", version=version)
    legacy_data = checkpoint_io.checkpoint_data(legacy)
    if version == 1:
        legacy_data.pop("attempt")
        legacy_data.pop("attempt_outcomes")
    checkpoint.write_text(json.dumps(legacy_data), encoding="utf-8")
    from jev_factorio.provider_decision_wal import ProviderDecisionWAL
    safety = checkpoint.with_name(checkpoint.name + ".safety")
    safety.mkdir(mode=0o700)
    ProviderDecisionWAL.initialize(
        safety / "provider-decisions.json")

    loaded = CampaignMemory.load(checkpoint, backend.session_id, "bootstrap_mining")
    assert loaded.version == 2
    assert loaded.async_decision is None
    assert not any(row.get("kind") == "async_plan_lineage" for row in loaded.history)
    loop, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    result = asyncio.run(loop.step_async())
    assert loop.memory.async_decision is None

    assert result["verified"] is True
    assert client.calls == 1
    assert len(loop._async_archive.records()) == 1
    assert len(loop._async_plan_lineage_entries()) == 1


def test_persistent_async_attempt_and_exact_archive_are_checkpointed_before_send(
        tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_FACTORIO_PROVENANCE", json.dumps({
        "run_id": "async-run", "segment_id": "async-segment",
        "execution_id": "async-execution", "code_revision": SOURCE,
    }))

    class PausedLiveShapedClient(CountingAsyncMock):
        is_mock = False

        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def evaluate(self, state, questions, *, identity, deadline=None,
                           decision_lease=None):
            self.calls += 1
            payload = self.prepare_decision_payload(state, questions)
            decision_lease.mark_may_have_been_sent()
            self.started.set()
            await self.release.wait()
            answers = MockJevClient().evaluate(payload["state"], payload["questions"])
            result = make_result(
                identity, answers, {"total_tokens": 1}, self.model,
                "offline-resolved", request_payload_sha256(payload),
            )
            decision_lease.save_response(result)
            return result

    backend = MockBackend()
    client = PausedLiveShapedClient()
    checkpoint = tmp_path / "campaign.json"
    loop = HierarchicalLoop(
        backend, jev=client, policy="jev", target="bootstrap_mining",
        checkpoint=str(checkpoint), tick_seconds=0, async_decisions=True,
        persist_recoverable_blocks=True, initialize_persistent_campaign=True,
    )

    async def run():
        task = asyncio.create_task(loop.step_async())
        await asyncio.wait_for(client.started.wait(), 2)
        captured = json.loads(checkpoint.read_text(encoding="utf-8"))
        assert captured["async_decision"]["disposition"] == "pending"
        assert captured["blocked_recovery"]["attempts"][-1]["outcome"] == "pending"
        record = loop._async_archive.load(captured["async_decision"])
        lease = loop._async_lease_from_record(record)
        assert lease.inspect().state == "may_have_been_sent"
        assert record["provider"]["payload_json"]
        assert record["provider"]["wire_body_sha256"]
        client.release.set()
        result = await task
        return result, captured, record

    result, captured, record = asyncio.run(run())

    assert client.calls == 1
    assert result["verified"] is True
    assert captured["async_decision"]["request_id"] == record["request_id"]
    assert loop.memory.async_decision is None
    assert loop._async_lease_from_record(record).inspect().state == "consumed"


def test_async_step_requires_explicit_opt_in_and_sync_step_rejects_async_mode(tmp_path):
    loop, _, _ = _controller(tmp_path)
    with pytest.raises(RuntimeError, match="await step_async"):
        loop.step()


def test_same_stable_actor_rejects_overlapping_async_steps(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class WaitingClient(CountingAsyncMock):
        async def evaluate(self, *args, **kwargs):
            entered.set()
            await asyncio.to_thread(release.wait, 2)
            return await super().evaluate(*args, **kwargs)

    async def run():
        loop, _, _ = _controller(tmp_path, client=WaitingClient())
        first = asyncio.create_task(loop.step_async())
        assert await asyncio.to_thread(entered.wait, 2)
        with pytest.raises(AsyncControllerBusy, match="already has a controller step"):
            await loop.step_async()
        release.set()
        await first

    asyncio.run(run())


def test_same_session_serializes_distinct_actor_facades_sharing_backend(tmp_path):
    class GatedSharedBackend(MockBackend):
        def __init__(self):
            super().__init__()
            self.guard = threading.Lock()
            self.entries = 0
            self.first_entered = threading.Event()
            self.release_actions = threading.Event()
            self.active_actions = 0
            self.max_active_actions = 0

        def act(self, action):
            with self.guard:
                self.entries += 1
                self.active_actions += 1
                self.max_active_actions = max(
                    self.max_active_actions, self.active_actions)
                self.first_entered.set()
            if not self.release_actions.wait(5):
                raise TimeoutError("test did not release the shared backend action")
            try:
                return super().act(action)
            finally:
                with self.guard:
                    self.active_actions -= 1

    class ActorView:
        def __init__(self, shared, actor_unit):
            self._shared = shared
            self.session_id = shared.session_id
            self._native_attachment = {
                "qualified": True, "session_id": shared.session_id,
                "actor_unit": actor_unit,
            }
            self.actor_unit = actor_unit
            self.surface_index = 2
            self.force_index = 3

        def __getattr__(self, name):
            return getattr(self._shared, name)

        def observe(self):
            snapshot = self._shared.observe()
            snapshot.world_kind = "fle"
            snapshot.factory["acceptance_runtime"] = {
                "session_id": snapshot.session_id,
                "actor_unit": self.actor_unit,
                "surface_index": self.surface_index,
                "force_index": self.force_index,
            }
            return snapshot

        def act(self, action):
            return self._shared.act(action)

    shared = GatedSharedBackend()
    first_dir, second_dir = tmp_path / "actor-41", tmp_path / "actor-42"
    first_dir.mkdir()
    second_dir.mkdir()
    first, _, first_client = _controller(
        first_dir, backend=ActorView(shared, 41), client=OfflineFLEAsyncMock())
    second, _, second_client = _controller(
        second_dir, backend=ActorView(shared, 42), client=OfflineFLEAsyncMock())

    async def run():
        first_task = asyncio.create_task(first.step_async())
        assert await asyncio.to_thread(shared.first_entered.wait, 5)
        second_task = asyncio.create_task(second.step_async())
        await asyncio.sleep(0.03)
        second_done_before_release = second_task.done()
        second_error = (second_task.exception() if second_done_before_release else None)
        active_before_release = shared.active_actions
        maximum_before_release = shared.max_active_actions
        shared.release_actions.set()
        first_result = await first_task
        if not second_done_before_release:
            try:
                await second_task
                second_error = None
            except BaseException as error:
                second_error = error
        sequential_result = None
        if isinstance(second_error, AsyncControllerBusy):
            sequential_result = await second.step_async()
        return (first_result, second_error, sequential_result,
                active_before_release, maximum_before_release)

    first_result, second_error, sequential_result, active, maximum = asyncio.run(run())

    assert isinstance(second_error, AsyncControllerBusy)
    assert active == 1
    assert maximum == 1
    assert first_result["verified"] is True
    assert first_client.calls == 1
    assert shared.tick == 2
    assert second_client.calls == 1
    assert sequential_result is not None and sequential_result["verified"] is True


def test_distinct_sessions_sharing_one_transport_are_serialized(tmp_path):
    class GatedSharedBackend(MockBackend):
        def __init__(self):
            super().__init__()
            self.guard = threading.Lock()
            self.first_entered = threading.Event()
            self.release_action = threading.Event()
            self.active = 0
            self.maximum = 0

        def act(self, action):
            with self.guard:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                self.first_entered.set()
            if not self.release_action.wait(5):
                raise TimeoutError("test did not release the shared transport")
            try:
                return super().act(action)
            finally:
                with self.guard:
                    self.active -= 1

    class SessionView:
        def __init__(self, shared, session_id):
            self._shared = shared
            self.session_id = session_id
            self.actor_unit = 41
            self.surface_index = 2
            self.force_index = 3
            self._native_attachment = {
                "qualified": True, "session_id": session_id, "actor_unit": 41,
            }

        def __getattr__(self, name):
            return getattr(self._shared, name)

        def observe(self):
            snapshot = self._shared.observe()
            snapshot.session_id = self.session_id
            snapshot.world_kind = "fle"
            snapshot.factory["acceptance_runtime"] = {
                "session_id": self.session_id, "actor_unit": 41,
                "surface_index": 2, "force_index": 3,
            }
            return snapshot

        def act(self, action):
            return self._shared.act(action)

    shared = GatedSharedBackend()
    first_dir, second_dir = tmp_path / "session-a", tmp_path / "session-b"
    first_dir.mkdir()
    second_dir.mkdir()
    first, _, first_client = _controller(
        first_dir, backend=SessionView(shared, "fle-session-a"),
        client=OfflineFLEAsyncMock())
    second, _, second_client = _controller(
        second_dir, backend=SessionView(shared, "fle-session-b"),
        client=OfflineFLEAsyncMock())

    async def run():
        first_task = asyncio.create_task(first.step_async())
        assert await asyncio.to_thread(shared.first_entered.wait, 5)
        second_task = asyncio.create_task(second.step_async())
        await asyncio.sleep(0.03)
        second_blocked = second_task.done()
        error = second_task.exception() if second_blocked else None
        active, maximum = shared.active, shared.maximum
        shared.release_action.set()
        first_result = await first_task
        if not second_blocked:
            try:
                await second_task
                error = None
            except BaseException as caught:
                error = caught
        retry = None
        if isinstance(error, AsyncControllerBusy):
            retry = await second.step_async()
        return first_result, error, retry, active, maximum

    first_result, error, retry, active, maximum = asyncio.run(run())
    assert isinstance(error, AsyncControllerBusy)
    assert active == 1 and maximum == 1
    assert first_result["verified"] is True
    assert first_client.calls == 1 and second_client.calls == 1
    assert retry is not None and retry["verified"] is True
    assert shared.tick == 2


def test_distinct_sessions_and_transports_remain_parallel(tmp_path):
    class GatedBackend(MockBackend):
        def __init__(self, barrier):
            super().__init__()
            self.barrier = barrier

        def act(self, action):
            self.barrier.entered()
            if not self.barrier.release.wait(5):
                raise TimeoutError("test did not release independent transports")
            return super().act(action)

    class Barrier:
        def __init__(self):
            self.guard = threading.Lock()
            self.active = 0
            self.maximum = 0
            self.both_entered = threading.Event()
            self.release = threading.Event()

        def entered(self):
            with self.guard:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                if self.active == 2:
                    self.both_entered.set()

    barrier = Barrier()
    first_dir, second_dir = tmp_path / "transport-a", tmp_path / "transport-b"
    first_dir.mkdir()
    second_dir.mkdir()
    first, first_backend, first_client = _controller(
        first_dir, backend=GatedBackend(barrier))
    second, second_backend, second_client = _controller(
        second_dir, backend=GatedBackend(barrier))

    async def run():
        first_task = asyncio.create_task(first.step_async())
        second_task = asyncio.create_task(second.step_async())
        both = await asyncio.to_thread(barrier.both_entered.wait, 5)
        barrier.release.set()
        results = await asyncio.gather(first_task, second_task)
        return both, results

    both, results = asyncio.run(run())
    assert first_backend is not second_backend
    assert both and barrier.maximum == 2
    assert all(result["verified"] for result in results)
    assert first_client.calls == second_client.calls == 1


def test_async_worker_rejects_backend_without_supported_transport_identity(tmp_path):
    class UnknownTransportBackend:
        def __init__(self):
            self.inner = MockBackend()
            self.session_id = self.inner.session_id
            self.actor_unit = 41
            self.actions = 0

        def observe(self):
            return self.inner.observe()

        def act(self, action):
            self.actions += 1
            return self.inner.act(action)

    backend = UnknownTransportBackend()
    client = CountingAsyncMock()
    loop, _, _ = _controller(tmp_path, backend=backend, client=client)
    with pytest.raises(AsyncControllerBusy, match="cannot establish a supported backend transport identity"):
        asyncio.run(loop.step_async())
    assert backend.actions == 0
    assert client.calls == 0


class _InjectedCrash(RuntimeError):
    pass


def _interrupt_before_plan_checkpoint(loop):
    def crash():
        raise _InjectedCrash("crash after durable provider response")

    loop._trace_decision = crash


def test_public_async_crash_after_response_resumes_without_a_second_provider_call(tmp_path):
    backend = MockBackend()
    client = CountingAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    _interrupt_before_plan_checkpoint(first)

    with pytest.raises(_InjectedCrash):
        asyncio.run(first.step_async())
    assert client.calls == 1
    assert first.memory.active_plan is None
    assert first.memory.async_decision["disposition"] == "pending"
    assert first._async_lease_from_record(
        first._async_archive.load(first.memory.async_decision)).inspect().state == "response_received"
    assert backend.tick == 0

    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    result = asyncio.run(resumed.step_async())

    assert client.calls == 1
    assert result["action"] == "walk_to_coal"
    assert result["verified"] is True
    assert backend.tick == 1
    assert resumed.memory.async_decision is None
    assert first._async_lease_from_record(
        first._async_archive.load(first.memory.async_decision)).inspect().state == "consumed"


def test_crash_after_checkpoint_pointer_before_wal_reuses_exact_archived_request(tmp_path):
    backend = MockBackend()
    client = CountingAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    prepare = first._async_prepare_selection

    def crash_after_checkpoint(**kwargs):
        prepare(**kwargs)
        raise _InjectedCrash("crash after checkpoint pointer, before WAL reservation")

    first._async_prepare_selection = crash_after_checkpoint
    with pytest.raises(_InjectedCrash):
        asyncio.run(first.step_async())
    pointer = first.memory.async_decision
    record = first._async_archive.load(pointer)
    assert client.calls == 0
    assert first._async_lease_from_record(record).inspect_optional() is None

    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    result = asyncio.run(resumed.step_async())
    assert result["action"] == "observe"
    assert client.calls == 1
    assert resumed.memory.async_decision is None
    assert first._async_lease_from_record(record).inspect().state == "consumed"


def test_archive_before_pointer_crash_is_unpaid_orphan_and_resume_is_safe(tmp_path):
    backend = MockBackend()
    client = CountingAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    first._observe()
    first._save()
    store = first._async_archive.store

    def store_then_crash(record):
        store(record)
        raise _InjectedCrash("crash after archive fsync, before checkpoint pointer")

    first._async_archive.store = store_then_crash
    with pytest.raises(_InjectedCrash):
        asyncio.run(first.step_async())
    archives = first._async_archive.records()
    assert len(archives) == 1
    assert first.memory.async_decision is None
    assert first._async_lease_from_record(archives[0]).inspect_optional() is None

    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    result = asyncio.run(resumed.step_async())
    assert client.calls == 1
    assert len(resumed._async_archive.records()) == 2
    assert result["action"] in {"walk_to_coal", "observe"}


def test_rolled_back_reserved_orphan_is_abandoned_only_before_transport(tmp_path):
    class CrashOnceBeforeTransport(CountingAsyncMock):
        async def evaluate(self, state, questions, *, identity, deadline=None,
                           decision_lease=None):
            self.calls += 1
            if self.calls == 1:
                raise _InjectedCrash("crash after WAL reserve, before MAY_HAVE_BEEN_SENT")
            return await AsyncMockJevClient.evaluate(
                self, state, questions, identity=identity, deadline=deadline,
                decision_lease=decision_lease)

    backend = MockBackend()
    client = CrashOnceBeforeTransport()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    with pytest.raises(_InjectedCrash):
        asyncio.run(first.step_async())
    record = first._async_archive.load(first.memory.async_decision)
    lease = first._async_lease_from_record(record)
    assert lease.inspect().state == "reserved"

    # A retained provider-health reservation survives, but the checkpoint
    # pointer is rolled back. Recovery may release it only because the WAL
    # still proves that transport was never authorized.
    first.memory.async_decision = None
    first._save()
    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    result = asyncio.run(resumed.step_async())

    assert result["verified"] is True
    assert client.calls == 2
    assert lease.inspect().state == "failed"
    assert lease.inspect().phase == "not_sent"
    assert backend.tick == 1


@pytest.mark.parametrize("crash_point", ["before_marker", "after_marker_fsync"])
def test_checkpoint_settlement_survives_marker_write_crash_and_repairs_marker(
        tmp_path, crash_point):
    backend = MockBackend()
    client = CountingAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    store_settlement = first._async_archive.store_settlement

    def crash_before_marker(record, entry):
        if crash_point == "after_marker_fsync":
            store_settlement(record, entry)
            raise _InjectedCrash("crash after marker file and directory fsync")
        raise _InjectedCrash("crash after settlement checkpoint, before archive marker")

    first._async_archive.store_settlement = crash_before_marker
    with pytest.raises(_InjectedCrash):
        asyncio.run(first.step_async())
    record = first._async_archive.records()[0]
    assert first.memory.async_decision is None
    assert any(row.get("kind") == "async_decision_settled"
               for row in first.memory.history)
    marker = first._async_archive.load_settlement(record)
    assert marker == (None if crash_point == "before_marker" else
                      next(row for row in first.memory.history
                           if row.get("kind") == "async_decision_settled")
                      ["entries"][0])
    assert first._async_lease_from_record(record).inspect().state == "consumed"
    assert backend.tick == 0

    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    result = asyncio.run(resumed.step_async())
    assert result["verified"] is True
    assert client.calls == 1
    assert resumed._async_archive.load_settlement(record) is not None
    assert backend.tick == 1


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(),
                    reason="descriptor-path fsync fault injection requires procfs")
def test_settlement_marker_file_fsync_failure_recovers_from_checkpoint_lineage(
        tmp_path, monkeypatch):
    backend = MockBackend()
    client = CountingAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    real_fsync = os.fsync
    archive_directory = tmp_path / "campaign.json.safety" / "decision-archives"
    failed = False
    archive_syncs = 0

    def fail_marker_file_once(descriptor):
        nonlocal failed, archive_syncs
        try:
            target = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        except OSError:
            target = None
        if (target is not None and target.parent == archive_directory
                and target.name.startswith(".safety-")):
            archive_syncs += 1
            if not failed and archive_syncs == 2:
                failed = True
                raise OSError("injected settlement marker file fsync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_marker_file_once)
    with pytest.raises(OSError, match="settlement marker file fsync failure"):
        asyncio.run(first.step_async())
    assert failed
    record = first._async_archive.records()[0]
    assert first.memory.async_decision is None
    assert first._async_archive.load_settlement(record) is None
    assert first._async_lease_from_record(record).inspect().state == "consumed"
    assert backend.tick == 0

    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    result = asyncio.run(resumed.step_async())
    assert result["verified"] is True
    assert client.calls == 1
    assert resumed._async_archive.load_settlement(record) is not None
    assert backend.tick == 1


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(),
                    reason="descriptor-path fsync fault injection requires procfs")
def test_pointer_clear_checkpoint_fsync_failure_keeps_selected_response_recoverable(
        tmp_path, monkeypatch):
    backend = MockBackend()
    client = CountingAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    real_save = checkpoint_io.save_checkpoint
    real_fsync = os.fsync
    checkpoint_path = tmp_path / "campaign.json"
    failed = False

    def save_with_one_final_fsync_failure(memory, path):
        nonlocal failed
        has_settlement = any(row.get("kind") == "async_decision_settled"
                             for row in memory.history)
        if failed or memory.async_decision is not None or not has_settlement:
            return real_save(memory, path)

        def fail_checkpoint_temp_once(descriptor):
            nonlocal failed
            try:
                target = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            except OSError:
                target = None
            if (not failed and target is not None and target.parent == checkpoint_path.parent
                    and target.name.startswith(checkpoint_path.name + ".")):
                failed = True
                raise OSError("injected pointer-clear checkpoint file fsync failure")
            return real_fsync(descriptor)

        monkeypatch.setattr(os, "fsync", fail_checkpoint_temp_once)
        try:
            return real_save(memory, path)
        finally:
            monkeypatch.setattr(os, "fsync", real_fsync)

    monkeypatch.setattr(checkpoint_io, "save_checkpoint", save_with_one_final_fsync_failure)
    with pytest.raises(OSError, match="pointer-clear checkpoint file fsync failure"):
        asyncio.run(first.step_async())
    assert failed
    durable = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert durable["async_decision"]["disposition"] == "selected"
    record = first._async_archive.load(durable["async_decision"])
    assert first._async_lease_from_record(record).inspect().state == "consumed"
    assert backend.tick == 0

    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    result = asyncio.run(resumed.step_async())
    assert result["verified"] is True
    assert client.calls == 1
    assert resumed.memory.active_plan is not None
    assert resumed.memory.async_decision is None
    assert resumed._async_archive.load_settlement(record) is not None
    assert backend.tick == 1


def test_settled_marker_without_checkpoint_disposition_fails_closed_after_rollback(tmp_path):
    backend = MockBackend()
    client = CountingAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    result = asyncio.run(first.step_async())
    assert result["verified"] is True
    record = first._async_archive.records()[0]
    assert first._async_archive.load_settlement(record) is not None
    assert first._async_lease_from_record(record).inspect().state == "consumed"
    assert first.memory.active_plan is not None

    # Model rollback to a checkpoint with neither the selected plan nor the
    # retained disposition, while the consumed WAL and marker remain durable.
    first.memory.active_plan = None
    first.memory.async_decision = None
    first.memory.history = [row for row in first.memory.history
                            if row.get("kind") not in {
                                "async_decision_settled", "async_plan_lineage"}]
    first._save()
    with pytest.raises(ValueError, match="marker has no retained checkpoint disposition"):
        resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
        asyncio.run(resumed.step_async())
    assert client.calls == 1
    assert backend.tick == 1


def test_selected_settlement_without_archive_specific_lineage_fails_closed(tmp_path):
    backend = MockBackend()
    client = CountingAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    result = asyncio.run(first.step_async())
    assert result["verified"] is True
    record = first._async_archive.records()[0]
    assert first._async_archive.load_settlement(record)["disposition"] == "selected"

    # Preserve the selected-decision marker but roll the checkpoint back to
    # one that predates the archive-specific selection/attempt disposition.
    first.memory.active_plan = None
    first.memory.step_index = 0
    first.memory.attempt_outcomes = []
    first.memory.history = [row for row in first.memory.history
                            if row.get("kind") != "async_plan_lineage"]
    first._save()
    with pytest.raises(ValueError, match="lacks archive-specific plan lineage"):
        resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
        asyncio.run(resumed.step_async())
    assert client.calls == 1
    assert backend.tick == 1


def test_selected_plan_rechecks_pre_dispatch_failure_as_exact_abandoned_lineage(tmp_path):
    from jev_factorio.skills import Plan, Step

    backend = MockBackend()

    class RemoveCoalAfterSelection(CountingAsyncMock):
        async def evaluate(self, *args, **kwargs):
            result = await super().evaluate(*args, **kwargs)
            backend.coal_left = 0
            return result

    client = RemoveCoalAfterSelection()
    loop, _, _ = _controller(tmp_path, backend=backend, client=client)
    plan = Plan("selected:coal", "stockpile_fuel", "Walk to observed coal",
                (Step("walk_to_coal", "near", "coal"),))
    loop._work_candidates = lambda snapshot: ([plan], "")

    result = asyncio.run(loop.step_async())

    assert result["verified"] is False
    assert backend.tick == 0
    assert client.calls == 1
    assert loop.memory.active_plan is None
    assert loop.memory.pending is None
    lineage = loop._async_plan_lineage_entries()
    assert len(lineage) == 1
    assert lineage[0]["state"] == "abandoned"
    assert lineage[0]["terminal"]["kind"] == "plan_failed"
    assert lineage[0]["terminal"]["attempt"] is None
    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    resumed._observe()
    resumed._async_reconcile_terminal_pointer()
    assert client.calls == 1


def test_ambiguous_selected_dispatch_keeps_active_lineage_and_original_attempt(tmp_path):
    from jev_factorio.skills import Plan, Step

    class LostReplyBackend(MockBackend):
        def act(self, action):
            super().act(action)
            raise ConnectionError("simulated lost dispatch reply after effect")

    backend = LostReplyBackend()
    client = CountingAsyncMock()
    loop, _, _ = _controller(tmp_path, backend=backend, client=client)
    plan = Plan("selected:coal", "stockpile_fuel", "Walk to observed coal",
                (Step("walk_to_coal", "near", "coal"),))
    loop._work_candidates = lambda snapshot: ([plan], "")

    result = asyncio.run(loop.step_async())
    assert result["verified"] is False
    assert "Ambiguous dispatch" in result["outcome"]
    assert backend.tick == 1
    assert client.calls == 1
    assert loop.memory.pending["dispatch"] == "ambiguous"
    original_attempt = dict(loop.memory.attempt)
    lineage = loop._async_plan_lineage_entries()
    assert len(lineage) == 1
    assert lineage[0]["state"] == "active"
    assert lineage[0]["terminal"] is None
    assert lineage[0]["verified_steps"] == []

    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    recovered = asyncio.run(resumed.step_async())
    assert recovered["verified"] is True
    assert recovered["action"] == "verify"
    assert recovered["outcome"] == "Observed expected postcondition"
    assert resumed.memory.pending is None
    assert resumed.memory.attempt is None
    assert resumed.memory.attempt_outcomes[-1]["id"] == original_attempt["id"]
    assert client.calls == 1
    assert backend.tick == 1
    completed = resumed._async_plan_lineage_entries()
    assert completed[0]["state"] == "completed"
    assert completed[0]["verified_steps"][0]["attempt"]["id"] == original_attempt["id"]


def test_retained_settlement_and_active_plan_allow_normal_resume(tmp_path):
    backend = MockBackend()
    client = CountingAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    initial = asyncio.run(first.step_async())
    assert initial["verified"] is True
    record = first._async_archive.records()[0]
    assert first._async_archive.load_settlement(record)["disposition"] == "selected"
    assert first.memory.active_plan is not None

    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    next_step = asyncio.run(resumed.step_async())
    assert next_step["verified"] is True
    assert client.calls == 1
    assert resumed.memory.active_plan is None

    # Once the selected plan is complete, later checkpoints may legitimately
    # omit it; the retained verified step history must reconcile the old
    # settlement before a distinct next-goal request can be made.
    completed_resume, _, _ = _controller(
        tmp_path, backend=backend, client=client, resume=True)
    completion = asyncio.run(completed_resume.step_async())
    assert completion["action"] == "walk_to_iron"
    assert completion["verified"] is True
    assert client.calls == 2
    assert completed_resume.memory.active_plan is not None
    ledgers = [row for row in completed_resume.memory.history
               if row.get("kind") == "async_decision_settled"]
    assert len(ledgers) == 1
    assert len(ledgers[0]["entries"]) == 2

    reloaded, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    following = asyncio.run(reloaded.step_async())
    assert following["verified"] is True
    assert client.calls == 2
    assert len(reloaded.memory.attempt_outcomes) >= 3


def test_repeated_same_plan_archives_remain_bound_to_their_verified_attempts(tmp_path):
    from jev_factorio.skills import Plan, Step

    loop, backend, client = _controller(tmp_path)
    recurring = Plan(
        "recurring:walk", "bootstrap_mining", "Walk to currently observed coal",
        (Step("walk_to_coal", "near", "coal"),),
    )
    loop._work_candidates = lambda snapshot: ([recurring], "")

    async def repeat():
        first = await loop.step_async()
        backend.at_resource = None
        backend.tick += 1
        second = await loop.step_async()
        backend.at_resource = None
        backend.tick += 1
        third = await loop.step_async()
        return first, second, third

    first, second, third = asyncio.run(repeat())

    assert [first["verified"], second["verified"], third["verified"]] == [True, True, True]
    assert client.calls == 3
    assert backend.tick >= 3
    assert len(loop.memory.attempt_outcomes) == 3
    assert len(loop._async_archive.records()) == 3


def test_completed_lineage_does_not_claim_identical_later_active_selection(tmp_path):
    from jev_factorio.skills import Plan, Step

    loop, backend, client = _controller(tmp_path)
    repeated = Plan(
        "recurring:walk", "bootstrap_mining", "Two-step repeated definition",
        (Step("walk_to_coal", "near", "coal"),
         Step("walk_to_iron", "near", "iron-ore")),
    )
    loop._work_candidates = lambda snapshot: ([repeated], "")

    async def run_twice():
        results = [await loop.step_async(), await loop.step_async()]
        backend.at_resource = None
        backend.tick += 1
        results.extend([await loop.step_async(), await loop.step_async()])
        return results

    results = asyncio.run(run_twice())
    assert all(result["verified"] for result in results)
    assert client.calls == 2
    lineages = loop._async_plan_lineage_entries()
    assert len(lineages) == 2
    assert all(entry["state"] == "completed" for entry in lineages)


def test_active_selection_lineage_survives_attempt_history_pruning_on_reload(tmp_path):
    backend = MockBackend()
    client = CountingAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    selected_step = asyncio.run(first.step_async())
    assert selected_step["verified"] is True
    assert first.memory.active_plan is not None
    assert first.memory.step_index == 1
    assert len(first.memory.attempt_outcomes) == 1

    # The archive-specific bounded ledger retains the full exact attempt
    # witness when the ordinary rolling attempt window has pruned it.
    first.memory.attempt_outcomes = []
    first._save()
    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    next_step = asyncio.run(resumed.step_async())
    assert next_step["verified"] is True
    assert client.calls == 1
    assert resumed.memory.attempt_outcomes[-1]["outcome"] == "verified"


def test_completed_old_lineage_survives_same_id_with_new_plan_content(tmp_path):
    from jev_factorio.skills import Plan, Step

    loop, backend, client = _controller(tmp_path)
    original = Plan(
        "recurring:walk", "bootstrap_mining", "Original one-step definition",
        (Step("walk_to_coal", "near", "coal"),),
    )
    replacement = Plan(
        "recurring:walk", "bootstrap_mining", "Later two-step definition",
        (Step("walk_to_coal", "near", "coal"),
         Step("walk_to_iron", "near", "iron-ore")),
    )
    loop._work_candidates = lambda snapshot: ([original], "")

    async def run():
        first = await loop.step_async()
        backend.at_resource = None
        backend.tick += 1
        loop._work_candidates = lambda snapshot: ([replacement], "")
        second = await loop.step_async()
        assert loop.memory.active_plan == replacement.to_dict()
        assert loop.memory.step_index == 1
        third = await loop.step_async()
        return first, second, third

    first, second, third = asyncio.run(run())
    assert [first["verified"], second["verified"], third["verified"]] == [True, True, True]
    assert client.calls == 2
    assert len(loop._async_archive.records()) == 2


def test_reload_rejects_verified_attempt_copied_between_archived_selections(tmp_path):
    from jev_factorio.skills import Plan, Step

    backend = MockBackend()
    client = CountingAsyncMock()
    loop, _, _ = _controller(tmp_path, backend=backend, client=client)
    plan = Plan(
        "recurring:walk", "bootstrap_mining", "Walk to currently observed coal",
        (Step("walk_to_coal", "near", "coal"),),
    )
    loop._work_candidates = lambda snapshot: ([plan], "")

    async def execute_twice():
        first = await loop.step_async()
        backend.at_resource = None
        backend.tick += 1
        second = await loop.step_async()
        return first, second

    first, second = asyncio.run(execute_twice())
    assert first["verified"] and second["verified"]
    lineage = next(row for row in loop.memory.history
                   if row.get("kind") == "async_plan_lineage")
    entries = lineage["entries"]
    assert len(entries) == 2
    entries[1]["verified_steps"] = [entries[0]["verified_steps"][0]]
    encoded = json.dumps(entries, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    lineage["entries_sha256"] = hashlib.sha256(encoded).hexdigest()
    loop._save()

    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    with pytest.raises(ValueError, match="attempt witness is not unique and verified"):
        asyncio.run(resumed.step_async())
    assert client.calls == 2


@pytest.mark.parametrize("crash_boundary", ["before_consume", "after_consume", "after_ack"])
def test_selected_plan_checkpoint_recovers_each_wal_health_finalize_boundary(
        tmp_path, crash_boundary):
    backend = MockBackend()
    client = CountingAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)

    if crash_boundary == "before_consume":
        def crash_before_consume(identity, request, result_sha256):
            raise _InjectedCrash("crash after selected-plan checkpoint")
        first._async_wal.consume_response = crash_before_consume
    elif crash_boundary == "after_consume":
        def crash_before_health_ack(lease):
            raise _InjectedCrash("crash after WAL consume")
        first.jev.acknowledge_decision_consumed = crash_before_health_ack
    else:
        acknowledge = first.jev.acknowledge_decision_consumed

        def crash_after_health_ack(lease):
            acknowledge(lease)
            raise _InjectedCrash("crash after health acknowledgment")
        first.jev.acknowledge_decision_consumed = crash_after_health_ack

    with pytest.raises(_InjectedCrash):
        asyncio.run(first.step_async())

    pointer = first.memory.async_decision
    assert pointer["disposition"] == "selected"
    assert first.memory.active_plan["id"] == pointer["selected_plan_id"]
    saved_record = first._async_archive.load(pointer)
    saved_lease = first._async_lease_from_record(saved_record)
    expected_wal_state = "response_received" if crash_boundary == "before_consume" else "consumed"
    assert saved_lease.inspect().state == expected_wal_state
    assert backend.tick == 0

    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    result = asyncio.run(resumed.step_async())

    assert client.calls == 1
    assert result["action"] == "walk_to_coal"
    assert result["verified"] is True
    assert backend.tick == 1
    assert resumed.memory.async_decision is None
    assert resumed._async_lease_from_record(saved_record).inspect().state == "consumed"


def test_stale_async_response_is_consumed_without_dispatch_or_reselection(tmp_path):
    backend = MockBackend()
    client = CountingAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    _interrupt_before_plan_checkpoint(first)
    with pytest.raises(_InjectedCrash):
        asyncio.run(first.step_async())

    backend.inv["coal"] = 1
    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    result = asyncio.run(resumed.step_async())

    assert client.calls == 1
    assert result["action"] == "observe"
    assert result["decision"]["diagnostics"]["outcome"] == "stale_async_response"
    assert backend.tick == 0
    assert resumed.memory.active_plan is None
    assert resumed.memory.async_decision is None
    archive = json.loads(next(
        (tmp_path / "campaign.json.safety" / "decision-archives").glob("*.json")
    ).read_text())
    assert resumed._async_lease_from_record(archive).inspect().state == "consumed"


def test_tick_only_progress_reduces_archived_response_then_uses_fresh_admission(tmp_path):
    backend = SemanticMockBackend()
    client = CountingAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    _interrupt_before_plan_checkpoint(first)
    with pytest.raises(_InjectedCrash):
        asyncio.run(first.step_async())

    # The game clock advanced while the saved response was pending, but all
    # decision-relevant facts and the source-bound plan remain unchanged.
    backend.tick += 1
    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    result = asyncio.run(resumed.step_async())

    assert client.calls == 1
    assert result["action"] == "walk_to_coal"
    assert result["verified"] is True
    assert backend.tick == 2
    assert resumed.memory.async_decision is None


@pytest.mark.parametrize("change", [
    "site", "provider_model", "source_revision", "request_budget", "deadline", "progress",
])
def test_saved_async_result_is_stale_after_bound_context_change(tmp_path, change):
    backend = SemanticMockBackend()
    client = CountingAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    _interrupt_before_plan_checkpoint(first)
    with pytest.raises(_InjectedCrash):
        asyncio.run(first.step_async())

    if change == "site":
        backend.pos = (1.0, 0.0)
    elif change == "deadline":
        backend.decision_deadline_tick += 1
    elif change == "progress":
        backend.completed_batches += 1
    resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
    if change == "provider_model":
        # Preserve the already-validated provider-health identity, then change
        # the live model before the saved response is admitted.
        client.model = "changed-model"
    elif change == "source_revision":
        resumed.provenance["code_revision"] = {
            "commit": "c" * 40, "source_sha256": "d" * 64,
        }
    elif change == "request_budget":
        resumed.max_request_bytes -= 1
    result = asyncio.run(resumed.step_async())

    assert client.calls == 1
    assert result["action"] == "observe"
    assert result["decision"]["diagnostics"]["outcome"] == "stale_async_response"
    assert backend.tick == 0
    assert resumed.memory.active_plan is None
    assert resumed.memory.async_decision is None


@pytest.mark.parametrize("change", ["actor", "session"])
def test_saved_async_result_rejects_changed_runtime_identity(tmp_path, change):
    backend = IdentifiedMockFLEBackend()
    client = OfflineFLEAsyncMock()
    first, _, _ = _controller(tmp_path, backend=backend, client=client)
    _interrupt_before_plan_checkpoint(first)
    with pytest.raises(_InjectedCrash):
        asyncio.run(first.step_async())

    if change == "actor":
        backend.actor_unit += 1
        backend._native_attachment["actor_unit"] = backend.actor_unit
    else:
        backend.session_id = "session-changed-after-provider-response"
        backend._native_attachment["session_id"] = backend.session_id

    with pytest.raises(ValueError):
        resumed, _, _ = _controller(tmp_path, backend=backend, client=client, resume=True)
        asyncio.run(resumed.step_async())
    assert client.calls == 1
    assert backend.tick == 0


def test_actual_transport_body_matches_durable_archived_request_bytes(tmp_path):
    observed = {}

    def handler(request):
        observed["body"] = request.content
        payload = json.loads(request.content)
        answers = MockJevClient().evaluate(payload["state"], payload["questions"])
        return httpx.Response(200, json={
            "answers": answers, "usage": {"total_tokens": 1},
            "model": "offline-resolved",
        })

    client = AsyncJevClient(
        api_key="offline-test", base_url="https://provider.invalid/evaluate",
        model="offline-model", transport=httpx.MockTransport(handler),
    )
    backend = MockBackend()
    loop = HierarchicalLoop(
        backend, jev=client, policy="jev", target="bootstrap_mining",
        checkpoint=str(tmp_path / "campaign.json"), tick_seconds=0,
        async_decisions=True,
    )
    loop.provenance["code_revision"] = SOURCE

    async def run():
        try:
            result = await loop.step_async()
            assert result["verified"] is True, result.get("decision")
        finally:
            await loop.aclose_async_provider()

    asyncio.run(run())
    archive = json.loads(next(
        (tmp_path / "campaign.json.safety" / "decision-archives").glob("*.json")
    ).read_text())
    archived_body = base64.b64decode(archive["provider"]["wire_body_base64"], validate=True)

    assert observed["body"] == archived_body
    assert hashlib.sha256(observed["body"]).hexdigest() == \
        archive["provider"]["wire_body_sha256"]


def test_cancellation_before_action_admission_waits_for_worker_and_keeps_selected_plan(tmp_path):
    observed = threading.Event()
    release = threading.Event()
    backend = MockBackend()
    client = CountingAsyncMock()
    loop, _, _ = _controller(tmp_path, backend=backend, client=client)
    original_observe = loop._observe

    def gated_observe(phase_name=None):
        snapshot = (original_observe() if phase_name is None
                    else original_observe(phase_name))
        if phase_name == "pre_dispatch_observe":
            observed.set()
            assert release.wait(2)
        return snapshot

    loop._observe = gated_observe

    async def run():
        task = asyncio.create_task(loop.step_async())
        assert await asyncio.to_thread(observed.wait, 2)
        cancellations = await _cancel_three_times_while_worker_is_gated(task)
        task_still_waiting = not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return cancellations, task_still_waiting

    cancellations, task_still_waiting = asyncio.run(run())

    assert cancellations == [True, True, True]
    assert task_still_waiting is True
    assert client.calls == 1
    assert backend.tick == 0
    assert loop.memory.pending is None
    assert loop.memory.attempt is None
    assert loop.memory.active_plan is not None
    assert loop.memory.async_decision is None


def test_cancellation_after_action_entry_waits_for_receipt_and_verification(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class GatedBackend(MockBackend):
        def act(self, action):
            entered.set()
            assert release.wait(2)
            return super().act(action)

    backend = GatedBackend()
    client = CountingAsyncMock()
    loop, _, _ = _controller(tmp_path, backend=backend, client=client)

    async def run():
        task = asyncio.create_task(loop.step_async())
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    assert client.calls == 1
    assert backend.tick == 1
    assert loop.memory.pending is None
    assert loop.memory.attempt is None
    assert loop.memory.attempt_outcomes[-1]["outcome"] == "verified"


async def _cancel_three_times_while_worker_is_gated(task):
    observations = []
    for _ in range(3):
        task.cancel()
        await asyncio.sleep(0.02)
        observations.append(not task.done())
    return observations


def test_repeated_cancellation_keeps_actor_and_worker_owned_until_action_finishes(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class GatedBackend(MockBackend):
        def act(self, action):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test did not release the admitted action")
            return super().act(action)

    backend = GatedBackend()
    client = CountingAsyncMock()
    loop, _, _ = _controller(tmp_path, backend=backend, client=client)

    async def run():
        task = asyncio.create_task(loop.step_async())
        assert await asyncio.to_thread(entered.wait, 2)
        execution = loop._async_execution
        assert execution is not None
        cancellation_observations = await _cancel_three_times_while_worker_is_gated(task)
        task_still_waiting = not task.done()
        execution_still_owned = loop._async_execution is execution
        worker_still_running = not execution["worker"].done()
        second_controller_rejected = False
        provider_close_rejected = False
        second = None
        if task_still_waiting:
            secondary_dir = tmp_path / "secondary"
            secondary_dir.mkdir()
            second, _, _ = _controller(secondary_dir, backend=backend)
            try:
                await second.step_async()
            except AsyncControllerBusy:
                second_controller_rejected = True
            try:
                await loop.aclose_async_provider()
            except AsyncControllerBusy:
                provider_close_rejected = True
        release.set()
        try:
            await task
            caller_cancelled = False
        except asyncio.CancelledError:
            caller_cancelled = True
        sequential_result = await second.step_async() if second is not None else None
        return (cancellation_observations, task_still_waiting,
                execution_still_owned, worker_still_running,
                second_controller_rejected, provider_close_rejected,
                caller_cancelled, sequential_result)

    (observations, task_waiting, execution_owned, worker_running,
     second_rejected, close_rejected, caller_cancelled,
     sequential_result) = asyncio.run(run())

    assert observations == [True, True, True]
    assert task_waiting is True
    assert execution_owned is True
    assert worker_running is True
    assert second_rejected is True
    assert close_rejected is True
    assert caller_cancelled is True
    assert client.calls == 1
    assert sequential_result is not None and sequential_result["verified"] is True
    assert backend.tick == 2
    assert loop.memory.pending is None
    assert loop.memory.attempt is None
    assert loop.memory.attempt_outcomes[-1]["outcome"] == "verified"
    assert loop._async_execution is None


@pytest.mark.parametrize("phase", ["after_send", "after_response"])
def test_repeated_cancellation_during_provider_phase_keeps_worker_owned(tmp_path, phase):
    entered = threading.Event()
    cancel_seen = threading.Event()

    async def run():
        release = asyncio.Event()

        class GatedProvider(CountingAsyncMock):
            async def evaluate(self, state, questions, *, identity, deadline=None,
                               decision_lease=None):
                self.calls += 1
                if phase == "after_send":
                    decision_lease.mark_may_have_been_sent()
                    response = None
                else:
                    response = await AsyncMockJevClient.evaluate(
                        self, state, questions, identity=identity,
                        deadline=deadline, decision_lease=decision_lease)
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancel_seen.set()
                    await release.wait()
                    raise
                return response

        backend = MockBackend()
        client = GatedProvider()
        loop, _, _ = _controller(tmp_path, backend=backend, client=client)
        task = asyncio.create_task(loop.step_async())
        assert await asyncio.to_thread(entered.wait, 2)
        execution = loop._async_execution
        assert execution is not None
        if phase == "after_response":
            record = loop._async_archive.records()[0]
            assert loop._async_lease_from_record(record).inspect().state == "response_received"
        cancellations = await _cancel_three_times_while_worker_is_gated(task)
        task_still_waiting = not task.done()
        worker_still_running = not execution["worker"].done()
        execution_still_owned = loop._async_execution is execution
        provider_cancellation_delivered = cancel_seen.is_set()
        close_rejected = False
        if task_still_waiting:
            try:
                await loop.aclose_async_provider()
            except AsyncControllerBusy:
                close_rejected = True
        release.set()
        try:
            await task
            caller_cancelled = False
        except asyncio.CancelledError:
            caller_cancelled = True
        record = loop._async_archive.records()[0]
        wal_state = loop._async_lease_from_record(record).inspect().state
        pointer = loop.memory.async_decision
        return (backend, client, loop, cancellations, task_still_waiting,
                worker_still_running, execution_still_owned,
                provider_cancellation_delivered, close_rejected,
                caller_cancelled, wal_state, pointer)

    (backend, client, loop, cancellations, task_waiting, worker_running,
     execution_owned, cancel_delivered, close_rejected, caller_cancelled,
     wal_state, pointer) = asyncio.run(run())

    assert cancellations == [True, True, True]
    assert task_waiting is True
    assert worker_running is True
    assert execution_owned is True
    assert cancel_delivered is True
    assert close_rejected is True
    assert caller_cancelled is True
    assert client.calls == 1
    assert backend.tick == 0
    if phase == "after_send":
        assert wal_state == "may_have_been_sent"
        assert pointer is not None and pointer["disposition"] == "pending"
    else:
        assert wal_state == "consumed"
        assert pointer is None


@pytest.mark.parametrize("phase", ["before_send", "after_send", "after_response"])
def test_cancellation_preserves_the_exact_provider_phase_and_prevents_action(tmp_path, phase):
    entered = threading.Event()

    class PhaseGateClient(CountingAsyncMock):
        async def evaluate(self, state, questions, *, identity, deadline=None,
                           decision_lease=None):
            self.calls += 1
            if phase == "after_send":
                decision_lease.mark_may_have_been_sent()
            if phase == "after_response":
                await AsyncMockJevClient.evaluate(
                    self,
                    state, questions, identity=identity, deadline=deadline,
                    decision_lease=decision_lease)
            entered.set()
            await asyncio.Event().wait()

    async def run():
        backend = MockBackend()
        client = PhaseGateClient()
        loop, _, _ = _controller(tmp_path, backend=backend, client=client)
        task = asyncio.create_task(loop.step_async())
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        pointer_after_cancel = (dict(loop.memory.async_decision)
                                if loop.memory.async_decision is not None else None)
        reconciled = await loop.step_async() if phase == "after_send" else None
        return loop, backend, client, reconciled, pointer_after_cancel

    loop, backend, client, reconciled, pointer_after_cancel = asyncio.run(run())

    assert client.calls == 1
    assert backend.tick == 0
    if phase == "before_send":
        assert loop.memory.async_decision is None
        assert loop.jev.state.get("in_flight") is None
        wal_record = loop._async_lease_from_record(
            json.loads(next(
                (tmp_path / "campaign.json.safety" / "decision-archives").glob("*.json")
            ).read_text())
        ).inspect()
        assert wal_record.state == "failed" and wal_record.phase == "not_sent"
        archived = loop._async_archive.records()[0]
        settlement = loop._async_archive.load_settlement(archived)
        assert settlement["disposition"] == "cancelled_before_send"
        assert any(row.get("kind") == "async_decision_settled"
                   for row in loop.memory.history)
        resumed_client = CountingAsyncMock()
        resumed, _, _ = _controller(
            tmp_path, backend=backend, client=resumed_client, resume=True)
        resumed_result = asyncio.run(resumed.step_async())
        assert resumed_result["action"] in {"walk_to_coal", "observe"}
        assert resumed_client.calls == 1
    elif phase == "after_send":
        assert pointer_after_cancel["disposition"] == "pending"
        assert reconciled is not None
        assert reconciled["action"] == "observe"
        assert loop.memory.async_decision["disposition"] == "no_action"
        archived = loop._async_archive.load(loop.memory.async_decision)
        assert loop._async_lease_from_record(archived).inspect().state == "ambiguous"
        assert loop.jev.state["phase"] == "exhausted"
        assert loop._async_lease_from_record(archived).inspect().state == "ambiguous"
    else:
        assert loop.memory.async_decision is None
        archived = json.loads(next(
            (tmp_path / "campaign.json.safety" / "decision-archives").glob("*.json")
        ).read_text())
        assert loop._async_lease_from_record(archived).inspect().state == "consumed"
        assert loop.jev.state["decision_outcome"]["state"] == "consumed"
