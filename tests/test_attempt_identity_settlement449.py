"""Settlement binds its first result to the checkpoint installed at that time."""
import hashlib
import json
from copy import deepcopy

import pytest

from attempt_helpers import ReceiptBackend, controller, install_plan
from jev_factorio.attempt_identity_archive import AttemptIdentityError


@pytest.mark.parametrize('change', [None, 'outcome_digest', 'checkpoint_digest', 'older_checkpoint'])
@pytest.mark.parametrize('later_checkpoint', [False, True])
def test_public_restore_checks_exact_settlement_bindings(tmp_path, monkeypatch, change, later_checkpoint):
    backend = ReceiptBackend()
    install_plan(monkeypatch, backend)
    loop = controller(tmp_path, backend)
    assert loop.step()['verified']
    original_outcomes = deepcopy(loop.memory.attempt_outcomes)
    original_calls, original_state = deepcopy(backend.calls), deepcopy(backend.state)
    journal_path = loop._attempt_identity_archive.path
    rows = [json.loads(line) for line in journal_path.read_bytes().splitlines()]
    settlement = next(row for row in rows if row['transition'] == 'settled')
    settled_digest = settlement['data']['checkpoint_sha256']
    assert settled_digest == hashlib.sha256(loop.checkpoint.read_bytes()).hexdigest()
    if later_checkpoint:
        # A subsequent owned checkpoint may legitimately differ from the one
        # that originally settled this result; validate against its historical binding.
        loop.memory.last_tick += 1
        loop._save()
        assert hashlib.sha256(loop.checkpoint.read_bytes()).hexdigest() != settled_digest
        rows = [json.loads(line) for line in journal_path.read_bytes().splitlines()]
        settlement = next(row for row in rows if row['transition'] == 'settled')
    if change:
        if change == 'older_checkpoint':
            older = next(row['data']['checkpoint_sha256'] for row in rows
                         if row['transition'] == 'checkpoint_bound'
                         and row['data']['checkpoint_sha256'] != settled_digest)
            settlement['data']['checkpoint_sha256'] = older
        else:
            key = 'outcome_sha256' if change == 'outcome_digest' else 'checkpoint_sha256'
            settlement['data'][key] = 'a' * 64
        canonical = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'),
            ensure_ascii=False, allow_nan=False).encode()
        previous = None
        for row in rows:
            row['previous_sha256'] = previous
            row['record_sha256'] = hashlib.sha256(canonical({
                k: v for k, v in row.items() if k != 'record_sha256'})).hexdigest()
            previous = row['record_sha256']
        journal_path.write_bytes(b''.join(canonical(row) + b'\n' for row in rows))
    checkpoint, journal = loop.checkpoint.read_bytes(), journal_path.read_bytes()
    for _ in range(2):
        if change:
            with pytest.raises(AttemptIdentityError):
                controller(tmp_path, backend, resume=True)
        else:
            restored = controller(tmp_path, backend, resume=True)
            # Controller construction preflights the journal; runtime memory is
            # loaded lazily. Exercise the same public archive reader explicitly.
            archive = restored._attempt_identity_archive
            with archive.owner():
                memory = archive.validate_current(restored.memory_type, restored.target)
            assert memory.attempt_outcomes == original_outcomes
            assert memory.attempt is memory.pending is None
        assert loop.checkpoint.read_bytes() == checkpoint
        assert journal_path.read_bytes() == journal
        assert backend.calls == original_calls and backend.state == original_state
