"""Read-only typed completion gate for an existing campaign owner.

This module does not own a controller, backend, provider, or world connection.
The existing owner supplies its retained RCON client and runtime identity.
When a checkpoint retains native feature owners, a source-pinned direct query
projects those records without invoking the mutating campaign/module observer
chain. A missing or unsupported observation is unknown and fails closed.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any


_SOURCE_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_NATIVE_FIELDS = {"tick", "speed", "marked", "runtime", "session_id", "connected"}


@dataclass(frozen=True)
class OwnerCompletionWitness:
    """Non-authorizing summary of a successful, stable preflight."""

    checkpoint_sha256: str
    session_id: str
    target: str
    checkpoint_tick: int
    observed_tick: int
    source_commit: str
    source_sha256: str
    owner_sha256: str
    observation_sha256: str | None = None


def _reject(condition: bool, message: str) -> None:
    if condition:
        raise ValueError(message)


def _unique_object(pairs):
    """Reject duplicate JSON keys instead of accepting parser last-write-wins."""
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate key in completion checkpoint")
        value[key] = item
    return value


def _stable_regular_read(path: Path, *, maximum: int) -> bytes:
    """Read one bounded regular file without following a final-component link."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or path.is_symlink() or before.st_size > maximum:
        raise ValueError("Completion input is not a bounded regular file")
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        identity = (before.st_dev, before.st_ino, before.st_size,
                    before.st_mtime_ns, before.st_ctime_ns, before.st_mode)
        if (not stat.S_ISREG(opened.st_mode)
                or identity != (opened.st_dev, opened.st_ino, opened.st_size,
                                opened.st_mtime_ns, opened.st_ctime_ns, opened.st_mode)):
            raise ValueError("Completion input changed while it was opened")
        parts = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, maximum + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise ValueError("Completion input exceeds its bound")
            parts.append(chunk)
        after_fd = os.fstat(descriptor)
        after_path = path.lstat()
        if (identity != (after_fd.st_dev, after_fd.st_ino, after_fd.st_size,
                         after_fd.st_mtime_ns, after_fd.st_ctime_ns, after_fd.st_mode)
                or identity != (after_path.st_dev, after_path.st_ino, after_path.st_size,
                                after_path.st_mtime_ns, after_path.st_ctime_ns, after_path.st_mode)
                or path.is_symlink()):
            raise ValueError("Completion input changed during its read")
        return b"".join(parts)
    finally:
        os.close(descriptor)


def _checkpoint_document(raw: bytes) -> dict:
    def invalid_constant(value):
        raise ValueError(f"Invalid checkpoint number: {value}")
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                              parse_constant=invalid_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("Completed checkpoint JSON is invalid") from error
    if type(document) is not dict:
        raise ValueError("Completed checkpoint is not an object")
    return document


def _pinned_source(value: object) -> dict[str, str]:
    if (type(value) is not dict or set(value) != {"commit", "source_sha256"}
            or type(value.get("commit")) is not str
            or _SOURCE_COMMIT.fullmatch(value["commit"]) is None
            or type(value.get("source_sha256")) is not str
            or _SHA256.fullmatch(value["source_sha256"]) is None):
        raise ValueError("Completed owner requires an exact source revision pin")
    return {"commit": value["commit"], "source_sha256": value["source_sha256"]}


def _owner_digest(path: Path) -> str:
    return hashlib.sha256(_stable_regular_read(path, maximum=16 * 1024 * 1024)).hexdigest()


def _snapshot_digest(snapshot) -> str:
    try:
        encoded = json.dumps(asdict(snapshot), sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("Current completion observation is not a stable typed snapshot") from error
    return hashlib.sha256(encoded).hexdigest()


def _validate_native_identity(identity: object, expected_session: str,
                              checkpoint_tick: int) -> int:
    if type(identity) is not dict or set(identity) != _NATIVE_FIELDS:
        raise ValueError("Completed owner runtime identity is incomplete")
    tick = identity["tick"]
    speed = identity["speed"]
    connected = identity["connected"]
    if (type(checkpoint_tick) is not int or checkpoint_tick < 0
            or type(identity["marked"]) is not bool or identity["marked"] is not True
            or type(identity["runtime"]) is not bool or identity["runtime"] is not True
            or type(identity["session_id"]) is not str
            or identity["session_id"] != expected_session
            or type(tick) is not int or tick < 0 or tick < checkpoint_tick
            or type(speed) not in (int, float) or not math.isfinite(speed) or speed != 1
            or type(connected) is not int or connected != 1):
        raise ValueError("Current native runtime does not match the completed checkpoint")
    return tick


def _goal_completion(memory, target: str) -> None:
    completed_tick = memory.completed_goals.get(target)
    if type(completed_tick) is not int or not 0 <= completed_tick <= memory.last_tick:
        raise ValueError("Completed checkpoint lacks its target milestone receipt")
    matching = [row for row in memory.history
                if row.get("kind") == "goal_completed"
                and row.get("goal") == target
                and type(row.get("tick")) is int
                and row["tick"] == completed_tick]
    if len(matching) != 1:
        raise ValueError("Completed checkpoint lacks one controller-authored target event")


def _unresolved_feature_owners(memory) -> None:
    """Reject durable work phases; do not reject completed receipts by presence."""
    if any(getattr(memory, key, None) is not None for key in (
            "background_job", "background_attempt", "background_step",
            "capital_investment", "solid_funding", "coal_funding")):
        raise ValueError("Completed checkpoint retains an active background or funding owner")

    projects = getattr(memory, "successor_projects", {})
    if (not isinstance(projects, dict)
            or any(not isinstance(row, dict) or row.get("status") != "qualified"
                   for row in projects.values())):
        raise ValueError("Completed checkpoint retains an unfinished successor project")

    connector = memory.connector_ownership
    if connector is not None:
        routes = connector.get("routes") if isinstance(connector, dict) else None
        if (not isinstance(routes, dict)
                or any(not isinstance(row, dict) or row.get("state") != "complete"
                       or row.get("pending") is not None for row in routes.values())):
            raise ValueError("Completed checkpoint retains an unfinished connector route")

    # These checkpoint maps retain paid prefixes but not a current native
    # flow certificate.  Their presence is resolved against the supplied
    # producer observation below; do not infer terminality from nonemptiness.


def _validate_current_owners(memory, snapshot, connector_details: object) -> None:
    """Reconcile each retained paid feature owner with one exact snapshot."""
    from .state import GameSnapshot

    if type(snapshot) is not GameSnapshot:
        raise ValueError("Feature ownership requires a current typed native observation")
    if (snapshot.session_id != memory.session_id or type(snapshot.tick) is not int
            or snapshot.tick < memory.last_tick or not isinstance(snapshot.factory, dict)):
        raise ValueError("Feature observation is stale or belongs to another session")

    def rows_for(name: str) -> dict:
        data = snapshot.factory.get(name)
        if data is None:
            return {}
        if (not isinstance(data, dict) or data.get("session_id") != snapshot.session_id
                or type(data.get("tick")) is not int or data["tick"] != snapshot.tick
                or not isinstance(data.get("sources"), dict)):
            raise ValueError(f"Current {name} producer projection is malformed or stale")
        return data["sources"]

    # Production-site rows carry native source units but do not have their own
    # checkpoint extension. Accept an owned row only when another retained
    # paid family binds that same source, or a successor project binds its
    # anchor and unit. Proposals/rejections are observations, not paid owners.
    from . import production_sites
    site_data = snapshot.factory.get("production_sites")
    site_rows = production_sites.sources(snapshot) if site_data is not None else {}
    bound_site_units: dict[str, int] = {}

    def bind_site(source: str, unit: object) -> None:
        if type(unit) is not int or unit <= 0:
            raise ValueError("Retained production-site source unit is invalid")
        previous = bound_site_units.setdefault(source, unit)
        if previous != unit:
            raise ValueError("Retained production-site families disagree on source identity")

    if hasattr(memory, "input_routes_schema"):
        for source, saved in memory.input_commitments.items():
            bind_site(source, saved.get("source_unit"))
    if hasattr(memory, "output_buffers_schema"):
        from . import output_buffers
        for source, saved in output_buffers.expected_commitments(memory).items():
            bind_site(source, saved.get("source_unit"))
    if hasattr(memory, "successor_schema"):
        for source, project in memory.successor_projects.items():
            if project.get("source_unit", 0):
                bind_site(source, project["source_unit"])
    for source, row in site_rows.items():
        if row.get("state") == "owned" and bound_site_units.get(source) != row.get("source_unit"):
            raise ValueError("Current production-site owner lacks a matching checkpoint binding")

    if hasattr(memory, "output_buffers_schema"):
        from . import output_buffers
        rows = output_buffers.sources(snapshot)
        expected = output_buffers.expected_commitments(memory)
        for source, saved in expected.items():
            row = rows.get(source)
            if (not isinstance(row, dict) or not output_buffers.current(row, snapshot)
                    or row.get("source") != source or row.get("layout") != saved["layout"]
                    or row.get("source_unit") != saved["source_unit"]
                    or row.get("parts") != saved["parts"]
                    or not output_buffers.flow_complete(source, saved["layout"], snapshot)):
                raise ValueError("Retained output ownership lacks current commissioned flow")
        for source, row in rows.items():
            if (row.get("state") == "fault"
                    or row.get("state") != "proposed" and source not in expected):
                raise ValueError("Current observation contains an unowned output route")
    else:
        from . import output_buffers
        rows = output_buffers.sources(snapshot) if snapshot.factory.get("output_buffers") is not None else {}
        if any(row.get("state") != "proposed" for row in rows.values()):
            raise ValueError("Native output-buffer ownership has no checkpoint binding")

    if hasattr(memory, "input_routes_schema"):
        from . import input_routes
        rows = input_routes.sources(snapshot)
        expected = memory.input_commitments
        for source, saved in expected.items():
            row = rows.get(source)
            if (not isinstance(row, dict) or not input_routes.current(row, snapshot)
                    or row.get("state") != "ready" or row.get("layout") != saved["layout"]
                    or row.get("source_unit") != saved["source_unit"]
                    or row.get("parts") != saved["parts"]
                    or not input_routes.flow_complete(source, saved["layout"], snapshot)):
                raise ValueError("Retained input ownership lacks current complete flow")
        for source, row in rows.items():
            if (row.get("state") == "fault"
                    or row.get("state") != "proposed" and source not in expected):
                raise ValueError("Current observation contains an unowned input route")
    else:
        from . import input_routes
        rows = input_routes.sources(snapshot) if snapshot.factory.get("input_routes") is not None else {}
        if rows:
            raise ValueError("Native input-route ownership has no checkpoint binding")

    if hasattr(memory, "outposts_schema"):
        from . import mining_outposts
        rows = mining_outposts.sources(snapshot)
        expected = memory.outpost_commitments
        for resource, saved in expected.items():
            row = rows.get(resource)
            if (not isinstance(row, dict) or not mining_outposts.current(row, snapshot)
                    or row.get("state") not in {"ready", "depleted"}
                    or any(row.get(key) != saved.get(key) for key in (
                        "layout", "surface_index", "force_index", "steps", "parts", "flow"))
                    or not mining_outposts.flow_complete(resource, saved["layout"], snapshot)):
                raise ValueError("Retained mining outpost lacks current complete flow")
        for resource, row in rows.items():
            if (row.get("state") in {"building", "fault"}
                    or row.get("state") != "proposed" and resource not in expected):
                raise ValueError("Current observation contains an unowned mining outpost")
    else:
        from . import mining_outposts
        rows = mining_outposts.sources(snapshot) if snapshot.factory.get("mining_outposts") is not None else {}
        if any(row.get("state") != "proposed" for row in rows.values()):
            raise ValueError("Native mining-outpost ownership has no checkpoint binding")

    if hasattr(memory, "successor_schema"):
        from . import successors as successor_contract
        rows = successor_contract.sources(snapshot)
        for source, project in memory.successor_projects.items():
            row = rows.get(source)
            retained = memory.successor_receipts.get(source, {})
            output = snapshot.factory.get("output_buffers", {}).get("sources", {}).get(source, {})
            route = snapshot.factory.get("input_routes", {}).get("sources", {}).get(source, {})
            if (project.get("status") != "qualified" or row is None
                    or not successor_contract.qualified(source, snapshot)
                    or retained.get("qualification") != row.get("qualification")
                    or retained.get("use") != row.get("use")
                    or retained.get("output") != output.get("parts", {})
                    or retained.get("input") != route.get("parts", {})):
                raise ValueError("Retained successor proof is not current")
            # This contract raises on malformed projects and returns None for a
            # valid project; it is not a boolean predicate.
            successor_contract.project_valid(project, source, snapshot.tick)
        for source, row in rows.items():
            if source not in memory.successor_projects and row.get("status") != "proposed":
                raise ValueError("Current observation contains an unowned successor project")
    else:
        from . import successors as successor_contract
        rows = (successor_contract.sources(snapshot)
                if snapshot.factory.get("successors") is not None else {})
        if rows:
            raise ValueError("Native successor ownership has no checkpoint binding")

    if hasattr(memory, "solid_routes_schema"):
        from . import solid_routes
        rows = solid_routes.completion_routes(snapshot)
        expected = memory.solid_commitments
        for route, saved in expected.items():
            row = rows.get(route)
            if (row is None or not solid_routes.current(row, snapshot)
                    or not solid_routes.reconciles(saved, row)
                    or not solid_routes.completion_flow_complete(route, row["layout"], snapshot)):
                raise ValueError("Retained solid route lacks current complete flow")
        for route, row in rows.items():
            if (row.get("state") == "fault" or row.get("pending")
                    or row.get("state") != "proposed" and route not in expected):
                raise ValueError("Current observation contains an unowned solid route")
    else:
        from . import solid_routes
        rows = solid_routes.routes(snapshot) if snapshot.factory.get("solid_routes") is not None else {}
        if any(row.get("state") != "proposed" or row.get("pending")
               for row in rows.values()):
            raise ValueError("Native solid-route ownership has no checkpoint binding")

    if hasattr(memory, "coal_supply_schema"):
        from . import coal_supply
        rows = coal_supply.completion_sources(snapshot)
        expected = memory.coal_commitments
        for target, saved in expected.items():
            row = rows.get(target)
            if (row is None or not coal_supply.current(row, snapshot)
                    or not coal_supply.reconciles(saved, row)
                    or not coal_supply.completion_flow_complete(snapshot)):
                raise ValueError("Retained coal route lacks current complete flow")
        for target, row in rows.items():
            if (row.get("state") in {"building", "fault", "awaiting_corridor", "observing_flow"}
                    or row.get("pending") or row.get("manual_pending")
                    or row.get("state") != "proposed" and target not in expected):
                raise ValueError("Current observation contains an unowned coal source")
    else:
        from . import coal_supply
        rows = coal_supply.sources(snapshot) if snapshot.factory.get("coal_supply") is not None else {}
        if any(row.get("state") != "proposed" or row.get("pending")
               or row.get("manual_pending") for row in rows.values()):
            raise ValueError("Native coal-supply ownership has no checkpoint binding")

    if memory.connector_ownership is not None:
        from .connector_checkpoint import validate_binding
        saved = validate_binding(memory.connector_ownership, memory.session_id)["routes"]
        if saved and (not isinstance(connector_details, dict) or set(connector_details) != set(saved)):
            raise ValueError("Connector route detail observations are missing")
        for receipt, detail in (connector_details or {}).items():
            if not isinstance(detail, dict) or detail != saved[receipt]:
                raise ValueError("Current connector detail differs from retained payment")
        observed = snapshot.factory.get("connector_ownership")
        if saved:
            if (not isinstance(observed, dict)
                    or observed.get("session_id") != memory.session_id
                    or observed.get("tick") != snapshot.tick
                    or not isinstance(observed.get("routes"), dict)):
                raise ValueError("Current connector summary is missing or stale")
            for receipt, saved_row in saved.items():
                row = observed["routes"].get(receipt)
                if (not isinstance(row, dict)
                        or any(row.get(key) != saved_row.get(key) for key in (
                            "id", "session_id", "source", "target", "source_unit", "target_unit",
                            "kind", "fluid", "actor_unit", "surface_index", "force_index",
                            "state", "pending", "paid", "external", "owned", "cell_count"))):
                    raise ValueError("Current connector summary differs from retained route")
    else:
        observed = snapshot.factory.get("connector_ownership")
        if observed is not None and (
                not isinstance(observed, dict)
                or observed.get("session_id") != memory.session_id
                or observed.get("tick") != snapshot.tick
                or not isinstance(observed.get("routes"), dict)
                or observed["routes"]):
            raise ValueError("Native connector ownership has no checkpoint binding")


def _capture_completed_checkpoint(
        checkpoint_path: str | Path, *, target: str, source_root: str | Path,
        expected_source_revision: dict[str, str], startup_source_revision: dict[str, str],
        owner_path: str | Path, expected_owner_sha256: str,
        startup_owner_sha256: str, native_identity: dict[str, Any]
):
    """Perform every local/durable gate before requesting a current observation."""
    from .main import _preflight_sync_async_rollback
    from .memory import checkpoint_memory_type, load_checkpoint_bytes
    from .provenance import source_revision
    from .supervisor import Supervisor

    path = Path(checkpoint_path)
    root = Path(source_root)
    owner = Path(owner_path)
    captured = _stable_regular_read(path, maximum=16 * 1024 * 1024)
    if not captured:
        raise ValueError("Completed checkpoint is empty")
    document = _checkpoint_document(captured)
    if type(document.get("session_id")) is not str or not document["session_id"]:
        raise ValueError("Completed checkpoint identity is missing")
    if document.get("target") != target:
        raise ValueError("Completed checkpoint target changed")
    session_id = document["session_id"]
    _reject(type(target) is not str or not target, "Completed target is invalid")

    expected_source = _pinned_source(expected_source_revision)
    startup_source = _pinned_source(startup_source_revision)
    if expected_source != startup_source:
        raise ValueError("Owner source changed before completion preflight")
    if (type(expected_owner_sha256) is not str or _SHA256.fullmatch(expected_owner_sha256) is None
            or type(startup_owner_sha256) is not str or _SHA256.fullmatch(startup_owner_sha256) is None
            or expected_owner_sha256 != startup_owner_sha256):
        raise ValueError("Owner script pin differs from startup")
    if _owner_digest(owner) != expected_owner_sha256:
        raise ValueError("Owner script changed after startup")
    observed_tick = _validate_native_identity(native_identity, session_id,
                                              document.get("last_tick", -1))

    if source_revision(root) != expected_source:
        raise ValueError("Source tree changed after owner startup")

    # This existing gate binds rollback, provider/WAL/archive state and the
    # original checkpoint bytes. It must run before any attachment query.
    async_capture = _preflight_sync_async_rollback(path, target)
    if async_capture != captured:
        raise ValueError("Checkpoint changed during async obligation preflight")

    memory_type = checkpoint_memory_type(document)
    memory = load_checkpoint_bytes(captured, session_id, target,
                                   checkpoint_path=path, memory_type=memory_type)
    archive_index = getattr(memory, "_blocked_recovery_archive_index", None)
    try:
        if (memory.status != "completed" or memory.target != target
                or memory.session_id != session_id):
            raise ValueError("Checkpoint is not a completed campaign for this runtime")
        _goal_completion(memory, target)
        if Supervisor.has_unresolved_work(document, path):
            raise ValueError("Completed checkpoint retains base or outer-selection work")
        _unresolved_feature_owners(memory)
    finally:
        if archive_index is not None:
            archive_index.close()

    if (source_revision(root) != expected_source
            or _owner_digest(owner) != expected_owner_sha256
            or _stable_regular_read(path, maximum=16 * 1024 * 1024) != captured):
        raise ValueError("Completion inputs changed during static preflight")
    return path, captured, document, memory, expected_source, observed_tick


def _requires_current_feature_observation(memory) -> bool:
    # Every native owner is checked against the retained runtime. Besides
    # reconciling declared families, this detects current paid families that
    # have no checkpoint extension instead of silently treating them as idle.
    return True


def _required_completion_modules(memory) -> frozenset[str]:
    """Map retained checkpoint families to their actual producer modules."""
    required = set()
    if hasattr(memory, "output_buffers_schema"):
        required.add("output_buffers")
    if hasattr(memory, "input_routes_schema"):
        required.update({"input_routes", "production_sites"})
    if hasattr(memory, "outposts_schema"):
        required.add("mining_outposts")
    if hasattr(memory, "successor_schema"):
        if hasattr(memory, "outposts_schema"):
            raise ValueError("Successor producer composition excludes mining outposts")
        required.update({"successors", "input_routes", "output_buffers",
                         "production_sites", "craft_jobs"})
    if hasattr(memory, "solid_routes_schema"):
        required.add("solid_routes")
    if hasattr(memory, "coal_supply_schema"):
        if not hasattr(memory, "solid_routes_schema"):
            raise ValueError("Coal supply completion requires its solid-route producer")
        required.update({"solid_routes", "coal_supply"})
    if memory.connector_ownership is not None:
        required.add("connector_ownership")
    return frozenset(required)


def _validate_completion_profile(memory, attachment: dict) -> None:
    """Require one exact bundled direct profile containing every checkpoint owner."""
    from .backends.native_current_attachment import (
        DIRECT_MODULE_PROFILES, source_bound_direct_profile,
    )

    profile = source_bound_direct_profile(attachment)
    if profile not in DIRECT_MODULE_PROFILES:
        raise ValueError("Native feature composition is not a source-reviewed direct profile")
    required = _required_completion_modules(memory)
    installed = frozenset(name for name, enabled in attachment["modules"].items() if enabled)
    if not required <= installed:
        raise ValueError("Current attachment omits a retained checkpoint owner module")
    if hasattr(memory, "successor_schema") and "mining_outposts" in installed:
        raise ValueError("Successor attachment cannot compose with mining outposts")


def observe_current_owner_snapshot(
        checkpoint_path: str | Path, *, target: str, source_root: str | Path,
        expected_source_revision: dict[str, str], startup_source_revision: dict[str, str],
        owner_path: str | Path, expected_owner_sha256: str,
        startup_owner_sha256: str, native_identity: dict[str, Any], client
):
    """Read one current typed snapshot through the existing native owner.

    All local checkpoint, rollback, source, and work-phase gates run first. The
    supplied RCON client is borrowed from the existing owner; this function
    never opens a client, starts FLE, installs assets, dispatches an action, or
    closes the borrowed connection. A source-bound direct attachment query
    captures the real composed producer records without calling any mutating
    observer. The returned tuple is non-authorizing until the complete typed
    checkpoint and durable-obligation gates pass.
    """
    from .backends.native_attachment import readback
    from .backends.native_owner_completion_attachment import observe_current_owner_snapshot as read_snapshot
    from .provenance import source_revision

    if not callable(getattr(client, "send_command", None)):
        raise ValueError("Completion observation requires the existing owner RCON client")

    path, captured, document, memory, expected_source, _ = _capture_completed_checkpoint(
        checkpoint_path, target=target, source_root=source_root,
        expected_source_revision=expected_source_revision,
        startup_source_revision=startup_source_revision, owner_path=owner_path,
        expected_owner_sha256=expected_owner_sha256,
        startup_owner_sha256=startup_owner_sha256, native_identity=native_identity)
    attachment = readback(client, completion_query=True)
    _validate_completion_profile(memory, attachment)
    snapshot, details = read_snapshot(
        client, checkpoint_binding=memory.connector_ownership)
    if (snapshot.world_kind != "fle" or snapshot.session_id != memory.session_id
            or type(snapshot.tick) is not int or snapshot.tick < memory.last_tick
            or snapshot.factory.get("tick") != snapshot.tick):
        raise ValueError("Current owner snapshot is stale or belongs to another runtime")

    if (source_revision(Path(source_root)) != expected_source
            or _owner_digest(Path(owner_path)) != expected_owner_sha256
            or _stable_regular_read(path, maximum=16 * 1024 * 1024) != captured):
        raise ValueError("Completion authority changed during native observation")
    return snapshot, details


def preflight_completed_owner(
        checkpoint_path: str | Path, *, target: str, source_root: str | Path,
        expected_source_revision: dict[str, str], startup_source_revision: dict[str, str],
        owner_path: str | Path, expected_owner_sha256: str,
        startup_owner_sha256: str, native_identity: dict[str, Any], client
) -> OwnerCompletionWitness:
    """Validate a completed checkpoint against one stable current observation.

    Structural receipts alone never authorize owner completion, including when
    the checkpoint happens to contain no optional feature extension. The
    current owners require the retained RCON client. The function obtains its
    own source-qualified current snapshot so a caller cannot substitute a
    hand-built ``GameSnapshot`` or connector-detail map.
    """
    from .main import _preflight_sync_async_rollback
    from .provenance import source_revision

    path, captured, _document, memory, expected_source, observed_tick = (
        _capture_completed_checkpoint(
            checkpoint_path, target=target, source_root=source_root,
            expected_source_revision=expected_source_revision,
            startup_source_revision=startup_source_revision, owner_path=owner_path,
            expected_owner_sha256=expected_owner_sha256,
            startup_owner_sha256=startup_owner_sha256, native_identity=native_identity))
    current_snapshot, connector_details = observe_current_owner_snapshot(
        path, target=target, source_root=source_root,
        expected_source_revision=expected_source_revision,
        startup_source_revision=startup_source_revision,
        owner_path=owner_path, expected_owner_sha256=expected_owner_sha256,
        startup_owner_sha256=startup_owner_sha256, native_identity=native_identity,
        client=client)
    snapshot_fingerprint = _snapshot_digest(current_snapshot)
    if (current_snapshot.tick < observed_tick or current_snapshot.world_kind != "fle"):
        raise ValueError("Feature observation predates or is unrelated to the runtime probe")
    _validate_current_owners(memory, current_snapshot, connector_details)

    if (source_revision(Path(source_root)) != expected_source
            or _owner_digest(Path(owner_path)) != expected_owner_sha256
            or _preflight_sync_async_rollback(path, target) != captured
            or _stable_regular_read(path, maximum=16 * 1024 * 1024) != captured
            or _snapshot_digest(current_snapshot) != snapshot_fingerprint):
        raise ValueError("Completion authority changed during native preflight")
    return OwnerCompletionWitness(
        checkpoint_sha256=hashlib.sha256(captured).hexdigest(),
        session_id=memory.session_id,
        target=target,
        checkpoint_tick=memory.last_tick,
        observed_tick=observed_tick,
        source_commit=expected_source["commit"],
        source_sha256=expected_source["source_sha256"],
        owner_sha256=expected_owner_sha256,
        observation_sha256=snapshot_fingerprint,
    )
