"""Real local locks and synthetic public dispatch; no native acceptance claim."""
import os
import threading

import pytest

from jev_factorio.attempt_identity_archive import (
    AttemptIdentityArchive, AttemptIdentityBusy, _OWNER_LOCAL,
)
from jev_factorio.memory import CampaignMemory


@pytest.mark.parametrize("fault", [
    "body", "interrupt", "unlock", "close_body", "close_normal",
])
def test_failed_owner_releases_authority_and_excludes_next_owner(tmp_path, monkeypatch, fault):
    if os.name != "posix":
        pytest.skip("POSIX cleanup fault controls use flock; Windows lock control separate")
    import fcntl

    archive = AttemptIdentityArchive(tmp_path / "checkpoint.json")
    key = os.path.normcase(os.path.abspath(archive.lock_path))
    real_close, real_flock = os.close, fcntl.flock
    descriptor = None

    def close_failed(current):
        real_close(current)
        if current == descriptor:
            raise OSError("controlled close failure after descriptor closure")

    def unlock_failed(current, flags):
        if current == descriptor and flags == fcntl.LOCK_UN:
            raise OSError("controlled unlock failure")
        return real_flock(current, flags)

    expected = KeyboardInterrupt if fault == "interrupt" else (
        OSError if fault == "close_normal" else RuntimeError)
    with pytest.raises(expected) as raised, monkeypatch.context() as patch:
        with archive.owner():
            descriptor = _OWNER_LOCAL.held[key]["descriptor"]
            if fault == "unlock":
                patch.setattr(fcntl, "flock", unlock_failed)
            elif fault.startswith("close_"):
                patch.setattr(os, "close", close_failed)
            if fault == "interrupt":
                raise KeyboardInterrupt("controlled interruption")
            if fault != "close_normal":
                raise RuntimeError("controlled owner failure")
    if fault in {"unlock", "close_body"}:
        assert str(raised.value) == "controlled owner failure"
        assert isinstance(raised.value.__cause__, OSError)

    acquired, release = threading.Event(), threading.Event()
    failures = []

    def competitor():
        try:
            with archive.owner():
                acquired.set()
                if not release.wait(3):
                    raise AssertionError("bounded competitor release expired")
        except BaseException as error:
            failures.append(error)
            acquired.set()

    worker = threading.Thread(target=competitor)
    worker.start()
    try:
        assert acquired.wait(3), "competitor acquisition deadline"
        assert not failures
        with pytest.raises(AttemptIdentityBusy):
            with archive.owner():
                pytest.fail("failed prior owner bypassed the real competitor lock")
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and not failures
    assert not archive.path.exists(), "lock controls must not create paid journal records"


@pytest.mark.parametrize("inner_failure", [False, True])
def test_nested_owner_preserves_outer_durable_checkpoint_authority(tmp_path, inner_failure):
    archive = AttemptIdentityArchive(tmp_path / "checkpoint.json")
    memory = CampaignMemory("owner-session", "bootstrap_mining")
    with archive.owner():
        try:
            with archive.owner():
                if inner_failure:
                    raise RuntimeError("controlled inner owner failure")
        except RuntimeError:
            assert inner_failure
        token = archive.before_checkpoint(memory)
        memory.save(archive.checkpoint)
        archive.after_checkpoint(token, memory)
    with archive.owner():
        restored = archive.validate_current(CampaignMemory, memory.target)
        assert restored.session_id == memory.session_id
        assert restored.pending is None and restored.attempt is None


def test_background_public_step_journals_dispatch_under_outer_owner(tmp_path):
    import json
    from test_background_work import ReceiptBackend, controller

    backend = ReceiptBackend()
    loop = controller(backend, tmp_path)
    observed = []
    original = backend.execute

    def execute(action, parameters):
        observed.append(action)
        rows = [json.loads(line) for line in loop._attempt_identity_archive.path.read_text().splitlines()]
        assert rows[-1]["transition"] == "dispatch_started"
        assert rows[-1]["attempt_id"] == loop.memory.attempt["id"]
        assert loop._attempt_identity_owner is loop._attempt_identity_archive
        return original(action, parameters)

    backend.execute = execute
    loop.step()
    assert observed == ["factory_craft_job"]
    rows = [json.loads(line) for line in loop._attempt_identity_archive.path.read_text().splitlines()]
    assert [r["transition"] for r in rows if r["transition"].startswith("dispatch_")] == [
        "dispatch_started", "dispatch_returned",
    ]
    assert loop.memory.background_attempt is not None
    assert loop.memory.background_attempt["id"] == next(
        r["attempt_id"] for r in rows if r["transition"] == "dispatch_started")
