"""After-state presence must control dashboard freshness and fallback."""
from __future__ import annotations

import http.client
import json
import threading
import time

import pytest

from jev_factorio import research_catalog
from jev_factorio.dashboard import DashboardServer, EventWriter, Monitor, SCHEMA, project_record, project_state
from test_research_catalog import DATA_20, raw_catalog


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
    catalog = raw_catalog(DATA_20, researched=["electronics", "automation-science-pack"])
    catalog["state"].update({"tick": 5, "current": "automation", "progress": 0.5})
    research_path = path.with_name(research_catalog.FILENAME)
    research_catalog.write(research_path, catalog)
    monitor = Monitor(path, research=research_path)
    writer = EventWriter(path)
    server = DashboardServer(0, monitor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with writer:
            writer.emit("run_started", 2, controller="hierarchical")
            fresh = state(101, "logistic-science-pack")
            writer.emit("decision_recorded", 7, record=project_record({
                "state": state(100, "automation"),
                "after_state": fresh,
                "action": "observe",
            }))
            monitor.poll()
            first = monitor.snapshot()["view"]
            assert first["state"]["mission"]["research"]["name"] == "logistic-science-pack"
            assert isinstance(first["state_observed_time"], (int, float))
            assert first["research"]["source"] == "telemetry"
            assert first["research"]["current"]["name"] == "logistic-science-pack"
            first_milestone_tick = monitor.research_seen["automation"]

            no_research = state(102, "automation")
            no_research["researched"] = []
            no_research["factory"].update(research=None, research_progress=None)
            writer.emit("decision_recorded", 7, record=project_record({
                "after_state": no_research, "action": "observe",
            }))
            monitor.poll()
            empty_research_view = _http_snapshot(server)["view"]
            assert empty_research_view["state_observed_time"] is not None
            assert empty_research_view["research"]["status"] == "ok"
            assert empty_research_view["research"]["source"] == "telemetry"
            assert empty_research_view["research"]["current"] is None

            incomplete_states = [
                {}, None, [], "malformed",
                {"tick": 102, "session_id": "session-481", "world_kind": "fle"},
                {"tick": 102, "researched": "automation"},
                {"tick": 102, "researched": ["automation", 7]},
                {"tick": 102, "researched": ["rocket-silo", 7]},
                {"researched": []},
            ]
            previous_time = empty_research_view["last_event_time"]
            for incomplete in incomplete_states:
                writer.emit("decision_recorded", 7, record=project_record({
                    "state": fresh,
                    "after_state": incomplete,
                    "action": "observe",
                    "decision": {"Authorization": "Bearer dashboard-481-private-token"},
                }))
                monitor.poll()
                snapshot = _http_snapshot(server)
                view = snapshot["view"]
                assert view["last_event_time"] > previous_time
                previous_time = view["last_event_time"]
                assert snapshot["source"]["invalid"] == 0
                assert view["state_observed_time"] is None
                assert view["research"]["status"] == "observation incomplete"
                assert view["research"]["source"] == "telemetry-history"
                assert view["research"]["current"] is None
                assert view["research"]["version"] == "2.0.77"
                assert view["research"]["tiers"]
                rocket = next(row for row in view["milestones"] if row["key"] == "rocket-silo")
                assert rocket["state"] != "done"
                assert "dashboard-481-private-token" not in json.dumps(snapshot)
                if isinstance(incomplete, dict) and incomplete:
                    assert view["state"].get("tick") == incomplete.get("tick")
                    if "researched" in incomplete:
                        assert view["state"].get("researched") == incomplete["researched"]
                else:
                    assert view["state"] == {}
            # The sidecar remains available for static tree metadata, but its
            # older current/progress cannot fill the explicit observation gap.
            assert view["research"]["source"] != "catalog"
            assert monitor.research_seen["automation"] == first_milestone_tick

        # A new controller run clears the old state projection, but does not
        # turn the old catalog's current topic into a fresh observation.
        with EventWriter(path) as run_reset_writer:
            run_reset_writer.emit("run_started", 2, controller="hierarchical")
            monitor.poll()
            after_run_reset = _http_snapshot(server)["view"]
            assert after_run_reset.get("state_observed_time") is None
            assert after_run_reset["research"]["status"] == "observation incomplete"
            assert after_run_reset["research"]["source"] == "unknown"
            assert after_run_reset["research"]["current"] is None

        # A replacement/truncated event file also cannot revive that sidecar.
        path.unlink()
        with EventWriter(path) as tail_reset_writer:
            tail_reset_writer.emit("run_started", 2, controller="hierarchical")
            monitor.poll()
            after_tail_reset = _http_snapshot(server)["view"]
            assert after_tail_reset.get("state_observed_time") is None
            assert after_tail_reset["research"]["status"] == "observation incomplete"
            assert after_tail_reset["research"]["source"] == "unknown"
            assert after_tail_reset["research"]["current"] is None

            recovered = state(103, "automation")
            tail_reset_writer.emit("decision_recorded", 7, record=project_record({
                "after_state": recovered, "action": "observe",
            }))
            monitor.poll()
            recovered_view = _http_snapshot(server)["view"]
            assert recovered_view["state_observed_time"] is not None
            assert recovered_view["research"]["status"] == "ok"
            assert recovered_view["research"]["source"] == "telemetry"
            assert recovered_view["research"]["current"]["name"] == "automation"
            # A fresh run may reseed historical telemetry milestones.
            assert monitor.research_seen["automation"] is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize(
    "observed_state",
    [
        None,
        [],
        "malformed",
        {},
        {"tick": 102, "session_id": "session-481", "world_kind": "fle"},
        {"tick": 102, "session_id": "session-481", "world_kind": "fle",
         "researched": ["automation", 7]},
    ],
    ids=["null", "array", "string", "empty-object", "missing-researched", "malformed-researched"],
)
def test_incomplete_raw_observation_clears_freshness_mission_and_catalog_current(tmp_path, observed_state):
    path = tmp_path / "dashboard.jsonl"
    catalog = raw_catalog(DATA_20, researched=["electronics", "automation-science-pack"])
    catalog["state"].update({"tick": 5, "current": "automation", "progress": 0.5})
    research_path = path.with_name(research_catalog.FILENAME)
    research_catalog.write(research_path, catalog)
    monitor = Monitor(path, research=research_path)
    writer = EventWriter(path)
    server = DashboardServer(0, monitor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with writer:
            writer.emit("run_started", 2, controller="hierarchical")
            complete = state(101, "logistic-science-pack")
            writer.emit("decision_recorded", 7, record=project_record({
                "after_state": complete, "action": "observe",
            }))
            monitor.poll()
            before = _http_snapshot(server)["view"]
            assert before["research"]["current"]["name"] == "logistic-science-pack"
            assert "mission_record" in before
            assert before["state_observed_time"] is not None

            writer.emit("observation", 2, state=observed_state)
            monitor.poll()
            snapshot = _http_snapshot(server)
            view = snapshot["view"]
            assert view["last_event_time"] > before["last_event_time"]
            assert view["state_observed_time"] is None
            assert monitor._research_observation_incomplete is True
            assert view["research"]["status"] == "observation incomplete"
            assert view["research"]["source"] != "catalog"
            assert view["research"]["current"] is None
            assert "mission_record" not in view
            assert "recorded_action" not in view
            assert snapshot["source"]["invalid"] == 0

            recovered = state(103, "automation")
            recovered["researched"] = ["automation"]
            writer.emit("observation", 2, state=project_state(recovered))
            monitor.poll()
            recovered_view = _http_snapshot(server)["view"]
            assert monitor._research_observation_incomplete is False
            assert recovered_view["state_observed_time"] is not None
            assert recovered_view["research"]["status"] == "ok"
            assert recovered_view["research"]["source"] == "telemetry"
            assert recovered_view["research"]["current"]["name"] == "automation"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_raw_producer_mission_projection_keeps_launch_fresh_but_not_catalog_research(tmp_path):
    from test_dashboard_mission import mission_state

    path = tmp_path / "dashboard.jsonl"
    catalog = raw_catalog(DATA_20, researched=["electronics", "automation-science-pack"])
    catalog["state"].update({"tick": 5, "current": "automation", "progress": 0.5})
    research_path = path.with_name(research_catalog.FILENAME)
    research_catalog.write(research_path, catalog)
    monitor = Monitor(path, research=research_path)
    writer = EventWriter(path)
    server = DashboardServer(0, monitor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with writer:
            writer.emit("run_started", 2, controller="hierarchical")
            complete = state(101, "logistic-science-pack")
            writer.emit("decision_recorded", 7, record=project_record({
                "after_state": complete, "action": "observe",
            }))
            monitor.poll()
            assert "mission_record" in monitor.view

            # attach() emits this bounded mission projection around for_jev();
            # this producer shape has valid launch telemetry but no research list.
            producer_state = project_state(mission_state())
            assert "mission" in producer_state and "researched" not in producer_state
            writer.emit("observation", 2, state=producer_state)
            monitor.poll()

            view = _http_snapshot(server)["view"]
            assert monitor._research_observation_incomplete is True
            assert view["state_observed_time"] is not None
            assert view["state"]["mission"]["launch"]["headline"] == "Launch prerequisites observed"
            assert view["research"]["status"] == "observation incomplete"
            assert view["research"]["source"] != "catalog"
            assert view["research"]["current"] is None
            assert "mission_record" not in view
            assert "recorded_action" not in view
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


@pytest.mark.parametrize("kind", ["decision_recorded", "observation"])
@pytest.mark.parametrize(
    "bad_researched",
    [
        ["rocket-silo", 7],
        ["rocket-silo", "bad name"],
        ["rocket-silo"] * (research_catalog.MAX_TECHNOLOGIES + 1),
    ],
    ids=["mixed-type", "invalid-name", "over-limit"],
)
def test_incoherent_researched_list_never_creates_new_milestone_history(kind, bad_researched):
    monitor = Monitor("unused-events")
    run_id = f"malformed-history-{kind}"
    valid_before = {"tick": 101, "session_id": "session-481", "world_kind": "fle",
                    "researched": ["automation"]}

    def send(seq, event_kind, value):
        if event_kind == "decision_recorded":
            data = {"record": value}
        else:
            data = {"state": value}
        monitor.accept({
            "schema": SCHEMA,
            "run_id": run_id,
            "seq": seq,
            "stage": 7,
            "time": 2000 + seq,
            "kind": event_kind,
            "data": data,
        })

    before_record = project_record({"after_state": valid_before, "action": "observe"})
    send(1, "decision_recorded", before_record)
    assert monitor.research_seen == {"automation": None}

    invalid_state = {"tick": 102, "session_id": "session-481", "world_kind": "fle",
                     "researched": bad_researched}
    invalid_record = project_record({"state": valid_before, "after_state": invalid_state, "action": "observe"})
    send(2, kind, invalid_record if kind == "decision_recorded" else invalid_state)

    assert monitor._research_observation_incomplete is True
    assert monitor.view["state_observed_time"] is None
    assert monitor.research_seen == {"automation": None}
    rocket = next(row for row in monitor.snapshot()["view"]["milestones"] if row["key"] == "rocket-silo")
    assert rocket["state"] != "done"

    recovered = {"tick": 103, "session_id": "session-481", "world_kind": "fle",
                 "researched": ["automation", "rocket-silo"]}
    recovered_record = project_record({"after_state": recovered, "action": "observe"})
    send(3, kind, recovered_record if kind == "decision_recorded" else recovered)

    assert monitor._research_observation_incomplete is False
    assert monitor.view["state_observed_time"] == 2003
    assert monitor.research_seen == {"automation": None, "rocket-silo": 103}
    rocket = next(row for row in monitor.snapshot()["view"]["milestones"] if row["key"] == "rocket-silo")
    assert rocket["state"] == "done"
    assert rocket["tick"] == 103


def test_raw_launch_projection_is_bound_to_current_observation_identity(tmp_path):
    """A prior launch display cannot gain freshness from a new outer record."""
    from test_dashboard_mission import mission_state

    path = tmp_path / "dashboard.jsonl"
    catalog = raw_catalog(DATA_20, researched=["electronics", "automation-science-pack"])
    catalog["state"].update({"tick": 5, "current": "automation", "progress": 0.5})
    research_path = path.with_name(research_catalog.FILENAME)
    research_catalog.write(research_path, catalog)
    monitor = Monitor(path, research=research_path)
    writer = EventWriter(path)
    server = DashboardServer(0, monitor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with writer:
            writer.emit("run_started", 2, controller="hierarchical")
            producer_state = project_state(mission_state())
            assert "researched" not in producer_state
            writer.emit("observation", 2, state=producer_state)
            monitor.poll()
            valid = _http_snapshot(server)["view"]
            assert valid["state_observed_time"] is not None
            assert valid["state"]["mission"]["launch"]["evidence_valid"] is True
            assert valid["research"]["status"] == "observation incomplete"
            assert valid["research"]["source"] != "catalog"
            assert valid["research"]["current"] is None

            cases = []
            for name in ("missing_outer_tick", "changed_outer_tick", "missing_outer_session",
                         "changed_outer_session", "missing_launch", "empty_launch",
                         "changed_launch_tick", "changed_launch_session", "bad_launch_schema",
                         "bad_launch_gates", "bad_gate_state", "bad_evidence_flag",
                         "bad_mission_schema", "bad_research_shape", "huge_research_progress",
                         "bad_automation_family", "oversized_outer_session"):
                bad = project_state(mission_state())
                if name == "missing_outer_tick":
                    bad.pop("tick")
                elif name == "changed_outer_tick":
                    bad["tick"] += 1
                elif name == "missing_outer_session":
                    bad.pop("session_id")
                elif name == "changed_outer_session":
                    bad["session_id"] = "other-session"
                elif name == "missing_launch":
                    bad["mission"].pop("launch")
                elif name == "empty_launch":
                    bad["mission"]["launch"] = {}
                elif name == "changed_launch_tick":
                    bad["mission"]["launch"]["tick"] += 1
                elif name == "changed_launch_session":
                    bad["mission"]["launch"]["session_id"] = "other-session"
                elif name == "bad_launch_schema":
                    bad["mission"]["launch"]["schema"] = True
                elif name == "bad_launch_gates":
                    bad["mission"]["launch"]["gates"] = []
                elif name == "bad_gate_state":
                    bad["mission"]["launch"]["gates"][0]["state"] = []
                elif name == "bad_evidence_flag":
                    bad["mission"]["launch"]["evidence_valid"] = "true"
                elif name == "bad_mission_schema":
                    bad["mission"]["schema"] = True
                elif name == "bad_research_shape":
                    bad["mission"]["research"] = []
                elif name == "huge_research_progress":
                    bad["mission"]["research"]["progress"] = 10 ** 1000
                elif name == "bad_automation_family":
                    bad["mission"]["automation"] = [{"family": []}]
                elif name == "oversized_outer_session":
                    bad["session_id"] = "s" * 161

                writer.emit("observation", 2, state=bad)
                monitor.poll()
                view = _http_snapshot(server)["view"]
                cases.append({
                    "case": name,
                    "fresh": view["state_observed_time"] is not None,
                    "mission_visible": "mission" in view["state"],
                    "research_status": view["research"]["status"],
                    "research_not_catalog": view["research"]["source"] != "catalog",
                    "research_current": view["research"]["current"],
                })

            assert cases == [
                {"case": row["case"], "fresh": False, "mission_visible": False,
                 "research_status": "observation incomplete", "research_not_catalog": True,
                 "research_current": None}
                for row in cases
            ], cases

            for changed_field in ("tick", "session_id"):
                with_research = project_state(mission_state())
                with_research["researched"] = ["automation"]
                if changed_field == "tick":
                    with_research["tick"] += 1
                else:
                    with_research["session_id"] = "other-session"
                writer.emit("observation", 2, state=with_research)
                monitor.poll()
                view = _http_snapshot(server)["view"]
                # The research list independently supplies observation freshness,
                # but it cannot validate the copied launch projection.
                assert view["state_observed_time"] is not None
                assert "mission" not in view["state"]
                assert view["research"]["source"] != "catalog"
                assert view["research"]["current"] is None

            writer.emit("observation", 2, state=project_state(mission_state()))
            monitor.poll()
            recovered = _http_snapshot(server)["view"]
            assert recovered["state_observed_time"] is not None
            assert recovered["state"]["mission"]["launch"]["evidence_valid"] is True
            assert recovered["research"]["status"] == "observation incomplete"
            assert recovered["research"]["current"] is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
