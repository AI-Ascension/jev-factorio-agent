"""Oversized display rows must break wait references without ending telemetry."""
from __future__ import annotations

import copy
import json

import pytest

from jev_factorio import dashboard
from jev_factorio.wait_record_codec import MARKER, digest, iter_stream


def record(tick: int) -> dict:
    return {
        "session_id": "synthetic-session",
        "code_revision": {"commit": "a" * 40, "source_sha256": "b" * 64},
        "world_kind": "mock", "target": "rocket_launch", "policy": "jev",
        "tick": tick, "state": {"tick": tick, "observed": ["x" * 1800] * 80},
        "status": "blocked", "model_call": False,
        "persistent_recovery": {"phase": "waiting_for_changed_game_evidence"},
    }


def oversized_record(tick: int) -> dict:
    value = record(tick)
    # These 384 strings survive the ordinary sanitizer's item/node limits but
    # exceed the production line bound. The writer must emit its bounded marker.
    value["oversized"] = [["y" * 2000] * 128 for _ in range(3)]
    return value


def read_wire(path):
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def test_truncated_decision_keeps_writer_live_and_requires_fresh_wait_anchor(tmp_path):
    path = tmp_path / "events.jsonl"
    source = oversized_record(3)
    original = copy.deepcopy(source)
    with dashboard.EventWriter(path) as writer:
        writer.emit("decision_recorded", 2, record=record(1))
        writer.emit("decision_recorded", 2, record=record(2))
        assert MARKER in read_wire(path)[1]

        writer.emit("decision_recorded", 2, record=source)
        assert writer.disabled is False
        encoder = writer._wait_record_encoder
        assert encoder.anchor is encoder.anchor_hash is encoder.scope is None
        assert encoder.rows_since_anchor == encoder.bytes_since_anchor == 0

        writer.emit("decision_recorded", 2, record=record(4))
        writer.emit("decision_recorded", 2, record=record(5))
        assert writer.disabled is False

    wire = read_wire(path)
    assert wire[2]["data"] == {
        "_display_truncated": "Event exceeds dashboard byte limit"}
    assert MARKER not in wire[3]
    assert wire[4][MARKER]["anchor_sha256"] == digest(wire[3])
    assert wire[5]["kind"] == "run_finished"
    with path.open("rb") as stream:
        decoded = list(iter_stream(stream, "dashboard"))
    assert [row["seq"] for row in decoded] == [1, 2, 3, 4, 5, 6]
    assert decoded[1]["data"]["record"]["tick"] == 2
    assert decoded[4]["data"]["record"]["tick"] == 5
    assert source == original


@pytest.mark.parametrize("failure", ["raise", "short"])
def test_failed_truncation_write_does_not_reopen_or_reanchor_writer(
        tmp_path, monkeypatch, failure):
    path = tmp_path / "events.jsonl"
    with dashboard.EventWriter(path) as writer:
        writer.emit("decision_recorded", 2, record=record(1))
        writer.emit("decision_recorded", 2, record=record(2))
        encoder = writer._wait_record_encoder
        retained = (copy.deepcopy(encoder.anchor), encoder.anchor_hash,
                    copy.deepcopy(encoder.scope), encoder.rows_since_anchor,
                    encoder.bytes_since_anchor)
        before = path.read_bytes()
        writes = []

        def fail_write(fd, encoded):
            writes.append(encoded)
            if failure == "raise":
                raise OSError("synthetic display write failure")
            return len(encoded) - 1

        monkeypatch.setattr(dashboard.os, "write", fail_write)
        writer.emit("decision_recorded", 2, record=oversized_record(3))
        writer.emit("decision_recorded", 2, record=record(4))
        assert writer.disabled is True
        assert len(writes) == 1
        assert (encoder.anchor, encoder.anchor_hash, encoder.scope,
                encoder.rows_since_anchor, encoder.bytes_since_anchor) == retained
        assert path.read_bytes() == before
