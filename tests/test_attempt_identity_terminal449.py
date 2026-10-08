"""Retain original attempt and first result across terminal checkpoint crashes."""
import hashlib
import json
from copy import deepcopy

import pytest

from attempt_helpers import ReceiptBackend, controller, install_plan
from jev_factorio.attempt_identity_archive import AttemptIdentityArchive, AttemptIdentityError


@pytest.mark.parametrize('boundary', ['before_checkpoint', 'before_bound', 'before_settled'])
@pytest.mark.parametrize('replace_identity', [False, True])
def test_public_terminal_write_crash_retains_result_without_replay(
        tmp_path, monkeypatch, boundary, replace_identity):
    backend = ReceiptBackend()
    install_plan(monkeypatch, backend)
    loop = controller(tmp_path, backend)
    original_save = loop.memory_type.save
    original_append = AttemptIdentityArchive._append
    def save(memory, *args, **kwargs):
        if boundary == 'before_checkpoint' and memory.attempt_outcomes:
            raise OSError('controlled terminal checkpoint crash')
        return original_save(memory, *args, **kwargs)
    def append(archive, transition, memory, **kwargs):
        if (memory.attempt_outcomes and
                ((boundary == 'before_bound' and transition == 'checkpoint_bound')
                 or (boundary == 'before_settled' and transition == 'settled'))):
            raise OSError('controlled terminal checkpoint crash')
        return original_append(archive, transition, memory, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(loop.memory_type, 'save', save)
        patch.setattr(AttemptIdentityArchive, '_append', append)
        with pytest.raises(OSError, match='controlled terminal checkpoint crash'):
            loop.step()
    assert len(backend.calls) == 1
    original_calls = deepcopy(backend.calls)
    native_state = deepcopy(backend.state)
    path, archive = loop.checkpoint, loop._attempt_identity_archive
    checkpoint = path.read_bytes()
    rows = [json.loads(line) for line in archive.path.read_bytes().splitlines()]
    finished = next(row['data']['finished'] for row in rows if row['transition'] == 'terminal_intent')
    original_id = finished['id']
    if replace_identity:
        raw = json.loads(checkpoint)
        replacement = 'f' * 32 if original_id != 'f' * 32 else 'e' * 32
        if boundary == 'before_checkpoint':
            assert raw['attempt']['id'] == original_id
            raw['attempt']['id'] = replacement
        else:
            assert raw['attempt_outcomes'][0]['id'] == original_id
            raw['attempt_outcomes'][0]['id'] = replacement
        path.write_text(json.dumps(raw, sort_keys=True))
        checkpoint = path.read_bytes()
        # Rehash the interrupted transaction's claimed old/new bytes as well:
        # a valid JSON/hash chain cannot create original-attempt authority.
        if rows[-1]['transition'] == 'checkpoint_intent':
            key = 'previous_sha256' if boundary == 'before_checkpoint' else 'checkpoint_sha256'
            rows[-1]['data'][key] = hashlib.sha256(checkpoint).hexdigest()
            canonical = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'),
                ensure_ascii=False, allow_nan=False).encode()
            rows[-1]['record_sha256'] = hashlib.sha256(canonical({
                k: v for k, v in rows[-1].items() if k != 'record_sha256'})).hexdigest()
            archive.path.write_bytes(b''.join(canonical(row) + b'\n' for row in rows))
    journal = archive.path.read_bytes()
    if replace_identity:
        for _ in range(2):
            with pytest.raises(AttemptIdentityError):
                controller(tmp_path, backend, resume=True)
            assert path.read_bytes() == checkpoint
            assert archive.path.read_bytes() == journal
            assert backend.calls == original_calls
            assert backend.state == native_state
    else:
        restored = controller(tmp_path, backend, resume=True)
        reconciled = archive.path.read_bytes()
        controller(tmp_path, backend, resume=True)
        assert archive.path.read_bytes() == reconciled and path.read_bytes() == checkpoint
        if boundary == 'before_checkpoint':
            restored.step()
        final = json.loads(path.read_bytes())
        assert final['attempt'] is final['pending'] is None
        assert final['attempt_outcomes'] == [finished]
        assert backend.calls == original_calls
        assert backend.state == native_state
        terminal_rows = [json.loads(line) for line in archive.path.read_bytes().splitlines()]
        assert sum(row['transition'] == 'terminal_intent' and row['attempt_id'] == original_id
                   for row in terminal_rows) == 1
        assert sum(row['transition'] == 'settled' and row['attempt_id'] == original_id
                   for row in terminal_rows) == 1
        controller(tmp_path, backend, resume=True)
        assert final == json.loads(path.read_bytes())


@pytest.mark.parametrize('expected_digest', [None, False, 'not-a-digest'])
def test_incomplete_expected_write_digest_rejected_without_journal_mutation(
        tmp_path, monkeypatch, expected_digest):
    backend = ReceiptBackend()
    install_plan(monkeypatch, backend)
    loop = controller(tmp_path, backend)
    original_save = loop.memory_type.save
    def save(memory, *args, **kwargs):
        if memory.attempt_outcomes:
            raise OSError('controlled terminal checkpoint crash')
        return original_save(memory, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(loop.memory_type, 'save', save)
        with pytest.raises(OSError, match='controlled terminal checkpoint crash'):
            loop.step()
    assert len(backend.calls) == 1
    checkpoint, journal_path = loop.checkpoint.read_bytes(), loop._attempt_identity_archive.path
    rows = [json.loads(line) for line in journal_path.read_bytes().splitlines()]
    assert rows[-1]['transition'] == 'checkpoint_intent'
    rows[-1]['data']['checkpoint_sha256'] = expected_digest
    canonical = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=False, allow_nan=False).encode()
    rows[-1]['record_sha256'] = hashlib.sha256(canonical({
        k: v for k, v in rows[-1].items() if k != 'record_sha256'})).hexdigest()
    journal_path.write_bytes(b''.join(canonical(row) + b'\n' for row in rows))
    journal = journal_path.read_bytes()
    for _ in range(2):
        with pytest.raises(AttemptIdentityError):
            controller(tmp_path, backend, resume=True)
        assert loop.checkpoint.read_bytes() == checkpoint
        assert journal_path.read_bytes() == journal
        assert len(backend.calls) == 1
