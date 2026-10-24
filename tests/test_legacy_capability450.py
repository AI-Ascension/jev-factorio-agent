"""Bounded regression coverage for the recognized legacy rocket capability block."""

from copy import deepcopy
from dataclasses import asdict
import json

import pytest
import requests

from jev_factorio.controller import HierarchicalLoop
from jev_factorio.memory import CampaignMemory
from jev_factorio.planning.catalog import Catalog
from jev_factorio.planning.factory import compile_factory
from jev_factorio.provider_health import ProviderBlocked
from jev_factorio.state import GameSnapshot
from jev_factorio.telemetry import make_attempt, utc_now


LEGACY_REASON = (
    "Missing full-game production/research/construction skills and "
    "version-specific native rocket victory telemetry"
)
SESSION = "mock:legacy-capability-450"


def current_catalog():
    # A current-version validated catalog with the rocket recipe chain lets
    # the real factory compiler reach its first paid launch prerequisite.
    def recipe(name, ingredients, category="crafting"):
        return {
            "name": name,
            "category": category,
            "enabled": True,
            "hidden": False,
            "energy": 1,
            "ingredients": [
                {"name": item, "amount": amount, "type": "item"}
                for item, amount in ingredients.items()
            ],
            "products": [{"name": name, "amount": 1, "type": "item", "probability": 1}],
        }

    return Catalog.from_dict({
        "version": "2.0.77",
        "mods": {"base": "2.0.77"},
        "recipes": {
            "rocket-part": recipe("rocket-part", {
                "steel-plate": 100,
                "electronic-circuit": 100,
                "plastic-bar": 100,
                "low-density-structure": 100,
                "rocket-fuel": 100,
            }, "rocket-building"),
            "cargo-landing-pad": recipe("cargo-landing-pad", {"steel-plate": 1}),
        },
        "technologies": {
            "rocket-silo": {"enabled": True, "prerequisites": [], "effects": []},
        },
        "machines": {"rocket-silo": {"categories": {"rocket-building": True}}},
        "hand_categories": {"crafting": True},
    })


def current_snapshot(*, session_id=SESSION, tick=10, valid_launch=True, connector_binding=None):
    factory = {
        "player_connected": True,
        "player_bound": True,
        "crafting_queue": 0,
        "entities": {},
        "receipts": {},
        "produced": {},
    }
    if valid_launch:
        factory["launch_readiness"] = {
            "schema": 1,
            "supported": True,
            "version": "2.0.77",
            "session_id": session_id,
            "tick": tick,
            "actor_unit": 1,
            "surface_index": 1,
            "force_index": 1,
            "fault": False,
            "attempts": {},
            "receipts": {},
            "pad": {},
            "pad_site": {"id": "site:1", "position": {"x": 0, "y": 0}},
            "fish": {},
            "silo": {},
        }
    if connector_binding is not None:
        factory["connector_ownership"] = deepcopy(connector_binding)
    return GameSnapshot(
        session_id=session_id,
        world_kind="mock",
        tick=tick,
        researched=["rocket-silo"],
        inventory={"cargo-landing-pad": 1},
        factory=factory,
    )


class OfflineRocketBackend:
    def __init__(self, *, catalog=True, valid_launch=True, session_id=SESSION,
                 connector_binding=None):
        self._catalog = current_catalog() if catalog is True else catalog
        self.state = current_snapshot(session_id=session_id, valid_launch=valid_launch,
                                      connector_binding=connector_binding)
        self.actions = []
        self.observations = 0

    def enable_factory(self):
        return self._catalog

    def observe(self):
        self.observations += 1
        return deepcopy(self.state)

    def execute(self, action, parameters):
        self.actions.append((action, deepcopy(parameters)))
        return "offline mock command"

    def act(self, action):
        assert action == "idle"
        self.state.tick += 1
        if "launch_readiness" in self.state.factory:
            self.state.factory["launch_readiness"]["tick"] = self.state.tick
        return "offline mock observation tick"


def legacy_checkpoint(path, *, version=1, status="blocked", reason=LEGACY_REASON,
                      session_id=SESSION, target="rocket_launch", patch=None):
    memory = CampaignMemory(
        session_id,
        target,
        version=version,
        active_goal="rocket_launch",
        completed_goals={"stockpile_fuel": 8, "bootstrap_mining": 8},
        history=[{"kind": "legacy_history_witness", "tick": 8, "receipt": "prior:paid"}],
        last_tick=9,
        status=status,
        reason=reason,
    )
    data = asdict(memory)
    if version == 1:
        data.pop("attempt")
        data.pop("attempt_outcomes")
    data.update(patch or {})
    original = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path.write_bytes(original)
    return original


def current_rocket_plan():
    plans, blocker = compile_factory("rocket_launch", current_snapshot(), current_catalog())
    assert blocker == "" and plans
    return plans[0]


@pytest.mark.parametrize("version", [1, 2], ids=["v1-metadata-migration", "supported-v2"])
def test_recognized_legacy_block_reaches_actual_factory_planner_and_records_transition(
    tmp_path, version
):
    checkpoint = tmp_path / f"legacy-v{version}.json"
    connector_binding = {"protocol": 1, "session_id": SESSION, "routes": {}}
    preserved = {"connector_ownership": connector_binding}
    if version == 2:
        plan = current_rocket_plan()
        outcome = make_attempt(
            SESSION, "rocket_launch", plan.to_dict(), 0, {"started_tick": 7})
        outcome.update(outcome="verified", finished_tick=8,
                       finished_at_utc=utc_now(), latency_seconds=None)
        preserved["attempt_outcomes"] = [outcome]
    original = legacy_checkpoint(checkpoint, version=version, patch=preserved)
    backend = OfflineRocketBackend(connector_binding=connector_binding)

    # Read-only restore accepts the historical checkpoint without rewriting it.
    loaded = CampaignMemory.load(checkpoint, SESSION, "rocket_launch")
    assert checkpoint.read_bytes() == original
    assert loaded.version == 2
    assert loaded.status == "blocked" and loaded.reason == LEGACY_REASON

    # This is the current production planner, not an injected compiler sentinel.
    plans, blocker = compile_factory("rocket_launch", backend.observe(), backend._catalog)
    assert blocker == ""
    assert [plan.steps[0].action for plan in plans] == ["factory_launch_pad"]

    loop = HierarchicalLoop(
        backend,
        policy="deterministic",
        target="rocket_launch",
        checkpoint=str(checkpoint),
        resume_controller=True,
        tick_seconds=0,
    )
    record = loop.step()

    assert record["action"] == "factory_launch_pad"
    assert backend.actions == [("factory_launch_pad", {"site": "site:1", "receipt": "launch:pad:10"})]
    assert loop.memory.status == "running"
    assert loop.memory.reason == ""
    assert loop.memory.history[0] == {"kind": "legacy_history_witness", "tick": 8, "receipt": "prior:paid"}
    assert loop.memory.connector_ownership == connector_binding
    if version == 2:
        assert loop.memory.attempt_outcomes == preserved["attempt_outcomes"]
    transitions = [event for event in loop.memory.history
                   if event.get("kind") == "legacy_capability_block_resumed"]
    assert len(transitions) == 1
    assert transitions[0]["tick"] == 10
    persisted = json.loads(checkpoint.read_bytes())
    assert persisted["status"] == "running"
    assert persisted["reason"] == ""
    assert [event["kind"] for event in persisted["history"]].count(
        "legacy_capability_block_resumed") == 1
    assert persisted["connector_ownership"] == connector_binding
    if version == 2:
        assert persisted["attempt_outcomes"] == preserved["attempt_outcomes"]


@pytest.mark.parametrize("case", ["other_reason", "uncertain", "completed", "wrong_goal"])
def test_unrelated_terminal_states_are_not_reactivated(tmp_path, case):
    checkpoint = tmp_path / f"{case}.json"
    kwargs = {"version": 2}
    if case == "other_reason":
        kwargs["reason"] = "unrelated capability or authority hold"
    elif case == "uncertain":
        kwargs["status"] = "uncertain"
    elif case == "completed":
        kwargs["status"] = "completed"
    elif case == "wrong_goal":
        kwargs["patch"] = {"active_goal": "bootstrap_mining"}
    legacy_checkpoint(checkpoint, **kwargs)
    backend = OfflineRocketBackend()
    loop = HierarchicalLoop(backend, policy="deterministic", target="rocket_launch",
                            checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    result = loop.step()

    assert result["action"] == "observe"
    assert backend.actions == []
    assert not any(event.get("kind") == "legacy_capability_block_resumed"
                   for event in loop.memory.history)
    assert loop.memory.status == kwargs.get("status", "blocked")


@pytest.mark.parametrize("catalog,valid_launch", [
    (None, True),
    (object(), True),
    (True, False),
], ids=["catalog-unavailable", "catalog-not-validated", "planner-capability-absent"])
def test_missing_or_unusable_current_capability_stays_blocked(
    tmp_path, catalog, valid_launch
):
    checkpoint = tmp_path / "unsupported.json"
    legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend(catalog=catalog, valid_launch=valid_launch)
    loop = HierarchicalLoop(backend, policy="deterministic", target="rocket_launch",
                            checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    result = loop.step()

    assert backend.actions == []
    assert loop.memory.status == "blocked"
    assert not any(event.get("kind") == "legacy_capability_block_resumed"
                   for event in loop.memory.history)
    if catalog is None or not valid_launch:
        assert result["action"] == "observe"


def test_in_memory_block_cannot_resume_without_a_durable_checkpoint():
    backend = OfflineRocketBackend()
    loop = HierarchicalLoop(backend, policy="deterministic", target="rocket_launch", tick_seconds=0)
    loop.memory = CampaignMemory(
        SESSION, "rocket_launch", active_goal="rocket_launch",
        completed_goals={"stockpile_fuel": 8, "bootstrap_mining": 8},
        last_tick=9, status="blocked", reason=LEGACY_REASON,
    )

    record = loop.step()

    assert record["action"] == "observe"
    assert backend.actions == []
    assert loop.memory.status == "blocked" and loop.memory.reason == LEGACY_REASON


@pytest.mark.parametrize("obligation", ["active_plan", "pending", "reservation"])
def test_legacy_block_with_foreground_or_paid_obligation_is_never_cleared(
    tmp_path, obligation
):
    checkpoint = tmp_path / f"{obligation}.json"
    plan = current_rocket_plan().to_dict()
    patch = {}
    if obligation in {"active_plan", "pending"}:
        patch["active_plan"] = plan
    if obligation == "pending":
        patch["pending"] = {
            "started_tick": 9, "polls": 0, "action": "factory_launch_pad", "dispatch": "prepared"
        }
    if obligation == "reservation":
        patch["reservations"] = {plan["id"]: {"cargo-landing-pad": 1}}
    legacy_checkpoint(checkpoint, version=1, patch=patch)
    before = json.loads(checkpoint.read_bytes())
    backend = OfflineRocketBackend()
    loop = HierarchicalLoop(backend, policy="deterministic", target="rocket_launch",
                            checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    record = loop.step()

    assert record["action"] == "observe"
    assert backend.actions == []
    after = json.loads(checkpoint.read_bytes())
    assert after["status"] == "blocked"
    assert after["reason"] == LEGACY_REASON
    assert after["active_plan"] == before["active_plan"]
    assert after["pending"] == before["pending"]
    assert after["reservations"] == before["reservations"]
    assert not any(event.get("kind") == "legacy_capability_block_resumed"
                   for event in after["history"])


def test_nonempty_background_craft_is_preserved_and_keeps_legacy_state_terminal(tmp_path):
    # Reuse the existing production-shaped background-work fixture to create
    # a loader-valid retained paid job; only its bounded test plan is synthetic.
    from test_background_work import ReceiptBackend, ScenarioLoop

    from jev_factorio.background import BackgroundWorkLoop

    backend = ReceiptBackend()
    checkpoint = tmp_path / "background.json"
    producer = ScenarioLoop(
        backend, policy="deterministic", factory_scheduling="ready-work",
        target="rocket_launch", checkpoint=str(checkpoint), tick_seconds=0)
    producer.memory = producer.memory_type(
        backend.state.session_id, producer.target,
        active_goal="rocket_launch",
        completed_goals={goal: 0 for goal in producer.order[:-1]},
        last_tick=backend.state.tick,
    )
    producer.step()
    saved = json.loads(checkpoint.read_bytes())
    assert saved["background_job"] is not None
    saved["status"], saved["reason"] = "blocked", LEGACY_REASON
    checkpoint.write_text(json.dumps(saved, sort_keys=True, separators=(",", ":")))
    before_job = deepcopy(saved["background_job"])
    before_attempt = deepcopy(saved["background_attempt"])
    before_calls = list(backend.calls)

    resumed = BackgroundWorkLoop(
        backend, policy="deterministic", factory_scheduling="ready-work",
        target="rocket_launch", checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)
    record = resumed.step()

    assert record["action"] == "observe"
    assert resumed.memory.status == "blocked"
    assert resumed.memory.reason == LEGACY_REASON
    assert resumed.memory.background_job == before_job
    assert resumed.memory.background_attempt == before_attempt
    assert backend.calls == before_calls
    assert not any(event.get("kind") == "legacy_capability_block_resumed"
                   for event in resumed.memory.history)


def test_failure_budget_is_not_bypassed_by_legacy_capability_resume(tmp_path):
    checkpoint = tmp_path / "budget.json"
    plan = current_rocket_plan()
    legacy_checkpoint(checkpoint, version=2, patch={"failures": {plan.id: 2}})
    backend = OfflineRocketBackend()
    loop = HierarchicalLoop(backend, policy="deterministic", target="rocket_launch",
                            checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    loop.step()

    assert backend.actions == []
    assert loop.memory.status == "blocked"
    assert loop.memory.reason == "Plan failure budget exhausted"
    assert not any(event.get("kind") == "legacy_capability_block_resumed"
                   for event in loop.memory.history)


def test_runtime_safety_admission_hold_precedes_legacy_resume(tmp_path, monkeypatch):
    import jev_factorio.operational_safety as operational_safety

    checkpoint = tmp_path / "safety-hold.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    loop = HierarchicalLoop(backend, policy="deterministic", target="rocket_launch",
                            checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)
    monkeypatch.setattr(operational_safety, "storage_ready", lambda *args, **kwargs: False)
    assert checkpoint.read_bytes() == original

    record = loop.step()

    assert record["action"] == "observe"
    assert loop._safety.phase == "storage_pressure"
    assert backend.actions == []
    assert loop.memory.status == "blocked" and loop.memory.reason == LEGACY_REASON
    assert any(row.get("kind") == "legacy_history_witness" for row in loop.memory.history)
    assert not any(event.get("kind") == "legacy_capability_block_resumed"
                   for event in loop.memory.history)


def test_unhealthy_provider_circuit_does_not_resume_legacy_block(tmp_path):
    class OfflineUnavailableProvider:
        uses_http_provider = True
        model = "offline-unavailable-model"
        answer_quantum = 0

        def __init__(self):
            self.calls = 0

        def evaluate(self, state, questions):
            self.calls += 1
            raise requests.Timeout("offline provider-health fixture")

    checkpoint = tmp_path / "provider-hold.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    provider = OfflineUnavailableProvider()
    loop = HierarchicalLoop(backend, jev=provider, policy="deterministic",
                            target="rocket_launch", checkpoint=str(checkpoint),
                            resume_controller=True, tick_seconds=0)
    questions = {"ready": {"type": "choice", "criteria": {"yes": "ready"},
                            "instructions": "choose yes"}}

    with pytest.raises(ProviderBlocked):
        loop.jev.evaluate({"offline": True}, questions)
    assert loop.jev.state["phase"] != "healthy"
    provider_state = deepcopy(loop.jev.state)
    assert checkpoint.read_bytes() == original

    record = loop.step()

    assert record["action"] == "observe"
    assert backend.actions == []
    assert provider.calls == 1
    assert loop.memory.status == "blocked" and loop.memory.reason == LEGACY_REASON
    assert any(row.get("kind") == "legacy_history_witness" for row in loop.memory.history)
    assert not any(event.get("kind") == "legacy_capability_block_resumed"
                   for event in loop.memory.history)
    assert loop.jev.state == provider_state


def test_resume_requires_matching_checkpoint_session_before_any_action(tmp_path):
    checkpoint = tmp_path / "wrong-session.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend(session_id="mock:changed-session")
    loop = HierarchicalLoop(backend, policy="deterministic", target="rocket_launch",
                            checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    with pytest.raises(ValueError, match="version, session, or target mismatch"):
        loop.step()

    assert backend.actions == []
    assert checkpoint.read_bytes() == original


def test_resume_requires_matching_checkpoint_target_before_any_action(tmp_path):
    checkpoint = tmp_path / "wrong-target.json"
    original = legacy_checkpoint(checkpoint, version=2, target="iron_smelting")
    backend = OfflineRocketBackend()
    loop = HierarchicalLoop(backend, policy="deterministic", target="rocket_launch",
                            checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    with pytest.raises(ValueError, match="version, session, or target mismatch"):
        loop.step()

    assert backend.actions == []
    assert checkpoint.read_bytes() == original


def test_resume_requires_matching_actor_ledger_before_legacy_migration(tmp_path):
    checkpoint = tmp_path / "changed-actor.json"
    binding = {"protocol": 1, "session_id": SESSION, "routes": {}}
    original = legacy_checkpoint(
        checkpoint, version=2, patch={"connector_ownership": binding})
    changed = {"protocol": 1, "session_id": SESSION, "routes": {"unexpected": {}}}
    backend = OfflineRocketBackend(connector_binding=changed)
    loop = HierarchicalLoop(backend, policy="deterministic", target="rocket_launch",
                            checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    with pytest.raises(ValueError, match="Uncheckpointed connector route appeared"):
        loop.step()

    assert backend.actions == []
    assert checkpoint.read_bytes() == original


def test_transition_save_failure_prevents_selection_dispatch_and_checkpoint_change(
    tmp_path, monkeypatch
):
    checkpoint = tmp_path / "sync-failure.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()

    def fail_save(self, path):
        raise OSError("synthetic transition checkpoint failure")

    monkeypatch.setattr(CampaignMemory, "save", fail_save)
    loop = HierarchicalLoop(backend, policy="deterministic", target="rocket_launch",
                            checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    with pytest.raises(OSError, match="transition checkpoint failure"):
        loop.step()

    assert backend.actions == []
    assert checkpoint.read_bytes() == original
    assert loop.memory.status == "blocked" and loop.memory.reason == LEGACY_REASON
    assert not any(event.get("kind") == "legacy_capability_block_resumed"
                   for event in loop.memory.history)


def test_full_bounded_history_stays_blocked_instead_of_evicting_a_witness(tmp_path):
    checkpoint = tmp_path / "full-history.json"
    history = [{"kind": "retained_witness", "ordinal": index}
               for index in range(64)]
    legacy_checkpoint(checkpoint, version=2, patch={"history": history})
    backend = OfflineRocketBackend()
    loop = HierarchicalLoop(backend, policy="deterministic", target="rocket_launch",
                            checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    result = loop.step()

    assert result["action"] == "observe"
    assert backend.actions == []
    assert loop.memory.status == "blocked" and loop.memory.reason == LEGACY_REASON
    assert loop.memory.history == history


def test_successful_transition_does_not_repeat_after_checkpoint_reload(tmp_path):
    checkpoint = tmp_path / "restart.json"
    legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    first = HierarchicalLoop(backend, policy="deterministic", target="rocket_launch",
                             checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    first.step()
    saved = json.loads(checkpoint.read_bytes())
    assert saved["status"] == "running"
    assert [event["kind"] for event in saved["history"]].count(
        "legacy_capability_block_resumed") == 1
    before = list(backend.actions)

    resumed = HierarchicalLoop(backend, policy="deterministic", target="rocket_launch",
                               checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)
    resumed.step()

    assert [action for action, _ in backend.actions].count("factory_launch_pad") == 1
    assert len(backend.actions) == len(before)
    after = json.loads(checkpoint.read_bytes())
    assert [event["kind"] for event in after["history"]].count(
        "legacy_capability_block_resumed") == 1
