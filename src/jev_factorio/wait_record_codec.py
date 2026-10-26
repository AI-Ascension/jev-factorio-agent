"""Lossless, bounded JSONL deltas for repeated persistent-wait records.

The on-disk form is an optimization only. Readers must expand every delta to
the original JSON object before using it as gameplay or dashboard evidence.
Model, action, research, and other event records are never rewritten by this
module unless the caller explicitly marks a decision record as a wait.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, BinaryIO, Iterable, Iterator

MARKER = "_jev_lossless_wait_record"
VERSION = 1
MAX_REFERENCE_ROWS = 12
MAX_REFERENCE_BYTES = 512 * 1024
MIN_SAVINGS_BYTES = 256
MIN_SAVINGS_RATIO = 0.08
MAX_RECORD_BYTES = 16 * 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_RECONSTRUCTED_NODES = 1_000_000
MAX_DECODED_STREAM_BYTES = 512 * 1024 * 1024


def canonical(value: Any) -> bytes:
    """Canonical JSON bytes; JSON encoding preserves bool/int/float identity."""
    _validate_json(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _validate_json(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ValueError("Wait record exceeds the JSON nesting limit")
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Wait record contains a non-finite number")
        return
    if type(value) is list:
        for item in value:
            _validate_json(item, depth + 1)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("Wait record contains a non-string object key")
            _validate_json(item, depth + 1)
        return
    raise ValueError("Wait record contains a value outside JSON")


def parse_json(raw: bytes | str) -> Any:
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("Duplicate JSON key")
            value[key] = item
        return value

    def reject_constant(_value):
        raise ValueError("Non-finite JSON value")

    result = json.loads(raw, object_pairs_hook=pairs, parse_constant=reject_constant)
    _validate_json(result)
    return result


def _scope(channel: str, row: dict) -> dict | None:
    if channel == "gameplay":
        session = row.get("session_id")
        revision = row.get("code_revision")
        process = row.get("process_id")
        if (type(session) is not str or not session
                or type(process) not in (int, str) or process == ""
                or not isinstance(revision, dict)
                or type(revision.get("commit")) is not str
                or type(revision.get("source_sha256")) is not str):
            return None
        return {"session_id": session, "process_id": process,
                "code_revision": revision,
                "run_id": row.get("run_id"), "segment_id": row.get("segment_id"),
                "execution_id": row.get("execution_id"),
                "world_kind": row.get("world_kind"), "target": row.get("target"),
                "policy": row.get("policy")}
    if channel == "dashboard":
        if row.get("schema") != "jev.dashboard.v1" or type(row.get("run_id")) is not str:
            return None
        data = row.get("data")
        record = data.get("record") if isinstance(data, dict) else None
        record = record if isinstance(record, dict) else {}
        revision = record.get("code_revision")
        session = record.get("session_id")
        if (type(session) is not str or not session or not isinstance(revision, dict)
                or type(revision.get("commit")) is not str
                or type(revision.get("source_sha256")) is not str):
            return None
        return {"run_id": row["run_id"], "session_id": session,
                "code_revision": revision, "world_kind": record.get("world_kind"),
                "target": record.get("target"), "policy": record.get("policy")}
    raise ValueError("Unknown wait-record channel")


def _anchor_eligible(row: dict) -> bool:
    """Old PR-249 omission markers are opaque and can never anchor a delta."""
    if row.get("compact_record") == "persistent_wait":
        return False
    stack = [row]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if item.get("omitted") == "persistent_wait_repeat":
                return False
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return True


def _same_json_type(left: Any, right: Any) -> bool:
    """Compare JSON values with exact Python JSON type identity."""
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return (left.keys() == right.keys()
                and all(_same_json_type(left[key], right[key]) for key in left))
    if type(left) is list:
        return (len(left) == len(right)
                and all(_same_json_type(a, b) for a, b in zip(left, right)))
    return left == right


def _at_path(root: Any, path: list[Any]) -> Any:
    value = root
    for component in path:
        if type(component) is str and type(value) is dict and component in value:
            value = value[component]
        elif type(component) is int and type(value) is list and 0 <= component < len(value):
            value = value[component]
        else:
            raise ValueError("Wait delta references a missing anchor path")
    return value


def _charge_reconstruction_budget(budget: list[int], size: int, nodes: int) -> None:
    budget[0] += size
    budget[1] += nodes
    if budget[0] > MAX_RECORD_BYTES or budget[1] > MAX_RECONSTRUCTED_NODES:
        raise ValueError("Wait record reconstruction exceeds its output budget")


def _measure_value(value: Any, budget: list[int], depth: int = 0) -> tuple[int, int]:
    """Measure canonical JSON and charge it before any referenced copy/hash."""
    if depth > MAX_JSON_DEPTH:
        raise ValueError("Wait record exceeds the JSON nesting limit")
    if type(value) is dict:
        size, nodes = 2, 1
        _charge_reconstruction_budget(budget, size, nodes)
        for index, (key, child) in enumerate(value.items()):
            if type(key) is not str:
                raise ValueError("Wait record contains a non-string object key")
            key_size = (1 if index else 0) + len(canonical(key)) + 1
            size += key_size
            _charge_reconstruction_budget(budget, key_size, 0)
            child_size, child_nodes = _measure_value(child, budget, depth + 1)
            size += child_size
            nodes += child_nodes
        return size, nodes
    if type(value) is list:
        size, nodes = 2, 1
        _charge_reconstruction_budget(budget, size, nodes)
        for index, child in enumerate(value):
            separator = 1 if index else 0
            size += separator
            _charge_reconstruction_budget(budget, separator, 0)
            child_size, child_nodes = _measure_value(child, budget, depth + 1)
            size += child_size
            nodes += child_nodes
        return size, nodes
    _validate_json(value, depth)
    size = len(canonical(value))
    _charge_reconstruction_budget(budget, size, 1)
    return size, 1


def _measure_json(value: Any) -> tuple[int, int]:
    """Measure one reconstructed JSON row under its byte and node budgets."""
    return _measure_value(value, [0, 0])


def _measure_patch(node: Any, base: Any, anchor: Any, budget: list[int],
                   depth: int = 0) -> tuple[int, int]:
    """Validate and project patch output bounds before `_apply` copies refs."""
    if depth > MAX_JSON_DEPTH:
        raise ValueError("Wait delta nesting limit exceeded")
    if type(node) is not dict or type(node.get("k")) is not str:
        raise ValueError("Malformed wait delta node")
    kind = node["k"]
    if kind == "v":
        if set(node) != {"k", "v"}:
            raise ValueError("Malformed inline wait delta")
        _validate_json(node["v"])
        return _measure_value(node["v"], budget)
    if kind == "r":
        if set(node) != {"k", "p", "h"} or type(node["p"]) is not list:
            raise ValueError("Malformed wait delta reference")
        ref = _at_path(anchor, node["p"])
        measured = _measure_value(ref, budget)
        if type(node["h"]) is not str or digest(ref) != node["h"]:
            raise ValueError("Wait delta reference hash mismatch")
        return measured
    if kind == "o":
        if (set(node) != {"k", "d", "f"} or type(node["d"]) is not list
                or type(node["f"]) is not dict or type(base) is not dict):
            raise ValueError("Malformed wait delta object")
        drops, fields = node["d"], node["f"]
        if (any(type(key) is not str for key in drops)
                or len(drops) != len(set(drops))
                or any(key not in base for key in drops)
                or any(key in fields for key in drops)):
            raise ValueError("Invalid wait delta object removals")
        size, nodes = 2, 1
        _charge_reconstruction_budget(budget, size, nodes)
        keys = (base.keys() - set(drops)) | fields.keys()
        for index, key in enumerate(sorted(keys)):
            key_size = (1 if index else 0) + len(canonical(key)) + 1
            size += key_size
            _charge_reconstruction_budget(budget, key_size, 0)
            if key in fields:
                child = fields[key]
                if key in base:
                    child_size, child_nodes = _measure_patch(
                        child, base[key], anchor, budget, depth + 1)
                else:
                    if type(child) is not dict or child.get("k") != "v":
                        raise ValueError("New wait delta keys must be inline")
                    child_size, child_nodes = _measure_patch(
                        child, None, anchor, budget, depth + 1)
            else:
                child_size, child_nodes = _measure_value(base[key], budget, depth + 1)
            size += child_size
            nodes += child_nodes
        return size, nodes
    if kind == "a":
        if (set(node) != {"k", "v"} or type(node["v"]) is not list
                or type(base) is not list or len(node["v"]) != len(base)):
            raise ValueError("Malformed wait delta array")
        size, nodes = 2, 1
        _charge_reconstruction_budget(budget, size, nodes)
        for index, child in enumerate(node["v"]):
            separator = 1 if index else 0
            size += separator
            _charge_reconstruction_budget(budget, separator, 0)
            child_size, child_nodes = _measure_patch(
                child, base[index], anchor, budget, depth + 1)
            size += child_size
            nodes += child_nodes
        return size, nodes
    raise ValueError("Unknown wait delta node")


def _patch(value: Any, base: Any, path: list[Any]) -> dict:
    """Choose the smallest existing typed node; tiny references can cost more."""
    inline = {"k": "v", "v": value}
    if (_same_json_type(value, base) and canonical(value) == canonical(base)):
        candidate = {"k": "r", "p": path, "h": digest(base)}
    elif type(value) is dict and type(base) is dict:
        fields = {}
        for key in sorted(value):
            fields[key] = (_patch(value[key], base[key], [*path, key])
                           if key in base else {"k": "v", "v": value[key]})
        candidate = {"k": "o", "d": sorted(base.keys() - value.keys()), "f": fields}
    elif type(value) is list and type(base) is list and len(value) == len(base):
        candidate = {"k": "a", "v": [_patch(item, old, [*path, index])
                                     for index, (item, old) in enumerate(zip(value, base))]}
    else:
        return inline
    # Both nodes use the unchanged decoder grammar and exact JSON type checks.
    # Prefer inline on ties rather than retain an unnecessary anchor dependency.
    return min((inline, candidate), key=lambda node: len(canonical(node)))


def _apply(node: Any, base: Any, anchor: Any, depth: int = 0) -> Any:
    if depth > MAX_JSON_DEPTH:
        raise ValueError("Wait delta nesting limit exceeded")
    if type(node) is not dict or type(node.get("k")) is not str:
        raise ValueError("Malformed wait delta node")
    kind = node["k"]
    if kind == "v":
        if set(node) != {"k", "v"}:
            raise ValueError("Malformed inline wait delta")
        _validate_json(node["v"])
        return copy.deepcopy(node["v"])
    if kind == "r":
        if set(node) != {"k", "p", "h"} or type(node["p"]) is not list:
            raise ValueError("Malformed wait delta reference")
        ref = _at_path(anchor, node["p"])
        if type(node["h"]) is not str or digest(ref) != node["h"]:
            raise ValueError("Wait delta reference hash mismatch")
        return copy.deepcopy(ref)
    if kind == "o":
        if (set(node) != {"k", "d", "f"} or type(node["d"]) is not list
                or type(node["f"]) is not dict or type(base) is not dict):
            raise ValueError("Malformed wait delta object")
        drops = node["d"]
        if (any(type(key) is not str for key in drops)
                or len(drops) != len(set(drops))
                or any(key not in base for key in drops)
                or any(key in node["f"] for key in drops)):
            raise ValueError("Invalid wait delta object removals")
        result = {}
        fields = node["f"]
        dropped = set(drops)
        for key, old in base.items():
            if key in dropped:
                continue
            if key in fields:
                result[key] = _apply(fields[key], old, anchor, depth + 1)
            else:
                result[key] = copy.deepcopy(old)
        for key, child in fields.items():
            if key not in base:
                if type(child) is not dict or child.get("k") != "v":
                    raise ValueError("New wait delta keys must be inline")
                result[key] = _apply(child, None, anchor, depth + 1)
        return result
    if kind == "a":
        if (set(node) != {"k", "v"} or type(node["v"]) is not list
                or type(base) is not list or len(node["v"]) != len(base)):
            raise ValueError("Malformed wait delta array")
        return [_apply(child, base[index], anchor, depth + 1)
                for index, child in enumerate(node["v"])]
    raise ValueError("Unknown wait delta node")


@dataclass(frozen=True)
class PreparedRecord:
    wire: dict
    source: dict
    anchor_candidate: bool
    scope: dict | None
    is_delta: bool


class Encoder:
    """Single-writer state. Call commit only after the JSONL write succeeds."""

    def __init__(self, channel: str):
        if channel not in {"gameplay", "dashboard"}:
            raise ValueError("Unknown wait-record channel")
        self.channel = channel
        self.anchor: dict | None = None
        self.anchor_hash: str | None = None
        self.scope: dict | None = None
        self.rows_since_anchor = 0
        self.bytes_since_anchor = 0

    def reset(self) -> None:
        """Discard references when the full source row was not written."""
        self.anchor = self.anchor_hash = self.scope = None
        self.rows_since_anchor = self.bytes_since_anchor = 0

    def prepare(self, row: dict, *, wait: bool, anchor_candidate: bool = True) -> PreparedRecord:
        if type(row) is not dict or MARKER in row:
            raise ValueError("Wait records must be ordinary JSON objects")
        _validate_json(row)
        scope = _scope(self.channel, row)
        full_size = len(canonical(row))
        can_reference = (
            wait and anchor_candidate and scope is not None and self.anchor is not None
            and canonical(scope) == canonical(self.scope)
            and self.rows_since_anchor < MAX_REFERENCE_ROWS
            and self.bytes_since_anchor <= MAX_REFERENCE_BYTES
            and _anchor_eligible(self.anchor)
        )
        if can_reference:
            wire = {MARKER: {
                "version": VERSION, "channel": self.channel,
                "scope_sha256": digest(scope), "anchor_sha256": self.anchor_hash,
                "anchor_tick": _tick(self.anchor, self.channel),
                "record_tick": _tick(row, self.channel),
                "record_sha256": digest(row), "patch": _patch(row, self.anchor, []),
            }}
            wire_size = len(canonical(wire)) + 1
            if (self.bytes_since_anchor + wire_size <= MAX_REFERENCE_BYTES
                    and wire_size - 1 <= full_size - MIN_SAVINGS_BYTES
                    and wire_size - 1 <= full_size * (1 - MIN_SAVINGS_RATIO)):
                return PreparedRecord(wire, copy.deepcopy(row), False, scope, True)
        eligible_scope = scope if anchor_candidate and _anchor_eligible(row) else None
        return PreparedRecord(row, copy.deepcopy(row), eligible_scope is not None,
                              eligible_scope, False)

    def commit(self, prepared: PreparedRecord, written_bytes: int) -> None:
        if type(written_bytes) is not int or written_bytes < 1:
            raise ValueError("Invalid wait-record write size")
        if prepared.is_delta:
            if self.anchor is None:
                raise ValueError("Wait delta lost its anchor before commit")
            self.rows_since_anchor += 1
            self.bytes_since_anchor += written_bytes
        elif prepared.anchor_candidate:
            self.anchor = copy.deepcopy(prepared.source)
            self.anchor_hash = digest(prepared.source)
            self.scope = copy.deepcopy(prepared.scope)
            self.rows_since_anchor = 0
            self.bytes_since_anchor = written_bytes
        else:
            self.anchor = self.anchor_hash = self.scope = None
            self.rows_since_anchor = self.bytes_since_anchor = 0

    def skip(self, written_bytes: int) -> None:
        """Account for a non-anchor row between dashboard decision records."""
        if type(written_bytes) is not int or written_bytes < 1:
            raise ValueError("Invalid wait-record write size")
        if self.anchor is not None:
            self.rows_since_anchor += 1
            self.bytes_since_anchor += written_bytes


def _tick(row: dict, channel: str) -> int | None:
    if channel == "gameplay":
        state = row.get("after_state") or row.get("state")
        value = state.get("tick") if isinstance(state, dict) else row.get("tick")
    else:
        data = row.get("data")
        record = data.get("record") if isinstance(data, dict) else None
        state = record.get("state") if isinstance(record, dict) else None
        value = record.get("tick") if isinstance(record, dict) else None
        if value is None and isinstance(state, dict):
            value = state.get("tick")
    return value if type(value) is int and value >= 0 else None


class Decoder:
    """Strict bounded reader that reconstructs each original full record."""

    def __init__(self, channel: str):
        if channel not in {"gameplay", "dashboard"}:
            raise ValueError("Unknown wait-record channel")
        self.channel = channel
        self.anchor: dict | None = None
        self.anchor_hash: str | None = None
        self.scope: dict | None = None
        self.rows_since_anchor = 0
        self.bytes_since_anchor = 0
        self.last_was_anchor = False

    def reset(self) -> None:
        self.anchor = self.anchor_hash = self.scope = None
        self.rows_since_anchor = self.bytes_since_anchor = 0
        self.last_was_anchor = False

    def decode(self, wire: dict, raw_size: int, *, anchor_candidate: bool = True) -> dict:
        self.last_was_anchor = False
        if type(raw_size) is not int or not 1 <= raw_size <= MAX_RECORD_BYTES:
            raise ValueError("Invalid wait-record byte size")
        if type(wire) is not dict:
            raise ValueError("Wait-record line must be an object")
        if MARKER not in wire:
            _validate_json(wire)
            scope = _scope(self.channel, wire)
            if anchor_candidate:
                if scope is not None and _anchor_eligible(wire):
                    self.anchor = copy.deepcopy(wire)
                    self.anchor_hash = digest(wire)
                    self.scope = scope
                    self.rows_since_anchor = 0
                    self.bytes_since_anchor = raw_size
                    self.last_was_anchor = True
                else:
                    self.reset()
            elif self.anchor is not None:
                self.rows_since_anchor += 1
                self.bytes_since_anchor += raw_size
            return wire
        if set(wire) != {MARKER} or type(wire[MARKER]) is not dict:
            raise ValueError("Malformed wait-record envelope")
        item = wire[MARKER]
        required = {"version", "channel", "scope_sha256", "anchor_sha256",
                    "anchor_tick", "record_tick", "record_sha256", "patch"}
        if set(item) != required or type(item.get("version")) is not int or item["version"] != VERSION:
            raise ValueError("Unsupported wait-record envelope")
        if item["channel"] != self.channel or self.anchor is None or self.scope is None:
            raise ValueError("Wait-record anchor is unavailable")
        if (self.rows_since_anchor + 1 > MAX_REFERENCE_ROWS
                or self.bytes_since_anchor + raw_size > MAX_REFERENCE_BYTES):
            raise ValueError("Wait-record anchor is outside its retention window")
        if (item["anchor_sha256"] != self.anchor_hash
                or not _same_json_type(item["anchor_tick"], _tick(self.anchor, self.channel))
                or item["scope_sha256"] != digest(self.scope)):
            raise ValueError("Wait-record anchor or scope mismatch")
        _measure_patch(item["patch"], self.anchor, self.anchor, [0, 0])
        row = _apply(item["patch"], self.anchor, self.anchor)
        scope = _scope(self.channel, row)
        if (scope is None or canonical(scope) != canonical(self.scope)
                or item["scope_sha256"] != digest(scope)
                or not _same_json_type(item["record_tick"], _tick(row, self.channel))
                or type(item["record_sha256"]) is not str
                or digest(row) != item["record_sha256"]):
            raise ValueError("Reconstructed wait record failed identity or hash validation")
        self.rows_since_anchor += 1
        self.bytes_since_anchor += raw_size
        return row

    def skip(self, raw_size: int) -> None:
        """Account for an intentionally skipped physical blank line."""
        if type(raw_size) is not int or not 1 <= raw_size <= MAX_RECORD_BYTES:
            raise ValueError("Invalid skipped JSONL byte size")
        if self.anchor is not None:
            self.rows_since_anchor += 1
            self.bytes_since_anchor += raw_size


def decision_anchor_candidate(row: dict, channel: str) -> bool:
    if channel == "gameplay":
        return True
    return row.get("kind") == "decision_recorded"


def persistent_wait(row: dict, channel: str) -> bool:
    record = row
    if channel == "dashboard":
        data = row.get("data")
        record = data.get("record") if isinstance(data, dict) else None
    if not isinstance(record, dict) or record.get("model_call") is not False:
        return False
    recovery = record.get("persistent_recovery")
    return (record.get("status") == "blocked" and isinstance(recovery, dict)
            and recovery.get("phase") in {
                "waiting_for_changed_game_evidence", "evaluation_outcome_unknown_waiting",
                "alternatives_exhausted_waiting", "idle_wait_exhausted",
            })


def decode_jsonl(raw: bytes, channel: str, *, require_final_newline: bool = True,
                 max_line: int = MAX_RECORD_BYTES, max_records: int = 50000,
                 max_decoded_bytes: int = MAX_DECODED_STREAM_BYTES) -> list[dict]:
    if type(raw) is not bytes or not raw or len(raw) > 256 * 1024 * 1024:
        raise ValueError("Expected a bounded nonempty JSONL stream")
    if type(max_decoded_bytes) is not int or max_decoded_bytes < 1:
        raise ValueError("Invalid reconstructed JSONL byte budget")
    if require_final_newline and not raw.endswith(b"\n"):
        raise ValueError("Expected a complete JSONL stream")
    decoder = Decoder(channel)
    rows = []
    decoded_bytes = 0
    offset = 0
    for line in raw.splitlines(keepends=True):
        size = len(line)
        content = line[:-1] if line.endswith(b"\n") else line
        if not content or size > max_line or len(rows) >= max_records:
            raise ValueError("Invalid, oversized, or excessive JSONL record")
        item = parse_json(content)
        row = decoder.decode(item, size,
                             anchor_candidate=decision_anchor_candidate(item, channel))
        row_bytes, _ = _measure_json(row)
        decoded_bytes += row_bytes
        if decoded_bytes > max_decoded_bytes:
            raise ValueError("Reconstructed JSONL stream exceeds its output budget")
        rows.append(row)
        offset += size
    return rows


def iter_stream(stream: BinaryIO, channel: str, *, max_line: int = MAX_RECORD_BYTES,
                require_final_newline: bool = True, max_records: int | None = 50000,
                skip_blank: bool = False,
                max_decoded_bytes: int = MAX_DECODED_STREAM_BYTES) -> Iterator[dict]:
    if type(max_decoded_bytes) is not int or max_decoded_bytes < 1:
        raise ValueError("Invalid reconstructed JSONL byte budget")
    decoder = Decoder(channel)
    count = 0
    decoded_bytes = 0
    while raw := stream.readline(max_line + 1):
        count += 1
        if ((max_records is not None and count > max_records) or len(raw) > max_line
                or (require_final_newline and not raw.endswith(b"\n"))):
            raise ValueError("Oversized, incomplete, or excessive JSONL record")
        content = raw[:-1] if raw.endswith(b"\n") else raw
        if skip_blank and not content.strip():
            try:
                decoder.skip(len(raw))
            except ValueError as error:
                raise ValueError(f"Invalid or unreconstructable JSONL record at line {count}") from error
            continue
        try:
            row = parse_json(content)
            row = decoder.decode(row, len(raw),
                                 anchor_candidate=decision_anchor_candidate(row, channel))
            row_bytes, _ = _measure_json(row)
            decoded_bytes += row_bytes
            if decoded_bytes > max_decoded_bytes:
                raise ValueError("Reconstructed JSONL stream exceeds its output budget")
            yield row
        except (ValueError, TypeError, KeyError, RecursionError) as error:
            raise ValueError(f"Invalid or unreconstructable JSONL record at line {count}") from error


def encode_line(prepared: PreparedRecord) -> bytes:
    return canonical(prepared.wire) + b"\n"
