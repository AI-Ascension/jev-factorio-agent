"""Durable original-attempt identity for checkpointed controller execution.

The checkpoint remains the campaign state format.  This private, bounded journal
adds an independently persisted identity anchor for actions which are executable
or still potentially paid.  It is deliberately not a distributed trust system:
the one operational owner lock protects cooperating processes, and a principal
able to rewrite both files can still replace both records.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Iterator
from uuid import uuid4

from .operational_safety import SafetyStateError, safety_dir


MAX_ATTEMPT_JOURNAL_BYTES = 64 * 1024 * 1024
MAX_ATTEMPT_JOURNAL_RECORDS = 65536
MAX_ATTEMPT_JOURNAL_RECORD_BYTES = 256 * 1024
MAX_ATTEMPT_OWNER_LOCKS = 128


class AttemptIdentityError(ValueError):
    """Attempt identity is absent, inconsistent, or cannot be durably proven."""


class AttemptIdentityBusy(RuntimeError):
    """Another process or controller currently owns this checkpoint."""


_REGISTRY_GUARD = threading.Lock()
_OWNER_LOCKS: dict[str, dict] = {}
_OWNER_LOCAL = threading.local()


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _strict_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate key in attempt identity journal")
        value[key] = item
    return value


def _reject_constant(value):
    raise ValueError(f"Invalid numeric constant: {value}")


def _stamp(path: Path) -> tuple | None:
    try:
        value = path.stat(follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISREG(value.st_mode):
        return None
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
            value.st_mode & 0o170000)


def _reject_leaf_link(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise AttemptIdentityError(f"{label} must not be a symlink")


def _sync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_private_directory(path: Path) -> None:
    _reject_leaf_link(path, "Attempt journal directory")
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            raise OSError("Attempt journal directory has no existing parent")
        current = parent
    if not current.is_dir() or current.is_symlink():
        raise AttemptIdentityError("Attempt journal parent is not a safe directory")
    for child in reversed(missing):
        child.mkdir(mode=0o700)
        if child.is_symlink():
            raise AttemptIdentityError("Attempt journal directory changed during creation")
        _sync_directory(child.parent)
    try:
        path.chmod(0o700)
    except OSError:
        # On Windows the ACL, rather than POSIX mode bits, is authoritative.
        if os.name == "posix":
            raise


def _open_regular(path: Path, flags: int, mode: int = 0o600) -> int:
    _reject_leaf_link(path, "Attempt identity file")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    binary = getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags | nofollow | binary, mode)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise AttemptIdentityError("Attempt identity path is not a regular file")
        current = _stamp(path)
        opened = os.fstat(descriptor)
        if (current is None or current[:2] != (opened.st_dev, opened.st_ino)
                or current[2] != opened.st_size):
            raise AttemptIdentityError("Attempt identity file changed while opening")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def _owner_guard(lock_path: Path) -> Iterator[None]:
    key = os.path.normcase(os.path.abspath(os.fspath(lock_path)))
    held = getattr(_OWNER_LOCAL, "held", None)
    if held is None:
        held = {}
        _OWNER_LOCAL.held = held
    existing = held.get(key)
    if existing is not None:
        existing["depth"] += 1
        try:
            yield
        finally:
            existing["depth"] -= 1
        return

    with _REGISTRY_GUARD:
        slot = _OWNER_LOCKS.get(key)
        if slot is None:
            if len(_OWNER_LOCKS) >= MAX_ATTEMPT_OWNER_LOCKS:
                raise AttemptIdentityBusy("Attempt owner lock registry is full")
            slot = {"lock": threading.RLock(), "users": 0}
            _OWNER_LOCKS[key] = slot
        slot["users"] += 1
    local_lock = slot["lock"]
    if not local_lock.acquire(blocking=False):
        with _REGISTRY_GUARD:
            slot["users"] -= 1
            if slot["users"] == 0 and _OWNER_LOCKS.get(key) is slot:
                del _OWNER_LOCKS[key]
        raise AttemptIdentityBusy("This checkpoint already has an active controller owner")

    descriptor = None
    locked = False
    try:
        flags = os.O_RDWR | os.O_CREAT
        descriptor = _open_regular(lock_path, flags, 0o600)
        if os.name == "posix":
            import fcntl
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise AttemptIdentityBusy("Another process owns this checkpoint") from error
            locked = True
        else:
            import msvcrt
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise AttemptIdentityBusy("Another process owns this checkpoint") from error
            locked = True
        held[key] = {"depth": 1, "descriptor": descriptor,
                     "slot": slot, "path": lock_path}
        yield
    except BaseException as original_error:
        # Discard reentrant authority before releasing its real OS lock.
        held.pop(key, None)
        cleanup_error = None
        try:
            if descriptor is not None:
                try:
                    if locked:
                        if os.name == "posix":
                            import fcntl
                            fcntl.flock(descriptor, fcntl.LOCK_UN)
                        else:
                            import msvcrt
                            os.lseek(descriptor, 0, os.SEEK_SET)
                            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                finally:
                    os.close(descriptor)
        except BaseException as error:
            cleanup_error = error
        finally:
            local_lock.release()
            with _REGISTRY_GUARD:
                slot["users"] -= 1
                if slot["users"] == 0 and _OWNER_LOCKS.get(key) is slot:
                    del _OWNER_LOCKS[key]
        if cleanup_error is not None:
            raise original_error from cleanup_error
        raise
    else:
        item = held.pop(key)
        descriptor = item["descriptor"]
        unlock_error = None
        try:
            if os.name == "posix":
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            else:
                import msvcrt
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        except BaseException as error:
            unlock_error = error
        finally:
            try:
                os.close(descriptor)
            finally:
                local_lock.release()
                with _REGISTRY_GUARD:
                    slot["users"] -= 1
                    if slot["users"] == 0 and _OWNER_LOCKS.get(key) is slot:
                        del _OWNER_LOCKS[key]
        if unlock_error is not None:
            raise AttemptIdentityError("Could not release attempt owner lock") from unlock_error


class AttemptIdentityArchive:
    """Append-only identity journal for one serialized controller checkpoint."""

    def __init__(self, checkpoint: Path | str):
        self.checkpoint = Path(os.path.abspath(os.fspath(checkpoint)))
        self.directory = safety_dir(self.checkpoint)
        self.path = self.directory / "attempt-identity-v1.jsonl"
        self.lock_path = self.directory / "attempt-identity-v1.lock"
        self.checkpoint_key = hashlib.sha256(
            os.fsencode(os.path.normcase(os.path.abspath(self.checkpoint)))).hexdigest()
        self._records: list[dict] = []
        self._raw = b""
        self._head: str | None = None
        self._checkpoint_sha256: str | None = None
        self._identity: tuple[str, str] | None = None
        self._loaded = False

    @contextmanager
    def owner(self):
        _ensure_private_directory(self.directory)
        with _owner_guard(self.lock_path):
            was_loaded = self._loaded
            if not was_loaded:
                self._load()
                self._loaded = True
            try:
                yield self
            finally:
                self._loaded = was_loaded

    def _require_owner(self) -> None:
        key = os.path.normcase(os.path.abspath(os.fspath(self.lock_path)))
        if key not in getattr(_OWNER_LOCAL, "held", {}):
            raise AttemptIdentityError("Attempt journal operation requires checkpoint ownership")

    def _read_file(self, path: Path, *, maximum: int) -> bytes:
        _reject_leaf_link(path, "Attempt identity file")
        try:
            before = _stamp(path)
        except OSError:
            before = None
        if before is None:
            raise FileNotFoundError(path)
        if before[2] > maximum:
            raise AttemptIdentityError("Attempt identity file exceeds its size bound")
        descriptor = _open_regular(path, os.O_RDONLY)
        try:
            descriptor_before = os.fstat(descriptor)
            chunks = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(65536, maximum + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > maximum:
                    raise AttemptIdentityError("Attempt identity file exceeds its size bound")
            descriptor_after = os.fstat(descriptor)
            after = _stamp(path)
            same = lambda left, right: (left.st_dev, left.st_ino, left.st_size,
                                        left.st_mtime_ns, left.st_mode & 0o170000) == (
                                        right.st_dev, right.st_ino, right.st_size,
                                        right.st_mtime_ns, right.st_mode & 0o170000)
            if (after is None or not same(descriptor_before, descriptor_after)
                    or (before[0], before[1], before[2], before[3], before[4]) != after):
                raise AttemptIdentityError("Attempt identity file changed while reading")
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def _load(self) -> None:
        self._require_owner()
        _reject_leaf_link(self.checkpoint, "Checkpoint")
        try:
            raw = self._read_file(self.path, maximum=MAX_ATTEMPT_JOURNAL_BYTES)
        except FileNotFoundError:
            raw = b""
        records = []
        previous = None
        if raw:
            if not raw.endswith(b"\n"):
                raise AttemptIdentityError("Attempt identity journal has an incomplete final record")
            for index, line in enumerate(raw.splitlines()):
                if len(line) > MAX_ATTEMPT_JOURNAL_RECORD_BYTES:
                    raise AttemptIdentityError("Attempt identity journal record exceeds its bound")
                try:
                    item = json.loads(line.decode("utf-8"), object_pairs_hook=_strict_object,
                                      parse_constant=_reject_constant)
                except (UnicodeError, ValueError, json.JSONDecodeError) as error:
                    raise AttemptIdentityError("Attempt identity journal is malformed") from error
                self._validate_record(item, index, previous)
                if records and (item["session_id"], item["target"]) != (
                        records[0]["session_id"], records[0]["target"]):
                    raise AttemptIdentityError("Attempt journal record belongs to another campaign")
                previous = item["record_sha256"]
                records.append(item)
                if len(records) > MAX_ATTEMPT_JOURNAL_RECORDS:
                    raise AttemptIdentityError("Attempt identity journal record limit exceeded")
        self._records, self._raw = records, raw
        self._head = previous
        self._identity = None
        self._checkpoint_sha256 = None
        if records:
            anchor = records[0]
            self._identity = (anchor["session_id"], anchor["target"])
            if anchor["transition"] not in {"campaign_anchor", "legacy_migration_anchor"}:
                raise AttemptIdentityError("Attempt identity journal is missing its campaign anchor")
            self._checkpoint_sha256 = anchor["data"]["checkpoint_sha256"]
            for item in records[1:]:
                if item["transition"] == "checkpoint_bound":
                    self._checkpoint_sha256 = item["data"]["checkpoint_sha256"]

    def _validate_record(self, item, sequence: int, previous: str | None) -> None:
        fields = {"schema", "sequence", "previous_sha256", "checkpoint_key_sha256",
                  "session_id", "target", "transition", "attempt_id", "identity",
                  "data", "record_sha256"}
        if type(item) is not dict or set(item) != fields:
            raise AttemptIdentityError("Attempt identity journal record fields are invalid")
        if (type(item["schema"]) is not int or item["schema"] != 1
                or type(item["sequence"]) is not int or item["sequence"] != sequence
                or item["previous_sha256"] != previous
                or item["checkpoint_key_sha256"] != self.checkpoint_key
                or type(item["session_id"]) is not str or not item["session_id"]
                or len(item["session_id"]) > 256
                or type(item["target"]) is not str or not item["target"]
                or len(item["target"]) > 128
                or type(item["transition"]) is not str
                or type(item["data"]) is not dict):
            raise AttemptIdentityError("Attempt identity journal record identity is invalid")
        if item["identity"] is not None:
            self._validate_identity(item["identity"])
            if item["attempt_id"] != item["identity"]["id"]:
                raise AttemptIdentityError("Attempt identity journal id does not match its tuple")
        elif item["attempt_id"] is not None:
            raise AttemptIdentityError("Attempt identity journal has an id without a tuple")
        digest = item["record_sha256"]
        if (type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or _sha({key: value for key, value in item.items() if key != "record_sha256"}) != digest):
            raise AttemptIdentityError("Attempt identity journal hash chain is invalid")
        if sequence == 0:
            if (item["identity"] is not None or item["attempt_id"] is not None
                    or item["transition"] not in {"campaign_anchor", "legacy_migration_anchor"}):
                raise AttemptIdentityError("Attempt identity journal anchor is invalid")
            expected_anchor_fields = {"checkpoint_sha256", "baseline_outcomes"}
            if item["transition"] == "legacy_migration_anchor":
                expected_anchor_fields |= {
                    "approval_schema", "approval_id", "approval_mode",
                    "approved_attempts", "approved_attempts_sha256",
                }
            if set(item["data"]) != expected_anchor_fields:
                raise AttemptIdentityError("Attempt identity journal anchor fields are invalid")
            checkpoint_sha = item["data"]["checkpoint_sha256"]
            if checkpoint_sha is not None and (type(checkpoint_sha) is not str
                    or re.fullmatch(r"[0-9a-f]{64}", checkpoint_sha) is None):
                raise AttemptIdentityError("Attempt identity journal anchor digest is invalid")
            baseline = item["data"]["baseline_outcomes"]
            if type(baseline) is not list or len(baseline) > 64:
                raise AttemptIdentityError("Attempt identity journal baseline is invalid")
            seen = set()
            for row in baseline:
                if (type(row) is not dict or set(row) != {"id", "sha256"}
                        or type(row["id"]) is not str
                        or re.fullmatch(r"(?:legacy:[0-9a-f]{64}|[0-9a-f]{32})", row["id"]) is None
                        or type(row["sha256"]) is not str
                        or re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) is None
                        or row["id"] in seen):
                    raise AttemptIdentityError("Attempt identity journal baseline outcome is invalid")
                seen.add(row["id"])
            if item["transition"] == "legacy_migration_anchor":
                approved = item["data"]["approved_attempts"]
                if (type(item["data"]["approval_schema"]) is not int
                        or item["data"]["approval_schema"] != 1
                        or type(item["data"]["approval_id"]) is not str
                        or re.fullmatch(r"[0-9a-f]{32}", item["data"]["approval_id"]) is None
                        or item["data"]["approval_mode"] != "explicit_supervisor_cli"
                        or type(approved) is not list or not 1 <= len(approved) <= 2
                        or type(item["data"]["approved_attempts_sha256"]) is not str
                        or item["data"]["approved_attempts_sha256"] != _sha(approved)):
                    raise AttemptIdentityError("Explicit legacy migration approval is malformed")
                roles = set()
                ids = set()
                for row in approved:
                    if (type(row) is not dict
                            or set(row) != {"owner", "identity", "checkpoint_dispatch"}
                            or row["owner"] not in {"attempt", "background_attempt"}):
                        raise AttemptIdentityError("Explicit migration attempt binding is malformed")
                    allowed_dispatch = (("prepared", "ambiguous", "returned")
                                        if row["owner"] == "attempt"
                                        else ("background_returned",))
                    if row["checkpoint_dispatch"] not in allowed_dispatch:
                        raise AttemptIdentityError("Explicit migration dispatch state is invalid")
                    self._validate_identity(row["identity"])
                    if ((row["identity"]["session_id"], row["identity"]["target"])
                            != (item["session_id"], item["target"])
                            or row["owner"] in roles or row["identity"]["id"] in ids):
                        raise AttemptIdentityError("Explicit migration attempt tuple is duplicated or foreign")
                    roles.add(row["owner"])
                    ids.add(row["identity"]["id"])
                if approved != sorted(approved, key=lambda row: row["owner"]):
                    raise AttemptIdentityError("Explicit migration attempts are not canonically ordered")
        elif item["transition"] not in {
            "checkpoint_intent", "checkpoint_bound", "checkpoint_write_abandoned",
            "attempt_prepared", "dispatch_started", "dispatch_returned",
            "dispatch_not_entered", "retained_transfer_retry_authorized",
            "terminal_intent", "settled", "background_transferred",
        }:
            raise AttemptIdentityError("Unknown attempt identity journal transition")
        if sequence > 0:
            transition = item["transition"]
            data = item["data"]
            if transition == "checkpoint_intent":
                expected = {"previous_sha256", "checkpoint_sha256", "active_attempts",
                            "terminal_outcomes"}
                if set(data) != expected:
                    raise AttemptIdentityError("Checkpoint write intent fields are invalid")
                for name in ("previous_sha256", "checkpoint_sha256"):
                    value = data[name]
                    if value is not None and (type(value) is not str
                            or re.fullmatch(r"[0-9a-f]{64}", value) is None):
                        raise AttemptIdentityError("Checkpoint write intent digest is invalid")
                if type(data["active_attempts"]) is not list or len(data["active_attempts"]) > 2:
                    raise AttemptIdentityError("Checkpoint active attempt set is invalid")
                for value in data["active_attempts"]:
                    self._validate_identity(value)
                if type(data["terminal_outcomes"]) is not list or len(data["terminal_outcomes"]) > 64:
                    raise AttemptIdentityError("Checkpoint terminal set is invalid")
                for value in data["terminal_outcomes"]:
                    if (type(value) is not dict or set(value) != {"id", "outcome_sha256"}
                            or type(value["id"]) is not str
                            or re.fullmatch(r"(?:legacy:[0-9a-f]{64}|[0-9a-f]{32})", value["id"]) is None
                            or type(value["outcome_sha256"]) is not str
                            or re.fullmatch(r"[0-9a-f]{64}", value["outcome_sha256"]) is None):
                        raise AttemptIdentityError("Checkpoint terminal identity is invalid")
            elif transition == "checkpoint_bound":
                if set(data) != {"checkpoint_sha256", "active_attempts", "terminal_outcomes"}:
                    raise AttemptIdentityError("Checkpoint binding fields are invalid")
                if (type(data["checkpoint_sha256"]) is not str
                        or re.fullmatch(r"[0-9a-f]{64}", data["checkpoint_sha256"]) is None
                        or type(data["active_attempts"]) is not list
                        or type(data["terminal_outcomes"]) is not list):
                    raise AttemptIdentityError("Checkpoint binding values are invalid")
                for value in data["active_attempts"]:
                    self._validate_identity(value)
            elif transition == "checkpoint_write_abandoned":
                if (set(data) != {"previous_sha256", "expected_sha256"}
                        or (data["previous_sha256"] is not None
                            and (type(data["previous_sha256"]) is not str
                                 or re.fullmatch(r"[0-9a-f]{64}", data["previous_sha256"]) is None))
                        or type(data["expected_sha256"]) is not str
                        or re.fullmatch(r"[0-9a-f]{64}", data["expected_sha256"]) is None):
                    raise AttemptIdentityError("Abandoned checkpoint intent is invalid")
            elif transition == "attempt_prepared":
                if (set(data) != {"checkpoint_sha256"}
                        or type(data["checkpoint_sha256"]) is not str
                        or re.fullmatch(r"[0-9a-f]{64}", data["checkpoint_sha256"]) is None):
                    raise AttemptIdentityError("Attempt preparation binding is invalid")
            elif transition in {"dispatch_started", "dispatch_returned"}:
                if (set(data) != {"checkpoint_sha256"}
                        and not (transition == "dispatch_started"
                                 and set(data) == {"checkpoint_sha256", "dispatch_number"})):
                    raise AttemptIdentityError("Dispatch transition fields are invalid")
                if data["checkpoint_sha256"] is not None and (
                        type(data["checkpoint_sha256"]) is not str
                        or re.fullmatch(r"[0-9a-f]{64}", data["checkpoint_sha256"]) is None):
                    raise AttemptIdentityError("Dispatch checkpoint binding is invalid")
                if transition == "dispatch_started" and (
                        type(data.get("dispatch_number")) is not int
                        or data["dispatch_number"] < 1):
                    raise AttemptIdentityError("Dispatch sequence is invalid")
            elif transition == "dispatch_not_entered":
                if (set(data) != {"reason", "checkpoint_sha256"}
                        or data["reason"] not in {"maintenance", "storage", "cancelled",
                                                  "connection_preflight", "transfer_preflight"}
                        or (data["checkpoint_sha256"] is not None
                            and (type(data["checkpoint_sha256"]) is not str
                                 or re.fullmatch(r"[0-9a-f]{64}", data["checkpoint_sha256"]) is None))):
                    raise AttemptIdentityError("Dispatch non-entry evidence is invalid")
            elif transition == "retained_transfer_retry_authorized":
                if (set(data) != {"proof_sha256"} or type(data["proof_sha256"]) is not str
                        or re.fullmatch(r"[0-9a-f]{64}", data["proof_sha256"]) is None):
                    raise AttemptIdentityError("Retained transfer retry proof is invalid")
            elif transition == "terminal_intent":
                if set(data) != {"finished"} or type(data["finished"]) is not dict:
                    raise AttemptIdentityError("Attempt terminal intent is invalid")
            elif transition == "settled":
                if (set(data) != {"checkpoint_sha256", "outcome_sha256"}
                        or any(type(data[name]) is not str
                               or re.fullmatch(r"[0-9a-f]{64}", data[name]) is None
                               for name in data)):
                    raise AttemptIdentityError("Attempt settlement fields are invalid")
            elif transition == "background_transferred":
                if (set(data) != {"checkpoint_sha256"}
                        or (data["checkpoint_sha256"] is not None
                            and (type(data["checkpoint_sha256"]) is not str
                                 or re.fullmatch(r"[0-9a-f]{64}", data["checkpoint_sha256"]) is None))):
                    raise AttemptIdentityError("Background ownership transfer is invalid")

    @staticmethod
    def _validate_identity(identity: object) -> None:
        fields = {"id", "origin", "action", "plan_id", "step_index", "step_sha256",
                  "started_tick", "started_at_utc", "process_id", "expected_unit_number",
                  "receipt", "session_id", "target"}
        if type(identity) is not dict or set(identity) != fields:
            raise AttemptIdentityError("Attempt identity tuple fields are invalid")
        legacy = identity["origin"] == "legacy"
        pattern = r"legacy:[0-9a-f]{64}" if legacy else r"[0-9a-f]{32}"
        if (type(identity["id"]) is not str or re.fullmatch(pattern, identity["id"]) is None
                or identity["origin"] not in {"legacy", "new"}
                or type(identity["action"]) is not str or not identity["action"]
                or len(identity["action"]) > 128
                or type(identity["plan_id"]) is not str or not identity["plan_id"]
                or len(identity["plan_id"]) > 256
                or type(identity["step_index"]) is not int or not 0 <= identity["step_index"] < 32
                or type(identity["step_sha256"]) is not str
                or re.fullmatch(r"[0-9a-f]{64}", identity["step_sha256"]) is None
                or type(identity["started_tick"]) is not int or identity["started_tick"] < 0
                or type(identity["session_id"]) is not str or not identity["session_id"]
                or type(identity["target"]) is not str or not identity["target"]):
            raise AttemptIdentityError("Attempt identity tuple is invalid")
        unit = identity["expected_unit_number"]
        if unit is not None and (type(unit) is not int or unit < 1):
            raise AttemptIdentityError("Attempt identity unit is invalid")
        receipt = identity["receipt"]
        if receipt is not None and (type(receipt) is not str or not 1 <= len(receipt) <= 128):
            raise AttemptIdentityError("Attempt identity receipt is invalid")
        if legacy:
            if identity["started_at_utc"] is not None or identity["process_id"] is not None:
                raise AttemptIdentityError("Legacy attempt timing is invalid")
        elif (type(identity["started_at_utc"]) is not str or len(identity["started_at_utc"]) > 40
              or type(identity["process_id"]) is not str
              or re.fullmatch(r"[0-9a-f]{32}", identity["process_id"]) is None):
            raise AttemptIdentityError("Attempt start provenance is invalid")

    @staticmethod
    def identity(memory, attempt: dict) -> dict:
        identity = {key: attempt.get(key) for key in (
            "id", "origin", "action", "plan_id", "step_index", "step_sha256",
            "started_tick", "started_at_utc", "process_id", "expected_unit_number", "receipt")}
        identity.update(session_id=memory.session_id, target=memory.target)
        AttemptIdentityArchive._validate_identity(identity)
        return identity

    def _append(self, transition: str, memory, *, identity=None, data: dict | None = None) -> dict:
        self._require_owner()
        if not self._loaded:
            raise AttemptIdentityError("Attempt journal is not loaded under its owner lock")
        if not self._records and transition not in {"campaign_anchor", "legacy_migration_anchor"}:
            raise AttemptIdentityError("Attempt journal must be anchored before appending")
        if (memory.session_id, memory.target) != self._identity:
            raise AttemptIdentityError("Attempt journal session or target changed")
        if identity is not None:
            self._validate_identity(identity)
            if (identity["session_id"], identity["target"]) != self._identity:
                raise AttemptIdentityError("Attempt tuple belongs to another campaign")
        if len(self._records) >= MAX_ATTEMPT_JOURNAL_RECORDS:
            raise AttemptIdentityError("Attempt identity journal is full; no records are evicted")
        record = {
            "schema": 1, "sequence": len(self._records), "previous_sha256": self._head,
            "checkpoint_key_sha256": self.checkpoint_key,
            "session_id": memory.session_id, "target": memory.target,
            "transition": transition,
            "attempt_id": identity["id"] if identity is not None else None,
            "identity": identity, "data": data or {},
        }
        record["record_sha256"] = _sha(record)
        line = _canonical(record) + b"\n"
        if len(line) > MAX_ATTEMPT_JOURNAL_RECORD_BYTES or len(self._raw) + len(line) > MAX_ATTEMPT_JOURNAL_BYTES:
            raise AttemptIdentityError("Attempt identity journal capacity reached; no records are evicted")
        try:
            live = self._read_file(self.path, maximum=MAX_ATTEMPT_JOURNAL_BYTES)
        except FileNotFoundError:
            live = b""
        if live != self._raw:
            raise AttemptIdentityError("Attempt identity journal changed outside its owner")
        descriptor = _open_regular(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            before = os.fstat(descriptor)
            view = memoryview(line)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("Attempt identity journal append made no progress")
                view = view[written:]
            os.fsync(descriptor)
            after = os.fstat(descriptor)
            path_stamp = _stamp(self.path)
            if (path_stamp is None or path_stamp[:2] != (after.st_dev, after.st_ino)
                    or before.st_size + len(line) != after.st_size):
                raise AttemptIdentityError("Attempt identity journal changed during append")
            if before.st_size == 0:
                _sync_directory(self.directory)
        finally:
            os.close(descriptor)
        self._records.append(record)
        self._raw += line
        self._head = record["record_sha256"]
        if transition == "checkpoint_bound":
            self._checkpoint_sha256 = record["data"]["checkpoint_sha256"]
        return record

    def _anchor(self, memory, checkpoint_sha256: str | None,
                baseline_outcomes: list[dict] | None = None) -> None:
        if self._records:
            if self._identity != (memory.session_id, memory.target):
                raise AttemptIdentityError("Checkpoint belongs to another attempt journal")
            return
        self._identity = (memory.session_id, memory.target)
        self._append("campaign_anchor", memory, data={
            "checkpoint_sha256": checkpoint_sha256,
            "baseline_outcomes": baseline_outcomes or [],
        })
        self._checkpoint_sha256 = checkpoint_sha256

    def _baseline_outcomes(self, memory) -> list[dict]:
        return [{"id": row["id"], "sha256": _sha(row)} for row in memory.attempt_outcomes]

    def _parse_capture(self, raw: bytes, memory_type, target: str):
        if type(raw) is not bytes or len(raw) > 64 * 1024 * 1024:
            raise AttemptIdentityError("Checkpoint capture is invalid or oversized")
        try:
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object,
                              parse_constant=_reject_constant)
        except (UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise AttemptIdentityError("Checkpoint capture is malformed") from error
        if (type(data) is not dict or type(data.get("session_id")) is not str
                or not data["session_id"] or data.get("target") != target):
            raise AttemptIdentityError("Checkpoint capture identity is invalid")
        if memory_type is None:
            from .memory import checkpoint_memory_type
            memory_type = checkpoint_memory_type(data)
        try:
            from .memory import load_checkpoint_bytes
            memory = load_checkpoint_bytes(
                raw, data["session_id"], target, checkpoint_path=self.checkpoint,
                memory_type=memory_type)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise AttemptIdentityError(
                f"Composed checkpoint validation failed: {error}") from error
        index = getattr(memory, "_blocked_recovery_archive_index", None)
        if index is not None:
            index.close()
        return data, memory

    def validate_capture(self, raw: bytes, memory_type, target: str):
        """Validate exact checkpoint bytes and journal while the owner lock is held."""
        self._require_owner()
        _, memory = self._parse_capture(raw, memory_type, target)
        if self.checkpoint.is_symlink():
            raise AttemptIdentityError("Checkpoint must not be a symlink")
        current = self._read_file(self.checkpoint, maximum=64 * 1024 * 1024)
        if current != raw:
            raise AttemptIdentityError("Checkpoint changed during attempt identity validation")
        current_sha = hashlib.sha256(raw).hexdigest()
        if not self._records:
            if (memory.pending is not None or memory.attempt is not None
                    or getattr(memory, "background_attempt", None) is not None):
                raise AttemptIdentityError(
                    "Legacy pending work has no independently authorized attempt identity journal")
            self._anchor(memory, current_sha, self._baseline_outcomes(memory))
            return memory
        self._reconcile_checkpoint_intent(memory, current_sha)
        if (self._identity != (memory.session_id, memory.target)
                or current_sha != self._checkpoint_sha256):
            raise AttemptIdentityError("Checkpoint does not match its durable attempt journal")
        self._reconcile_bound_checkpoint(memory, current_sha)
        self._validate_memory(memory)
        if self._read_file(self.checkpoint, maximum=64 * 1024 * 1024) != raw:
            raise AttemptIdentityError("Checkpoint changed during attempt identity restore")
        return memory

    def validate_current(self, memory_type, target: str):
        """Validate the current path, allowing an as-yet-unsaved new campaign."""
        self._require_owner()
        _reject_leaf_link(self.checkpoint, "Checkpoint")
        if not self.checkpoint.exists():
            if self._records and self._checkpoint_sha256 is not None:
                raise AttemptIdentityError("Owned checkpoint disappeared")
            if not self._records:
                return None
            return None
        raw = self._read_file(self.checkpoint, maximum=64 * 1024 * 1024)
        return self.validate_capture(raw, memory_type, target)

    def _transition_state(self) -> tuple[dict[str, dict], list[dict]]:
        attempts: dict[str, dict] = {}
        baseline = []
        for record in self._records:
            transition = record["transition"]
            identity = record["identity"]
            data = record["data"]
            if transition in {"campaign_anchor", "legacy_migration_anchor"}:
                baseline = data["baseline_outcomes"]
                if transition == "legacy_migration_anchor":
                    for approved in data["approved_attempts"]:
                        identity = approved["identity"]
                        attempts[identity["id"]] = {
                            "identity": identity, "prepared": True,
                            "dispatch": (["returned"] if approved["checkpoint_dispatch"]
                                         in {"returned", "background_returned"}
                                         else ["started"]),
                            "dispatch_reasons": [None],
                            "terminal": None, "settled": False,
                            "background": approved["owner"] == "background_attempt",
                            "retry_authorized": False, "migration_authorized": True,
                        }
            elif transition in {"checkpoint_intent", "checkpoint_write_abandoned"}:
                continue
            elif transition == "checkpoint_bound":
                continue
            else:
                if identity is None:
                    raise AttemptIdentityError("Attempt transition has no identity tuple")
                state = attempts.setdefault(identity["id"], {
                    "identity": identity, "prepared": False, "dispatch": [],
                    "dispatch_reasons": [],
                    "terminal": None, "settled": False, "background": False,
                    "retry_authorized": False, "migration_authorized": False,
                })
                if state["identity"] != identity:
                    raise AttemptIdentityError("Attempt id was reused with a different identity tuple")
                if transition == "attempt_prepared":
                    if state["prepared"]:
                        raise AttemptIdentityError("Attempt prepared more than once")
                    state["prepared"] = True
                elif transition == "dispatch_started":
                    if (not state["prepared"] or state["terminal"] is not None
                            or (state["dispatch"] and state["dispatch"][-1] != "not_entered")
                            or (state["dispatch"] and not state["retry_authorized"])):
                        raise AttemptIdentityError("Attempt dispatch order is invalid")
                    state["dispatch"].append("started")
                    state["dispatch_reasons"].append(None)
                    state["retry_authorized"] = False
                elif transition == "dispatch_returned":
                    if not state["dispatch"] or state["dispatch"][-1] != "started":
                        raise AttemptIdentityError("Attempt return is not bound to dispatch entry")
                    state["dispatch"][-1] = "returned"
                elif transition == "dispatch_not_entered":
                    if state["dispatch"] and state["dispatch"][-1] == "started":
                        state["dispatch"][-1] = "not_entered"
                        state["dispatch_reasons"][-1] = data["reason"]
                    elif (not state["dispatch"] and state["prepared"]
                          and data["reason"] in {"maintenance", "storage", "cancelled"}):
                        state["dispatch"].append("not_entered")
                        state["dispatch_reasons"].append(data["reason"])
                    else:
                        raise AttemptIdentityError("Attempt non-entry is not bound to an eligible preflight")
                elif transition == "retained_transfer_retry_authorized":
                    if (not state["dispatch"] or state["dispatch"][-1] not in {"started", "not_entered"}
                            or state["retry_authorized"]
                            or set(data) != {"proof_sha256"}
                            or type(data["proof_sha256"]) is not str
                            or re.fullmatch(r"[0-9a-f]{64}", data["proof_sha256"]) is None):
                        raise AttemptIdentityError("Retained transfer retry authorization is invalid")
                    state["dispatch"][-1] = "not_entered"
                    state["dispatch_reasons"][-1] = "transfer_preflight"
                    state["retry_authorized"] = True
                elif transition == "terminal_intent":
                    from .telemetry import validate_attempt
                    finished = data.get("finished")
                    if set(data) != {"finished"} or type(finished) is not dict:
                        raise AttemptIdentityError("Attempt terminal record is malformed")
                    validate_attempt(finished, finished=True)
                    if (self.identity_from_finished(finished, identity) != identity
                            or (state["terminal"] is not None and state["terminal"] != finished)):
                        raise AttemptIdentityError("Attempt terminal record changed its original identity")
                    state["terminal"] = finished
                elif transition == "settled":
                    if state["terminal"] is None or state["settled"]:
                        raise AttemptIdentityError("Attempt settlement has no unique terminal record")
                    if set(data) != {"checkpoint_sha256", "outcome_sha256"}:
                        raise AttemptIdentityError("Attempt settlement binding is malformed")
                    state["settled"] = True
                elif transition == "background_transferred":
                    if not state["prepared"] or state["terminal"] is not None:
                        raise AttemptIdentityError("Background transfer is not bound to an open attempt")
                    state["background"] = True
        return attempts, baseline

    @staticmethod
    def identity_from_finished(finished: dict, expected: dict) -> dict:
        return {**{key: finished.get(key) for key in (
            "id", "origin", "action", "plan_id", "step_index", "step_sha256",
            "started_tick", "started_at_utc", "process_id", "expected_unit_number", "receipt")},
            "session_id": expected["session_id"], "target": expected["target"]}

    def _reconcile_checkpoint_intent(self, memory, current_sha: str) -> None:
        pending = self._records[-1] if self._records else None
        if pending is None or pending["transition"] != "checkpoint_intent":
            return
        data = pending["data"]
        if set(data) != {"previous_sha256", "checkpoint_sha256", "active_attempts",
                         "terminal_outcomes"}:
            raise AttemptIdentityError("Checkpoint write intent is malformed")
        if current_sha == data["checkpoint_sha256"]:
            self._append("checkpoint_bound", memory, data={
                "checkpoint_sha256": current_sha,
                "active_attempts": data["active_attempts"],
                "terminal_outcomes": data["terminal_outcomes"],
            })
            for identity in data["active_attempts"]:
                if not self._has_transition("attempt_prepared", identity["id"]):
                    self._append("attempt_prepared", memory, identity=identity,
                                 data={"checkpoint_sha256": current_sha})
            self._settle_from_checkpoint(memory, current_sha)
        elif current_sha == data["previous_sha256"]:
            self._append("checkpoint_write_abandoned", memory, data={
                "previous_sha256": current_sha,
                "expected_sha256": data["checkpoint_sha256"],
            })
        else:
            raise AttemptIdentityError("Checkpoint matches neither side of its write intent")

    def _has_transition(self, transition: str, attempt_id: str) -> bool:
        return any(row["transition"] == transition and row["attempt_id"] == attempt_id
                   for row in self._records)

    def _reconcile_bound_checkpoint(self, memory, current_sha: str) -> None:
        """Finish interrupted metadata publication from an exact durable binding.

        The checkpoint-bound record already commits the immutable active tuple
        set. This repairs a crash before its prepared/settled records, never a
        missing dispatch witness and never an unbound or changed checkpoint.
        """
        binding = next((row for row in reversed(self._records)
                        if row["transition"] == "checkpoint_bound"), None)
        if binding is None:
            return  # Explicit legacy anchors retain their separate authority.
        if (binding["data"]["checkpoint_sha256"] != current_sha
                or binding["data"]["active_attempts"] != self._active_identities(memory)):
            raise AttemptIdentityError("Checkpoint differs from its exact bound attempt set")
        attempts, _ = self._transition_state()
        missing = []
        for identity in binding["data"]["active_attempts"]:
            state = attempts.get(identity["id"])
            if state is None:
                missing.append(identity)
            elif state["identity"] != identity or not state["prepared"]:
                raise AttemptIdentityError("Bound attempt has inconsistent durable preparation")
        # Validate the complete resulting ownership before appending anything;
        # an unrelated missing/changed outcome must not gain metadata authority.
        self._validate_memory(memory, recovered_prepared=missing)
        for identity in missing:
            self._append("attempt_prepared", memory, identity=identity,
                         data={"checkpoint_sha256": current_sha})
        self._settle_from_checkpoint(memory, current_sha)

    def _validate_memory(self, memory, *, recovered_prepared=()) -> None:
        attempts, baseline = self._transition_state()
        for identity in recovered_prepared:
            if identity["id"] in attempts:
                raise AttemptIdentityError("Recovered attempt already has durable transitions")
            attempts[identity["id"]] = {
                "identity": identity, "prepared": True,
                "terminal": None, "settled": False,
            }
        baseline_by_id = {row["id"]: row["sha256"] for row in baseline}
        outcomes_by_id = {row["id"]: row for row in memory.attempt_outcomes}
        for row in memory.attempt_outcomes:
            state = attempts.get(row["id"])
            if state is not None and state["terminal"] is not None:
                if state["terminal"] != row:
                    raise AttemptIdentityError("Checkpoint outcome differs from its journal terminal")
            elif baseline_by_id.get(row["id"]) != _sha(row):
                raise AttemptIdentityError("Checkpoint contains an outcome without durable identity authority")
        active = {}
        for name in ("attempt", "background_attempt"):
            row = getattr(memory, name, None)
            if row is not None:
                identity = self.identity(memory, row)
                if identity["id"] in active:
                    raise AttemptIdentityError("Checkpoint repeats one active attempt owner")
                active[identity["id"]] = identity
        unresolved = {}
        for attempt_id, state in attempts.items():
            if state["settled"]:
                if attempt_id in active:
                    raise AttemptIdentityError("Settled attempt is still active in checkpoint")
                continue
            if state["terminal"] is not None and attempt_id in outcomes_by_id:
                continue
            unresolved[attempt_id] = state["identity"]
        if active != unresolved:
            raise AttemptIdentityError("Checkpoint active attempts differ from durable ownership")
        for attempt_id, identity in active.items():
            state = attempts.get(attempt_id)
            if state is None or not state["prepared"] or state["identity"] != identity:
                raise AttemptIdentityError("Active checkpoint attempt lacks its exact prepared anchor")
            if state["terminal"] is not None and attempt_id in outcomes_by_id:
                raise AttemptIdentityError("Terminal attempt has conflicting active checkpoint state")

    def _active_identities(self, memory) -> list[dict]:
        values = []
        for name in ("attempt", "background_attempt"):
            attempt = getattr(memory, name, None)
            if attempt is not None:
                values.append(self.identity(memory, attempt))
        if len({item["id"] for item in values}) != len(values):
            raise AttemptIdentityError("Checkpoint has duplicate active attempt owners")
        return sorted(values, key=lambda item: item["id"])

    def _active_attempt_bindings(self, memory) -> list[dict]:
        values = []
        for owner in ("attempt", "background_attempt"):
            attempt = getattr(memory, owner, None)
            if attempt is not None:
                dispatch = (memory.pending.get("dispatch") if owner == "attempt"
                            and isinstance(memory.pending, dict) else "background_returned")
                if owner == "attempt" and dispatch not in {"prepared", "ambiguous", "returned"}:
                    raise AttemptIdentityError("Legacy attempt has no valid pending dispatch state")
                values.append({"owner": owner, "identity": self.identity(memory, attempt),
                               "checkpoint_dispatch": dispatch})
        values.sort(key=lambda row: row["owner"])
        if (len(values) > 2
                or len({row["owner"] for row in values}) != len(values)
                or len({row["identity"]["id"] for row in values}) != len(values)):
            raise AttemptIdentityError("Checkpoint has duplicate active attempt identities")
        return values

    def _migration_capture(self, memory_type, target: str, session_id: str):
        self._require_owner()
        if self._records:
            raise AttemptIdentityError("Attempt identity journal is already initialized")
        if not isinstance(session_id, str) or not session_id:
            raise AttemptIdentityError("Migration requires the exact session identity")
        _reject_leaf_link(self.checkpoint, "Checkpoint")
        raw = self._read_file(self.checkpoint, maximum=64 * 1024 * 1024)
        data, memory = self._parse_capture(raw, memory_type, target)
        if data["session_id"] != session_id:
            raise AttemptIdentityError("Migration session does not match the captured checkpoint")
        if self._read_file(self.checkpoint, maximum=64 * 1024 * 1024) != raw:
            raise AttemptIdentityError("Checkpoint changed during migration capture")
        attempts = self._active_attempt_bindings(memory)
        if not attempts:
            raise AttemptIdentityError("Checkpoint has no pending attempt requiring migration")
        return raw, memory, attempts

    def inspect_legacy_migration(self, memory_type, target: str, session_id: str) -> dict:
        """Read the exact existing legacy tuple set for a later explicit approval."""
        self._require_owner()
        raw, memory, attempts = self._migration_capture(memory_type, target, session_id)
        return {
            "schema": 1,
            "checkpoint_sha256": hashlib.sha256(raw).hexdigest(),
            "session_id": memory.session_id,
            "target": memory.target,
            "approved_attempts": attempts,
            "approved_attempts_sha256": _sha(attempts),
        }

    def authorize_legacy_migration(self, memory_type, target: str, session_id: str,
                                   checkpoint_sha256: str,
                                   approved_attempts_sha256: str) -> dict:
        """One-use explicit owner approval for the exact captured pending tuple set.

        The first journal record carries both the campaign anchor and approval,
        so a crash cannot leave a half-issued migration authority. Restore never
        creates this record.
        """
        self._require_owner()
        if (type(checkpoint_sha256) is not str
                or re.fullmatch(r"[0-9a-f]{64}", checkpoint_sha256) is None
                or type(approved_attempts_sha256) is not str
                or re.fullmatch(r"[0-9a-f]{64}", approved_attempts_sha256) is None):
            raise AttemptIdentityError("Explicit migration approval requires both exact SHA-256 pins")
        raw, memory, attempts = self._migration_capture(memory_type, target, session_id)
        current_sha = hashlib.sha256(raw).hexdigest()
        identities_sha = _sha(attempts)
        if current_sha != checkpoint_sha256:
            raise AttemptIdentityError("Explicit approval checkpoint digest does not match current bytes")
        if identities_sha != approved_attempts_sha256:
            raise AttemptIdentityError("Explicit approval attempt tuple digest does not match")
        if self._read_file(self.checkpoint, maximum=64 * 1024 * 1024) != raw:
            raise AttemptIdentityError("Checkpoint changed before explicit approval was recorded")
        self._identity = (memory.session_id, memory.target)
        self._append("legacy_migration_anchor", memory, data={
            "checkpoint_sha256": current_sha,
            "baseline_outcomes": self._baseline_outcomes(memory),
            "approval_schema": 1,
            "approval_id": uuid4().hex,
            "approval_mode": "explicit_supervisor_cli",
            "approved_attempts": attempts,
            "approved_attempts_sha256": identities_sha,
        })
        self._checkpoint_sha256 = current_sha
        if self._read_file(self.checkpoint, maximum=64 * 1024 * 1024) != raw:
            raise AttemptIdentityError("Checkpoint changed while migration approval was recorded")
        self._validate_memory(memory)
        return {"approval_id": self._records[0]["data"]["approval_id"],
                "checkpoint_sha256": current_sha,
                "approved_attempts_sha256": identities_sha,
                "approved_attempt_count": len(attempts)}

    def _terminal_rows(self, memory) -> list[dict]:
        attempts, _ = self._transition_state()
        rows = []
        for row in memory.attempt_outcomes:
            state = attempts.get(row["id"])
            if state is not None and state["terminal"] == row:
                rows.append({"id": row["id"], "outcome_sha256": _sha(row)})
        return sorted(rows, key=lambda item: item["id"])

    def before_checkpoint(self, memory) -> dict | None:
        self._require_owner()
        if not self._records:
            try:
                current = self._read_file(self.checkpoint, maximum=64 * 1024 * 1024)
            except FileNotFoundError:
                current = None
            if current is not None:
                raise AttemptIdentityError("Existing checkpoint needs explicit preflight before save")
            self._anchor(memory, None, [])
        if (self._identity != (memory.session_id, memory.target)
                or (self._records[-1]["transition"] == "checkpoint_intent")):
            raise AttemptIdentityError("Attempt journal is not at a stable checkpoint boundary")
        from .checkpoint_io import checkpoint_data
        payload = json.dumps(checkpoint_data(memory), sort_keys=True,
                             allow_nan=False).encode("utf-8")
        expected = hashlib.sha256(payload).hexdigest()
        try:
            current = self._read_file(self.checkpoint, maximum=64 * 1024 * 1024)
            current_sha = hashlib.sha256(current).hexdigest()
        except FileNotFoundError:
            current_sha = None
        if current_sha != self._checkpoint_sha256:
            raise AttemptIdentityError("Checkpoint changed outside its attempt owner")
        if expected == current_sha:
            return {"expected_sha256": expected, "changed": False}
        active = self._active_identities(memory)
        terminal = self._terminal_rows(memory)
        self._append("checkpoint_intent", memory, data={
            "previous_sha256": current_sha,
            "checkpoint_sha256": expected,
            "active_attempts": active,
            "terminal_outcomes": terminal,
        })
        return {"expected_sha256": expected, "changed": True,
                "active_attempts": active, "terminal_outcomes": terminal}

    def after_checkpoint(self, token: dict, memory) -> None:
        self._require_owner()
        try:
            installed = self._read_file(self.checkpoint, maximum=64 * 1024 * 1024)
        except OSError as error:
            raise AttemptIdentityError("Checkpoint disappeared after its write") from error
        digest = hashlib.sha256(installed).hexdigest()
        if digest != token["expected_sha256"]:
            raise AttemptIdentityError("Installed checkpoint differs from its durable write intent")
        if token["changed"]:
            self._append("checkpoint_bound", memory, data={
                "checkpoint_sha256": digest,
                "active_attempts": token["active_attempts"],
                "terminal_outcomes": token["terminal_outcomes"],
            })
            for identity in token["active_attempts"]:
                if not self._has_transition("attempt_prepared", identity["id"]):
                    self._append("attempt_prepared", memory, identity=identity,
                                 data={"checkpoint_sha256": digest})
        self._settle_from_checkpoint(memory, digest)

    def _settle_from_checkpoint(self, memory, checkpoint_sha256: str) -> None:
        attempts, _ = self._transition_state()
        outcomes = {row["id"]: row for row in memory.attempt_outcomes}
        active_ids = {item["id"] for item in self._active_identities(memory)}
        for attempt_id, state in attempts.items():
            terminal = state["terminal"]
            if state["settled"] or terminal is None:
                continue
            if attempt_id in active_ids:
                continue
            row = outcomes.get(attempt_id)
            if row != terminal:
                raise AttemptIdentityError("Attempt terminal is absent from its checkpoint")
            self._append("settled", memory, identity=state["identity"], data={
                "checkpoint_sha256": checkpoint_sha256,
                "outcome_sha256": _sha(row),
            })

    def prepare_attempt(self, memory, attempt: dict) -> None:
        """Bind a freshly checkpointed attempt; normally called after save."""
        identity = self.identity(memory, attempt)
        if not self._has_transition("attempt_prepared", identity["id"]):
            if self._checkpoint_sha256 is None:
                raise AttemptIdentityError("Prepared attempt has no bound checkpoint")
            self._append("attempt_prepared", memory, identity=identity,
                         data={"checkpoint_sha256": self._checkpoint_sha256})

    def dispatch_started(self, memory, attempt_id: str) -> None:
        attempts, _ = self._transition_state()
        state = attempts.get(attempt_id)
        attempt = memory.attempt or getattr(memory, "background_attempt", None)
        if attempt is None or attempt["id"] != attempt_id:
            raise AttemptIdentityError("Dispatch attempt is not the current checkpoint owner")
        identity = self.identity(memory, attempt)
        if state is None or not state["prepared"] or state["identity"] != identity:
            raise AttemptIdentityError("Dispatch lacks its exact prepared identity anchor")
        if state["terminal"] is not None or state["settled"]:
            raise AttemptIdentityError("A terminal attempt cannot be dispatched again")
        if state["dispatch"] and state["dispatch"][-1] != "not_entered":
            raise AttemptIdentityError("An ambiguous or returned attempt cannot be dispatched again")
        if state["dispatch"] and not state["retry_authorized"]:
            raise AttemptIdentityError("Retained transfer has no exact retry authority")
        self._append("dispatch_started", memory, identity=identity,
                     data={"checkpoint_sha256": self._checkpoint_sha256,
                           "dispatch_number": len(state["dispatch"]) + 1})

    def dispatch_returned(self, memory, attempt_id: str) -> None:
        attempt = memory.attempt or getattr(memory, "background_attempt", None)
        if attempt is None or attempt["id"] != attempt_id:
            raise AttemptIdentityError("Returned dispatch lost its original attempt")
        self._append("dispatch_returned", memory, identity=self.identity(memory, attempt),
                     data={"checkpoint_sha256": self._checkpoint_sha256})

    def dispatch_not_entered(self, memory, attempt_id: str, reason: str) -> None:
        attempt = memory.attempt or getattr(memory, "background_attempt", None)
        if attempt is None or attempt["id"] != attempt_id:
            raise AttemptIdentityError("Non-entry proof lost its original attempt")
        self._append("dispatch_not_entered", memory, identity=self.identity(memory, attempt),
                     data={"reason": reason, "checkpoint_sha256": self._checkpoint_sha256})

    def authorize_retained_transfer_retry(self, memory, attempt: dict, proof: dict) -> None:
        """Preserve only the existing exact proof that the transfer RPC never began."""
        self._require_owner()
        identity = self.identity(memory, attempt)
        attempts, _ = self._transition_state()
        state = attempts.get(identity["id"])
        if (state is None or state["identity"] != identity or state["terminal"] is not None
                or state["settled"] or not state["dispatch"]
                or state["dispatch"][-1] != "started" or state["retry_authorized"]
                or attempt["origin"] != "new"
                or attempt["dispatch_phases"].get("dispatch", {}).get("status") != "started"
                or "transfer_rpc" in attempt["dispatch_phases"]):
            raise AttemptIdentityError("Retained transfer retry does not match the narrow no-RPC proof")
        proof_sha = _sha({"attempt": identity, "proof": proof,
                          "reserved_source_sha256": _sha(memory.reservations)})
        self._append("retained_transfer_retry_authorized", memory, identity=identity,
                     data={"proof_sha256": proof_sha})

    def mark_background_transferred(self, memory, attempt: dict) -> None:
        identity = self.identity(memory, attempt)
        attempts, _ = self._transition_state()
        state = attempts.get(identity["id"])
        if state is None or state["identity"] != identity or not state["prepared"]:
            raise AttemptIdentityError("Background transfer lacks its original prepared attempt")
        if not state["background"]:
            self._append("background_transferred", memory, identity=identity,
                         data={"checkpoint_sha256": self._checkpoint_sha256})

    def require_safe_plan_clear(self, memory, attempt: dict,
                                background_attempt: dict | None) -> None:
        """Permit clearing only after terminal settlement or exact background transfer."""
        identity = self.identity(memory, attempt)
        attempts, _ = self._transition_state()
        state = attempts.get(identity["id"])
        if (state is not None and state["identity"] == identity
                and state["background"] and isinstance(background_attempt, dict)
                and self.identity(memory, background_attempt) == identity):
            return
        raise AttemptIdentityError(
            "Cannot clear a plan while its exact attempt remains checkpoint-owned")

    def prepare_terminal(self, memory, finished: dict) -> dict:
        self._require_owner()
        from .telemetry import validate_attempt
        validate_attempt(finished, finished=True)
        identity = self.identity_from_finished(finished, {
            "session_id": memory.session_id, "target": memory.target})
        self._validate_identity(identity)
        attempts, _ = self._transition_state()
        state = attempts.get(identity["id"])
        if state is None or state["identity"] != identity or not state["prepared"]:
            raise AttemptIdentityError("Attempt terminal lacks its original prepared identity")
        if state["settled"]:
            if (state["terminal"] is None
                    or state["terminal"].get("outcome") != finished.get("outcome")):
                raise AttemptIdentityError("Attempt terminal changed after settlement")
            return state["terminal"]
        if state["terminal"] is not None:
            if state["terminal"].get("outcome") != finished.get("outcome"):
                raise AttemptIdentityError("Attempt terminal differs from its retained first result")
            return state["terminal"]
        self._append("terminal_intent", memory, identity=identity,
                     data={"finished": finished})
        return finished

    def validate_terminal_or_pending(self, attempt: dict) -> dict | None:
        """Return the exact first terminal row, if a crash left it unsettled."""
        attempts, _ = self._transition_state()
        state = attempts.get(attempt.get("id")) if isinstance(attempt, dict) else None
        if state is None:
            raise AttemptIdentityError("Attempt has no durable journal identity")
        memory_identity = {**{key: attempt.get(key) for key in (
            "id", "origin", "action", "plan_id", "step_index", "step_sha256",
            "started_tick", "started_at_utc", "process_id", "expected_unit_number", "receipt")},
            "session_id": state["identity"]["session_id"],
            "target": state["identity"]["target"]}
        if memory_identity != state["identity"]:
            raise AttemptIdentityError("Current attempt differs from durable original identity")
        return state["terminal"]

    def terminal_context(self, memory, attempt: dict) -> dict:
        """Return terminal and dispatch facts after rechecking the full attempt tuple."""
        identity = self.identity(memory, attempt)
        attempts, _ = self._transition_state()
        state = attempts.get(identity["id"])
        if state is None or state["identity"] != identity:
            raise AttemptIdentityError("Attempt has no durable journal identity")
        if (not state["dispatch"]
                or len(state["dispatch"]) != len(state["dispatch_reasons"])):
            dispatch, reason = None, None
        else:
            dispatch, reason = state["dispatch"][-1], state["dispatch_reasons"][-1]
        return {"terminal": state["terminal"], "dispatch": dispatch,
                "dispatch_reason": reason, "background": state["background"]}

    def baseline_outcomes(self) -> dict[str, str]:
        if not self._records:
            return {}
        return {row["id"]: row["sha256"] for row in self._records[0]["data"]["baseline_outcomes"]}

    def owner_status(self) -> dict:
        """Small diagnostic suitable for tests; never exposes an action payload."""
        self._require_owner()
        attempts, _ = self._transition_state()
        return {"record_count": len(self._records), "head_sha256": self._head,
                "checkpoint_sha256": self._checkpoint_sha256,
                "active_attempt_ids": sorted(key for key, row in attempts.items()
                                              if not row["settled"])}
