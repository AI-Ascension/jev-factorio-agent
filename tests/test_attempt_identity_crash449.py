"""Public restore controls at journal/checkpoint crash boundaries, offline only."""
import json
import hashlib

import pytest

from attempt_helpers import ReceiptBackend, controller, install_plan
from jev_factorio.attempt_identity_archive import AttemptIdentityArchive, AttemptIdentityError


@pytest.mark.parametrize("change", [None, "uuid", "journal_session", "journal_target"])
def test_restore_after_bound_checkpoint_before_prepared_append(tmp_path, monkeypatch, change):
    backend = ReceiptBackend("delayed")
    install_plan(monkeypatch, backend)
    loop = controller(tmp_path, backend)
    original_append = AttemptIdentityArchive._append

    def interrupted(archive, transition, *args, **kwargs):
        if transition == "attempt_prepared":
            raise OSError("controlled failure before prepared journal append")
        return original_append(archive, transition, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(AttemptIdentityArchive, "_append", interrupted)
        with pytest.raises(OSError, match="before prepared journal append"):
            loop.step()
    assert backend.calls == []
    path = loop.checkpoint
    raw = path.read_bytes()
    saved = json.loads(raw)
    original_id = saved["attempt"]["id"]
    archive = loop._attempt_identity_archive
    journal_before = archive.path.read_bytes()
    rows = [json.loads(line) for line in journal_before.splitlines()]
    assert rows[-1]["transition"] == "checkpoint_bound"
    assert rows[-1]["data"]["active_attempts"][0]["id"] == original_id

    if change:
        if change == "uuid":
            saved["attempt"]["id"] = "f" * 32 if original_id != "f" * 32 else "e" * 32
            path.write_text(json.dumps(saved, sort_keys=True))
        else:
            key, value = ("session_id", "foreign-session") if change == "journal_session" else (
                "target", "rocket_launch")
            rows[-1][key] = value
            canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"),
                                                 ensure_ascii=False, allow_nan=False).encode()
            rows[-1]["record_sha256"] = hashlib.sha256(canonical({
                k: v for k, v in rows[-1].items() if k != "record_sha256"})).hexdigest()
            journal_before = b"".join(canonical(row) + b"\n" for row in rows)
            archive.path.write_bytes(journal_before)
        changed = path.read_bytes()
        with pytest.raises(AttemptIdentityError):
            controller(tmp_path, backend, resume=True)
        assert path.read_bytes() == changed
        assert archive.path.read_bytes() == journal_before
        assert backend.calls == []
    else:
        resumed = controller(tmp_path, backend, resume=True)
        assert path.read_bytes() == raw
        prepared_journal = archive.path.read_bytes()
        controller(tmp_path, backend, resume=True)
        assert archive.path.read_bytes() == prepared_journal
        assert path.read_bytes() == raw
        resumed.step()
        assert resumed.memory.attempt["id"] == original_id
        assert resumed.memory.pending is not None
        assert backend.calls == []
        rows = [json.loads(line) for line in archive.path.read_bytes().splitlines()]
        prepared = [r for r in rows if r["transition"] == "attempt_prepared"]
        assert len(prepared) == 1 and prepared[0]["attempt_id"] == original_id
