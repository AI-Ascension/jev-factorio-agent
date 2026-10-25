"""Captured generator and actual owner-main controls; no native deployment."""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

from jev_factorio.controller import HierarchicalLoop
from jev_factorio import owner_completion, provenance
from jev_factorio.native_owner_template import (
    prepare_native_owner_completion_source,
    prepare_native_owner_generator_source,
)

FIXTURES = Path(__file__).parent / "fixtures"
OWNER_SHA = "3e43cd362a6158134961ddae1900b20952e0469489695b402f0666a183f935a4"
GENERATOR_SHA = "520c70edcbd5bcfc7b55c1aa54efd9e93065e80babfe0baff17d3f45b6ef27ff"


def _capture(name, expected):
    raw = (FIXTURES / name).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == expected
    return raw


def _owner():
    return _capture("native_owner_v46_509.py.txt", OWNER_SHA)


def _generator():
    return _capture("native_owner_v47_generator509.py.txt", GENERATOR_SHA)


def _emit_owner(generator):
    # Execute only the actual generator's source assignment, never its file,
    # process, pin, admission, or native staging operations.
    tree = ast.parse(generator)
    assignments = [node for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "owner"
                           for target in node.targets)]
    assert len(assignments) == 1
    assignment = ast.Module(body=assignments, type_ignores=[])
    namespace = {"old": _owner(),
                 "prepare_native_owner_completion_source": prepare_native_owner_completion_source}
    exec(compile(assignment, "<captured-generator-expression>", "exec"), namespace)
    return namespace["owner"].encode()


@pytest.mark.parametrize("version", [46, 47])
def test_known_owner_source_is_guarded_and_repeated_preparation_is_byte_identical(version):
    raw = _owner()
    if version == 47:
        raw = raw.replace(b"v46", b"v47").replace(b"attempts >= 46", b"attempts >= 47")
        assert hashlib.sha256(raw).hexdigest() == (
            "19024f97c757818cce3940a19442c5ea997d628b8870db69451e087d1b114b9e")
    prepared = prepare_native_owner_completion_source(
        raw, expected_owner_sha256=hashlib.sha256(raw).hexdigest())
    assert prepared != raw
    assert prepare_native_owner_completion_source(
        prepared, expected_owner_sha256=hashlib.sha256(prepared).hexdigest()) == prepared
    assert f"attempts >= {version}".encode() in prepared
    assert f"recovery-v{version}-consumed.json".encode() in prepared


@pytest.mark.parametrize("mutation", [
    b"\n# unrelated modification\n",
    b"\n# replacement completion entry point\n",
])
def test_caller_digest_does_not_register_a_modified_owner_template(mutation):
    raw = _owner() + mutation
    with pytest.raises(ValueError, match="not a supported captured profile"):
        prepare_native_owner_completion_source(raw, expected_owner_sha256=hashlib.sha256(raw).hexdigest())


def test_source_replacement_cannot_inherit_an_existing_owner_pin():
    with pytest.raises(ValueError, match="differs from its expected digest"):
        prepare_native_owner_completion_source(_owner() + b"\n", expected_owner_sha256=OWNER_SHA)


def test_generator_changes_only_its_import_and_owner_source_expression():
    original = _generator()
    prepared = prepare_native_owner_generator_source(original, expected_generator_sha256=GENERATOR_SHA)
    original_tree, prepared_tree = ast.parse(original), ast.parse(prepared)

    def retained(tree):
        return [ast.dump(node) for node in tree.body
                if not (isinstance(node, ast.ImportFrom)
                        and node.module == "native_owner_template509")
                and not (isinstance(node, ast.Assign)
                         and any(isinstance(target, ast.Name) and target.id == "owner"
                                 for target in node.targets))]

    assert retained(original_tree) == retained(prepared_tree)
    generated = _emit_owner(prepared)
    direct = prepare_native_owner_completion_source(_owner(), expected_owner_sha256=OWNER_SHA)
    assert generated == direct.replace(b"v46", b"v47").replace(b"attempts >= 46", b"attempts >= 47")
    assert prepare_native_owner_completion_source(
        generated, expected_owner_sha256=hashlib.sha256(generated).hexdigest()) == generated


def test_unknown_generator_is_not_accepted_even_with_a_matching_caller_digest():
    changed = _generator().replace(b"attempts >= 47", b"attempts >= 99")
    with pytest.raises(ValueError, match="not a supported captured profile"):
        prepare_native_owner_generator_source(changed, expected_generator_sha256=hashlib.sha256(changed).hexdigest())


def test_corrected_generator_loads_its_real_standalone_dependency(tmp_path, monkeypatch):
    import jev_factorio.native_owner_template as helper

    # The frozen generation repository lacks this new package module. Load the
    # delivered sibling helper using the actual emitted import and expression;
    # do not satisfy the import through an injected function or package alias.
    (tmp_path / "native_owner_template509.py").write_bytes(Path(helper.__file__).read_bytes())
    corrected = prepare_native_owner_generator_source(
        _generator(), expected_generator_sha256=GENERATOR_SHA)
    tree = ast.parse(corrected)
    nodes = [node for node in tree.body
             if (isinstance(node, ast.ImportFrom) and node.module == "native_owner_template509")
             or (isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "owner"
                         for target in node.targets))]
    assert len(nodes) == 2
    namespace = {"old": _owner()}
    previous = sys.modules.get("native_owner_template509")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(sys, "path", [str(tmp_path), *sys.path])
            patch.delitem(sys.modules, "native_owner_template509", raising=False)
            exec(compile(ast.Module(body=nodes, type_ignores=[]), "<actual-generator-dependency>", "exec"), namespace)
            assert namespace["owner"].encode() == _emit_owner(corrected)
            assert Path(sys.modules["native_owner_template509"].__file__).parent == tmp_path
    finally:
        if previous is None:
            sys.modules.pop("native_owner_template509", None)
        else:
            sys.modules["native_owner_template509"] = previous


def _run_completed_main(tmp_path, monkeypatch, source, *, target="rocket_launch",
                        real_preflight=False, close_failure=False, unconsumed=False):
    root = tmp_path / "owner"
    run = root / "run"
    run.mkdir(parents=True)
    (root / "server").mkdir()
    (root / "server/rcon-password").write_text("offline-password\n")
    (root / "provider-private.json").write_text("{}\n")
    owner_path = root / "continuous_owner_v47.py"
    owner_path.write_bytes(source)
    # The real producer emits a complete checkpoint; an incorrect target is
    # still a valid producer checkpoint, not a reduced/malformed test mapping.
    memory = HierarchicalLoop.memory_type(
        session_id="offline-session", target=target, last_tick=20, status="completed",
        completed_goals={target: 20},
        history=[{"kind": "goal_completed", "goal": target, "tick": 20}])
    checkpoint = run / "controller.json"
    memory.save(checkpoint)
    before = checkpoint.read_bytes()
    pin = {"commit": "a" * 40, "source_sha256": "b" * 64}
    authority = {"new_source_revision": pin, "checkpoint_sha256": hashlib.sha256(before).hexdigest()}
    authority_bytes = json.dumps(authority).encode()
    (root / "recovery-v47.json").write_bytes(authority_bytes)
    if not unconsumed:
        (run / "recovery-v47-consumed.json").write_text("{}")
    (root / "controller-config.json").write_text(json.dumps({
        "owner_sha256": hashlib.sha256(source).hexdigest(), "source_revision": pin,
        "recovery_sha256": hashlib.sha256(authority_bytes).hexdigest()}))
    module = ModuleType("offline_generated_owner")
    module.__file__ = str(owner_path)
    exec(compile(source, str(owner_path), "exec"), module.__dict__)
    module.ROOT, module.RUN = root, run
    module.SOURCE, module.CHECKPOINT = root / "source", checkpoint
    module.signal = SimpleNamespace(SIGTERM=15, SIGINT=2, signal=lambda *_: None)
    holds, calls, clients = [], [], []
    module.hold = holds.append

    def forbidden(*_, **__):
        pytest.fail("Completed owner must not launch a child or retry a failed fixture")

    module.subprocess = SimpleNamespace(Popen=forbidden)
    module.time = SimpleNamespace(time=lambda: 0, sleep=forbidden)

    class OfflineClient:
        def __init__(self, *_args, **_kwargs):
            self.close_attempted = self.closed = False
            self.commands = []
            clients.append(self)

        def send_command(self, command):
            self.commands.append(command)
            return json.dumps({"tick": 20, "speed": 1, "marked": True, "runtime": True,
                               "session_id": "offline-session", "connected": 1})

        def close(self):
            self.close_attempted = True
            if close_failure and len(clients) == 2 and self is clients[1]:
                raise OSError("offline close failure")
            self.closed = True

    def observe_gate(path, **values):
        calls.append((path, values))
        return owner_completion.OwnerCompletionWitness(
            hashlib.sha256(before).hexdigest(), "offline-session", target, 20, 20,
            pin["commit"], pin["source_sha256"], hashlib.sha256(source).hexdigest(), "c" * 64)

    rcon = ModuleType("factorio_rcon")
    rcon.RCONClient = OfflineClient
    environment, paths = dict(os.environ), list(sys.path)
    try:
        with monkeypatch.context() as patch:
            patch.setitem(sys.modules, "factorio_rcon", rcon)
            patch.setattr(provenance, "source_revision", lambda *_args, **_kwargs: pin)
            if not real_preflight:
                patch.setattr(owner_completion, "preflight_completed_owner", observe_gate)
            module.main()
    finally:
        os.environ.clear()
        os.environ.update(environment)
        sys.path[:] = paths
    assert checkpoint.read_bytes() == before
    assert module.child is None
    assert all(client.close_attempted for client in clients)
    assert not list(run.glob("*-prepared.json"))
    assert not (run / "owner-initialization.json").exists()
    status_path = run / "owner-status.json"
    status = json.loads(status_path.read_text()) if status_path.exists() else None
    return holds, calls, clients, status


def test_actual_generator_and_real_preflight_reject_valid_completed_checkpoint_for_wrong_target(
        tmp_path, monkeypatch):
    legacy = _emit_owner(_generator())
    old = _run_completed_main(tmp_path / "before", monkeypatch, legacy,
                              target="other-target", real_preflight=True)
    assert old[3]["phase"] == "completed"  # Concrete unchanged-generator regression.
    corrected = _emit_owner(prepare_native_owner_generator_source(
        _generator(), expected_generator_sha256=GENERATOR_SHA))
    new = _run_completed_main(tmp_path / "after", monkeypatch, corrected,
                              target="other-target", real_preflight=True)
    assert new[3] is None
    assert new[0] == ["Completed checkpoint typed reconciliation failed (ValueError)"]
    assert len(new[2]) == 2 and all(client.closed for client in new[2])
    assert len(new[2][0].commands) == 1 and not new[2][1].commands


def test_generated_owner_calls_gate_and_binds_reported_witness_then_closes_clients(tmp_path, monkeypatch):
    source = _emit_owner(prepare_native_owner_generator_source(
        _generator(), expected_generator_sha256=GENERATOR_SHA))
    holds, calls, clients, status = _run_completed_main(tmp_path, monkeypatch, source)
    assert not holds and len(calls) == 1
    assert calls[0][1]["client"] is clients[1]
    assert calls[0][1]["expected_owner_sha256"] == hashlib.sha256(source).hexdigest()
    assert calls[0][1]["expected_source_revision"] == calls[0][1]["startup_source_revision"]
    assert status["completion_checkpoint_sha256"]
    assert status["completion_observation_sha256"] == "c" * 64
    assert len(clients) == 2 and all(client.closed for client in clients)
    # Witness spy isolates caller wiring. Other509tests validate real settled
    # completion and current source-produced Lua; this is not native acceptance.


def test_completed_owner_cannot_use_an_unconsumed_recovery_grant(tmp_path, monkeypatch):
    source = _emit_owner(prepare_native_owner_generator_source(
        _generator(), expected_generator_sha256=GENERATOR_SHA))
    holds, calls, clients, status = _run_completed_main(tmp_path, monkeypatch, source, unconsumed=True)
    assert holds == ["Unconsumed recovery authority cannot authorize a completed return"]
    assert not calls and status is None and len(clients) == 1
    assert not (tmp_path / "owner/run/recovery-v47-consumed.json").exists()


def test_completion_close_failure_is_a_hold_and_never_reports_success(tmp_path, monkeypatch):
    source = _emit_owner(prepare_native_owner_generator_source(
        _generator(), expected_generator_sha256=GENERATOR_SHA))
    holds, calls, clients, status = _run_completed_main(tmp_path, monkeypatch, source, close_failure=True)
    assert holds == ["Completed checkpoint typed reconciliation failed (OSError)"]
    assert len(calls) == 1 and status is None
    assert clients[0].closed and clients[1].close_attempted and not clients[1].closed
