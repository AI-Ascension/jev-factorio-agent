"""Bounded real process exclusion; offline fixture ownership only."""
from contextlib import ExitStack
import multiprocessing
import subprocess
import sys
import threading

import pytest

from jev_factorio.attempt_identity_archive import (
    AttemptIdentityArchive, AttemptIdentityBusy, AttemptIdentityError,
)
from jev_factorio.memory import CampaignMemory


def _fork_probe(archive, connection):
    try:
        try:
            archive._require_owner()
            inherited = "admitted"
        except AttemptIdentityError:
            inherited = "rejected"
        try:
            with archive.owner():
                exclusion = "admitted"
        except AttemptIdentityBusy:
            exclusion = "busy"
        connection.send((inherited, exclusion))
        if not connection.poll(5) or connection.recv() != "parent_released":
            raise AssertionError("bounded parent release deadline")
        with archive.owner():
            memory = archive.validate_current(CampaignMemory, "bootstrap_mining")
            connection.send(("restored", memory.last_tick))
    except BaseException as error:
        connection.send(("error", type(error).__name__, str(error)))
    finally:
        connection.close()


@pytest.mark.parametrize("other_thread", [False, True])
def test_fork_does_not_inherit_owner_and_reloads_after_parent_release(tmp_path, other_thread):
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("Actual fork control requires POSIX; spawn process control is separate")
    archive = AttemptIdentityArchive(tmp_path / "checkpoint.json")
    memory = CampaignMemory("fork-owner-session", "bootstrap_mining")
    ready, release = threading.Event(), threading.Event()
    failures = []

    def save():
        token = archive.before_checkpoint(memory)
        memory.save(archive.checkpoint)
        archive.after_checkpoint(token, memory)

    def thread_owner():
        try:
            with archive.owner():
                save()
                ready.set()
                assert release.wait(5), "bounded thread release deadline"
                memory.last_tick = 17
                save()
        except BaseException as error:
            failures.append(error)
            ready.set()

    thread = None
    child = None
    parent_connection = None
    observations = None
    try:
        with ExitStack() as stack:
            if other_thread:
                thread = threading.Thread(target=thread_owner)
                thread.start()
                assert ready.wait(5) and not failures
            else:
                stack.enter_context(archive.owner())
                save()
            original_checkpoint, original_journal = (
                archive.checkpoint.read_bytes(), archive.path.read_bytes())
            context = multiprocessing.get_context("fork")
            parent_connection, child_connection = context.Pipe()
            child = context.Process(target=_fork_probe, args=(archive, child_connection))
            child.start()
            child_connection.close()
            assert parent_connection.poll(5), "bounded fork observation deadline"
            observations = parent_connection.recv()
            assert archive.checkpoint.read_bytes() == original_checkpoint
            assert archive.path.read_bytes() == original_journal
            if not other_thread:
                memory.last_tick = 17
                save()
        release.set()
        if thread:
            thread.join(5)
            assert not thread.is_alive() and not failures
        parent_connection.send("parent_released")
        assert parent_connection.poll(5), "bounded child restore deadline"
        restored = parent_connection.recv()
        child.join(5)
        assert not child.is_alive() and child.exitcode == 0
        assert observations == ("rejected", "busy")
        assert restored == ("restored", 17)
    finally:
        release.set()
        if thread:
            thread.join(5)
        if child and child.is_alive():
            child.terminate()
            child.join(5)
        if parent_connection:
            parent_connection.close()


def test_fresh_interpreter_obeys_os_lock_and_can_acquire_after_release(tmp_path):
    archive = AttemptIdentityArchive(tmp_path / "checkpoint.json")
    probe = """
import sys
from jev_factorio.attempt_identity_archive import AttemptIdentityArchive, AttemptIdentityBusy
archive = AttemptIdentityArchive(sys.argv[1])
try:
    with archive.owner():
        print('acquired')
except AttemptIdentityBusy:
    print('busy')
"""

    def run():
        result = subprocess.run([sys.executable, "-B", "-c", probe, str(archive.checkpoint)],
                                capture_output=True, text=True, timeout=10, check=True)
        assert result.stderr == ""
        return result.stdout.strip()

    with archive.owner():
        assert run() == "busy"
    assert run() == "acquired"
    assert not archive.checkpoint.exists() and not archive.path.exists()


@pytest.mark.parametrize("body_error", [False, True])
def test_inherited_context_unwind_does_not_release_parent_lock(tmp_path, body_error):
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("Actual inherited-context unwind requires POSIX fork")
    # A private interpreter permits the child to unwind the actual suspended
    # owner context rather than multiprocessing's immediate exit shortcut.
    probe = r'''
import os, subprocess, sys
from jev_factorio.attempt_identity_archive import AttemptIdentityArchive, AttemptIdentityError
archive = AttemptIdentityArchive(sys.argv[1])
reader, writer = os.pipe()
child = False
try:
    try:
        with archive.owner():
            pid = os.fork()
            if pid == 0:
                child = True
                os.close(reader)
                try:
                    archive._require_owner()
                except AttemptIdentityError:
                    pass
                else:
                    raise AssertionError('child inherited operation authority')
                if sys.argv[2] == 'True':
                    raise RuntimeError('controlled child body failure')
            else:
                os.close(writer)
                message = os.read(reader, 4096)
                assert message == b'unwound', message
                _, status = os.waitpid(pid, 0)
                assert status == 0, status
                fresh = subprocess.run([sys.executable, '-B', '-c', """
import sys
from jev_factorio.attempt_identity_archive import AttemptIdentityArchive, AttemptIdentityBusy
try:
    with AttemptIdentityArchive(sys.argv[1]).owner():
        print('admitted')
except AttemptIdentityBusy:
    print('busy')
""", sys.argv[1]], capture_output=True, text=True, timeout=5, check=True)
                assert fresh.stdout.strip() == 'busy', fresh.stdout
    except RuntimeError as error:
        assert child and str(error) == 'controlled child body failure', repr(error)
    if child:
        os.write(writer, b'unwound')
    else:
        print('parent lock preserved')
finally:
    if child:
        os._exit(0)
'''
    result = subprocess.run([sys.executable, "-B", "-c", probe,
                             str(tmp_path / "checkpoint.json"), str(body_error)],
                            capture_output=True, text=True, timeout=15, check=True)
    assert result.stderr == "" and result.stdout.strip() == "parent lock preserved"
