"""Typed completion gates for existing, completed campaign owners.

These unit controls cover the checkpoint and family boundaries. The paired
native adapter tests exercise the public query with source-produced Lua rows.
They remain offline modeled controls, not saved-world acceptance.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from jev_factorio.owner_completion import (
    _checkpoint_document,
    _goal_completion,
    _required_completion_modules,
    _unresolved_feature_owners,
    _validate_completion_profile,
    _validate_current_owners,
    _validate_native_identity,
)
from jev_factorio.backends.native_current_attachment import DIRECT_MODULE_PROFILES
from jev_factorio.state import GameSnapshot


def _owner(**values):
    defaults = {
        "session_id": "session-a",
        "last_tick": 20,
        "connector_ownership": None,
        "background_job": None,
        "background_attempt": None,
        "background_step": None,
        "capital_investment": None,
        "solid_funding": None,
        "coal_funding": None,
        "successor_projects": {},
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def _native_identity(**changes):
    identity = {
        "tick": 20,
        "speed": 1,
        "marked": True,
        "runtime": True,
        "session_id": "session-a",
        "connected": 1,
    }
    identity.update(changes)
    return identity


def test_native_identity_requires_exact_current_marked_connected_session():
    assert _validate_native_identity(_native_identity(), "session-a", 20) == 20
    adverse = [
        {},
        _native_identity(unexpected=True),
        _native_identity(session_id="other"),
        _native_identity(tick=19),
        _native_identity(speed=0),
        _native_identity(marked=False),
        _native_identity(runtime=False),
        _native_identity(connected=0),
        _native_identity(connected=True),
    ]
    for identity in adverse:
        with pytest.raises(ValueError):
            _validate_native_identity(identity, "session-a", 20)


def test_completed_target_requires_one_matching_controller_event():
    owner = _owner(
        completed_goals={"rocket_launch": 20},
        history=[{"kind": "goal_completed", "goal": "rocket_launch", "tick": 20}],
    )
    _goal_completion(owner, "rocket_launch")
    for history in ([], [*owner.history, dict(owner.history[0])],
                    [{"kind": "goal_completed", "goal": "rocket_launch", "tick": 19}]):
        owner.history = history
        with pytest.raises(ValueError):
            _goal_completion(owner, "rocket_launch")


def test_terminal_receipts_are_retained_but_unfinished_work_markers_block():
    owner = _owner(
        successor_projects={"growth:iron-plate": {"status": "qualified"}},
        connector_ownership={"routes": {"receipt-1": {"state": "complete", "pending": None}}},
    )
    _unresolved_feature_owners(owner)

    for name in ("background_job", "background_attempt", "background_step",
                 "capital_investment", "solid_funding", "coal_funding"):
        adverse = _owner(**{name: {"retained": True}})
        with pytest.raises(ValueError, match="retains an active background or funding owner"):
            _unresolved_feature_owners(adverse)

    for project in ({"status": "active"}, {"status": "paused"}):
        adverse = _owner(successor_projects={"growth:iron-plate": project})
        with pytest.raises(ValueError, match="unfinished successor"):
            _unresolved_feature_owners(adverse)

    for route in ({"state": "building", "pending": None},
                  {"state": "complete", "pending": {"phase": "prepared"}}):
        adverse = _owner(connector_ownership={"routes": {"receipt-1": route}})
        with pytest.raises(ValueError, match="unfinished connector"):
            _unresolved_feature_owners(adverse)


@pytest.mark.parametrize(
    "family,fields,expected",
    [
        ("base action and reservation", {}, set()),
        ("strict two-stage and persistent-selection ledger", {}, set()),
        ("async decision archive/WAL/provider health", {}, set()),
        ("background work", {"background_schema": 2}, set()),
        ("connector route ownership", {"connector_ownership": {"routes": {}}},
         {"connector_ownership"}),
        ("capital investment", {"capital_investment": None}, set()),
        ("output buffers", {"output_buffers_schema": 1}, {"output_buffers"}),
        ("input routes", {"input_routes_schema": 1}, {"input_routes", "production_sites"}),
        ("mining outposts", {"input_routes_schema": 1, "outposts_schema": 1},
         {"input_routes", "production_sites", "mining_outposts"}),
        ("successor projects", {"successor_schema": 1},
         {"successors", "input_routes", "output_buffers", "production_sites", "craft_jobs"}),
        ("solid routes and funding", {"solid_routes_schema": 1}, {"solid_routes"}),
        ("coal supply and funding", {"solid_routes_schema": 1, "coal_supply_schema": 1},
         {"solid_routes", "coal_supply"}),
        ("planner fault and compatible-source recovery", {}, set()),
        ("ordinary receipts and goal history", {}, set()),
    ],
)
def test_all_fourteen_durable_marker_families_map_to_required_sources(family, fields, expected):
    # Family labels are the complete design matrix; only native producer
    # extensions require a source module. Other durable markers are checked by
    # the composed checkpoint/outer-ledger validators before querying native.
    assert family
    owner = _owner(**fields)
    assert _required_completion_modules(owner) == expected


def _attachment(profile):
    enabled = DIRECT_MODULE_PROFILES[profile]
    names = set().union(*DIRECT_MODULE_PROFILES.values())
    return {
        "modules": {name: name in enabled for name in names},
        "connector_observer_bridge_qualified": False,
        "connector_snapshot_qualified": False,
        "native_installation": {"profile": False},
    }


@pytest.mark.parametrize(
    "fields,profile,accepted",
    [
        ({}, "default", True),
        ({"output_buffers_schema": 1}, "buffers_without_background", True),
        ({"input_routes_schema": 1}, "current_full", True),
        ({"outposts_schema": 1, "input_routes_schema": 1}, "current_full", True),
        ({"successor_schema": 1}, "current_successors", True),
        ({"successor_schema": 1}, "current_full", False),
        ({"coal_supply_schema": 1}, "current_full", False),
    ],
)
def test_completion_requires_exact_source_reviewed_composition(fields, profile, accepted):
    owner = _owner(**fields)
    if "connector_ownership" not in fields:
        owner.connector_ownership = None
    if accepted:
        _validate_completion_profile(owner, _attachment(profile))
    else:
        with pytest.raises(ValueError):
            _validate_completion_profile(owner, _attachment(profile))


def test_current_owner_gate_requires_current_typed_snapshot():
    owner = _owner()
    current = GameSnapshot(tick=20, session_id="session-a", world_kind="fle", factory={})
    _validate_current_owners(owner, current, {})
    for stale in (
        GameSnapshot(tick=20, session_id="other", world_kind="fle", factory={}),
        GameSnapshot(tick=19, session_id="session-a", world_kind="fle", factory={}),
        SimpleNamespace(tick=20, session_id="session-a", factory={}),
    ):
        with pytest.raises(ValueError):
            _validate_current_owners(owner, stale, {})


def test_checkpoint_parser_rejects_duplicate_keys_nonobjects_and_nonfinite_values():
    assert _checkpoint_document(b'{"schema":1}') == {"schema": 1}
    for raw in (b'{"schema":1,"schema":2}', b'[]', b'{"tick":NaN}', b'\xff'):
        with pytest.raises(ValueError):
            _checkpoint_document(raw)
