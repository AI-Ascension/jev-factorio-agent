"""After-state presence must control dashboard freshness and fallback."""
from __future__ import annotations

import http.client
import json
import threading
import time

import pytest

from jev_factorio.dashboard import DashboardServer, EventWriter, Monitor, SCHEMA, project_record


def state(tick: int, research: str) -> dict:
    return {
        "tick": tick,
        "session_id": "session-481",
        "world_kind": "fle",
        "researched": ["automation"],
        "factory": {"research": research, "research_progress": 0.25},
    }


def _http_snapshot(server: DashboardServer) -> dict:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    try:
        connection.request("GET", "/api/snapshot")
        response = connection.getresponse()
        assert response.status == 200
        return json.loads(response.read())
    finally:
        connection.close()


def test_project_record_uses_after_state_when_present_and_legacy_state_only_when_absent():
    old = state(100, "old-research")
    legacy = project_record({
        "state": {"tick": 101, "session_id": "session-481"},
        "decision": {"state": {"facts": {"factory": {"research": "legacy-research"}}}},
    })
    assert legacy["state"]["mission"]["research"]["name"] == "legacy-research"

    current = project_record({"state": old, "after_state": state(102, "current-research")})
    assert current["state"]["tick"] == 102
    assert current["state"]["mission"]["research"]["name"] == "current-research"


@pytest.mark.parametrize("after_state", [{}, None, [], "malformed"])
def test_explicit_empty_or_malformed_after_state_clears_instead_of_falling_back(after_state):
    projected = project_record({
        "state": state(100, "stale-research"),
        "after_state": after_state,
        "decision": {"state": {"facts": {"factory": {"research": "stale-decision-research"}}}},
    })
    assert projected["state"] == {}


def test_partial_after_state_is_projected_without_legacy_decision_facts():
    projected = project_record({
        "state": state(100, "stale-research"),
        "after_state": {"tick": 103, "session_id": "session-481", "world_kind": "fle"},
        "decision": {"state": {"facts": {"factory": {"research": "stale-decision-research"}}}},
    })
    assert projected["state"]["tick"] == 103
    assert projected["state"]["mission"]["research"]["name"] is None
    assert "researched" not in projected["state"]


def test_newer_empty_after_state_clears_served_snapshot_and_does_not_leak_secrets(tmp_path):
    path = tmp_path / "dashboard.jsonl"
    monitor = Monitor(path)
    writer = EventWriter(path)
    server = DashboardServer(0, monitor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with writer:
            writer.emit("run_started", 2, controller="hierarchical")
            writer.emit("decision_recorded", 7, record=project_record({
                "state": state(100, "stale-research"),
                "after_state": state(101, "current-research"),
                "action": "observe",
            }))
            monitor.poll()
            first = monitor.snapshot()["view"]
            assert first["state"]["mission"]["research"]["name"] == "current-research"
            assert isinstance(first["state_observed_time"], (int, float))
            first_milestone_tick = monitor.research_seen["automation"]

            writer.emit("decision_recorded", 7, record=project_record({
                "state": state(101, "stale-research"),
                "after_state": {},
                "action": "observe",
                "decision": {"Authorization": "Bearer dashboard-481-private-token"},
            }))
            monitor.poll()
            snapshot = _http_snapshot(server)
            view = snapshot["view"]
            assert view["last_event_time"] > first["last_event_time"]
            assert view["state"] == {}
            assert view["state_observed_time"] is None
            assert "stale-research" not in json.dumps(snapshot)
            assert "current-research" not in json.dumps(snapshot)
            assert "dashboard-481-private-token" not in json.dumps(snapshot)
            # Earlier research remains a historical milestone at its original tick.
            assert monitor.research_seen["automation"] == first_milestone_tick
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_legacy_reader_accepts_explicit_incomplete_state_to_clear_prior_record(tmp_path):
    path = tmp_path / "legacy.jsonl"
    first = {
        "session_id": "legacy-481",
        "state": state(100, "old-legacy-research"),
        "action": "observe",
        "recorded_at_utc": "2026-10-06T10:00:00+00:00",
    }
    path.write_text(json.dumps(first) + "\n", encoding="utf-8")
    monitor = Monitor(path, legacy=True)
    monitor.poll()
    assert monitor.view["state"]["mission"]["research"]["name"] == "old-legacy-research"
    assert monitor.view["state_observed_time"] is not None

    second = {
        "session_id": "legacy-481",
        "state": state(100, "old-legacy-research"),
        "after_state": state(101, "current-legacy-research"),
        "action": "observe",
        "recorded_at_utc": "2026-10-06T10:00:30+00:00",
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(second) + "\n")
    monitor.poll()
    assert monitor.view["state"]["tick"] == 101
    assert monitor.view["state"]["mission"]["research"]["name"] == "current-legacy-research"

    third = {
        "session_id": "legacy-481",
        "after_state": None,
        "action": "observe",
        "recorded_at_utc": "2026-10-06T10:01:00+00:00",
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(third) + "\n")
    monitor.poll()

    assert monitor.rejected == 0
    assert monitor.view["state"] == {}
    assert monitor.view["state_observed_time"] is None
    assert monitor.view["last_event_time"] > 0


def test_future_incomplete_event_cannot_make_old_observation_fresh():
    monitor = Monitor("unused-events")
    now = time.time()
    monitor.accept({
        "schema": SCHEMA, "run_id": "future-481", "seq": 1, "stage": 7,
        "time": now - 3600, "kind": "decision_recorded",
        "data": {"record": project_record({"after_state": state(100, "stale-research")})},
    })
    assert monitor.view["state_observed_time"] == now - 3600

    future = now + 3600
    monitor.accept({
        "schema": SCHEMA, "run_id": "future-481", "seq": 2, "stage": 7,
        "time": future, "kind": "decision_recorded",
        "data": {"record": project_record({"state": state(100, "stale-research"), "after_state": []})},
    })
    assert monitor.view["last_event_time"] == future
    assert monitor.view["state"] == {}
    assert monitor.view["state_observed_time"] is None
