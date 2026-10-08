"""The production controller composition must reach the retained verifier."""
from copy import deepcopy
from dataclasses import asdict
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from jev_factorio.background import BackgroundWorkLoop
from jev_factorio.attempt_identity_archive import AttemptIdentityArchive, AttemptIdentityError
from jev_factorio.buffer_controller import buffered_loop_type
from jev_factorio.input_controller import input_loop_type
from jev_factorio.outpost_controller import outpost_loop_type
from jev_factorio.planning.catalog import Catalog
from jev_factorio.main import _preflight_selected_checkpoint
from test_shared_paid_connectors import FIXTURE, Pages, retained


def composed(tmp_path, *, approve_legacy=True, replace_attempt=False):
    kind = outpost_loop_type(input_loop_type(buffered_loop_type(BackgroundWorkLoop)))
    memory, snapshot, _ = retained()
    memory = kind.memory_type(**asdict(memory))
    path = tmp_path / 'controller.json'
    memory.save(path)
    # This retained V24 checkpoint predates the independent journal. Approve
    # its exact private fixture bytes once; ordinary restore must never do so.
    original_capture = path.read_bytes()
    archive = AttemptIdentityArchive(path)
    if approve_legacy:
        with archive.owner():
            approval = archive.inspect_legacy_migration(
                kind.memory_type, memory.target, memory.session_id)
            archive.authorize_legacy_migration(
                kind.memory_type, memory.target, memory.session_id,
                approval['checkpoint_sha256'], approval['approved_attempts_sha256'])
        assert path.read_bytes() == original_capture
    if replace_attempt:
        changed = json.loads(original_capture)
        changed['attempt']['id'] = uuid4().hex
        path.write_text(json.dumps(changed))
    # Exercise the CLI's selected composition before backend construction.
    selected = path.read_bytes()
    journal = archive.path.read_bytes() if archive.path.exists() else None
    try:
        _preflight_selected_checkpoint(path, kind.memory_type, memory.target)
    except AttemptIdentityError:
        assert path.read_bytes() == selected
        assert (archive.path.read_bytes() if archive.path.exists() else None) == journal
        raise
    catalog = Catalog.from_dict(json.loads(
        FIXTURE.with_name('native-v22-capital-power.json').read_text())['catalog'])

    def forbidden(*args, **kwargs):
        pytest.fail('Retained verification must not dispatch or request a new decision')

    backend = SimpleNamespace(craft_jobs_supported=True, output_buffers_supported=True,
        input_routes_supported=True, mining_outposts_supported=True,
        enable_factory=lambda: catalog, observe=lambda: deepcopy(snapshot), execute=forbidden)
    loop = kind(backend, SimpleNamespace(is_mock=False, decide=forbidden, query=forbidden),
                policy='jev', factory_scheduling='ready-work', checkpoint=str(path),
                resume_controller=True, tick_seconds=.01)
    # Replay only the retained native detail pages, with no adapter installation.
    backend._factory = Pages(memory.connector_ownership)
    return loop, memory, snapshot


def test_full_production_composition_resumes_and_verifies_without_redispatch(tmp_path):
    loop, before, _ = composed(tmp_path)
    loop.run(steps=1)
    assert loop.memory.pending is None and loop.memory.attempt is None
    assert loop.memory.connector_ownership == before.connector_ownership
    outcome = next(row for row in loop.memory.attempt_outcomes if row['id'] == before.attempt['id'])
    assert outcome['outcome'] == 'verified'
    assert not loop.memory.reservations


def test_composed_legacy_approval_is_one_use_and_cannot_replace_existing_journal(tmp_path):
    loop, memory, _ = composed(tmp_path)
    path = tmp_path / 'controller.json'
    archive = AttemptIdentityArchive(path)
    checkpoint_before, journal_before = path.read_bytes(), archive.path.read_bytes()
    approval = json.loads(journal_before.splitlines()[0])['data']
    with archive.owner(), pytest.raises(AttemptIdentityError):
        archive.authorize_legacy_migration(
            loop.memory_type, memory.target, memory.session_id,
            approval['checkpoint_sha256'], approval['approved_attempts_sha256'])
    assert path.read_bytes() == checkpoint_before
    assert archive.path.read_bytes() == journal_before


@pytest.mark.parametrize('fault', ['_capital_fault', '_buffer_fault', '_buffer_save_poisoned',
                                  '_input_fault', '_outpost_fault', '_save_poisoned'])
def test_shared_connector_does_not_bypass_composed_faults(tmp_path, fault):
    loop, memory, snapshot = composed(tmp_path)
    loop.memory = memory
    setattr(loop, fault, True)
    assert loop._execution_barrier(snapshot)
    assert loop.memory.pending == memory.pending


@pytest.mark.parametrize('approve_legacy,replace_attempt', [(False, False), (True, True)])
def test_composed_cli_preflight_rejects_unapproved_or_replaced_identity(
        tmp_path, approve_legacy, replace_attempt):
    with pytest.raises(AttemptIdentityError):
        composed(tmp_path, approve_legacy=approve_legacy, replace_attempt=replace_attempt)
    path = tmp_path / 'controller.json'
    # Repeated public preflight cannot repair or approve the rejected capture.
    before = path.read_bytes()
    archive = AttemptIdentityArchive(path)
    journal_before = archive.path.read_bytes() if archive.path.exists() else None
    kind = outpost_loop_type(input_loop_type(buffered_loop_type(BackgroundWorkLoop)))
    with pytest.raises(AttemptIdentityError):
        _preflight_selected_checkpoint(path, kind.memory_type, 'rocket_launch')
    assert path.read_bytes() == before
    assert (archive.path.read_bytes() if archive.path.exists() else None) == journal_before
