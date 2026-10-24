"""Bounded regression coverage for the recognized legacy rocket capability block."""

from copy import deepcopy
from dataclasses import asdict
import json

import pytest
import requests

from jev_factorio.controller import HierarchicalLoop
from jev_factorio.dashboard import EventWriter, attach
from jev_factorio.memory import CampaignMemory
from jev_factorio.planning.catalog import Catalog
from jev_factorio.planning.factory import compile_factory
from jev_factorio.operational_safety import atomic_json, safety_dir
from jev_factorio.provider_health import ProviderBlocked, ProviderCircuit
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


@pytest.mark.parametrize("version", [1, 2], ids=["v1", "v2"])
def test_dashboard_wrapped_provider_circuit_cooldown_keeps_legacy_block_blocked(
    tmp_path, version
):
    class OfflineUnavailableProvider:
        uses_http_provider = True
        model = "offline-unavailable-model"
        answer_quantum = 0

        def __init__(self):
            self.calls = 0

        def evaluate(self, state, questions):
            self.calls += 1
            raise requests.Timeout("offline provider-health fixture")

    checkpoint = tmp_path / f"dashboard-provider-hold-v{version}.json"
    connector_binding = {"protocol": 1, "session_id": SESSION, "routes": {}}
    preserved = {}
    if version == 2:
        plan = current_rocket_plan()
        outcome = make_attempt(
            SESSION, "rocket_launch", plan.to_dict(), 0, {"started_tick": 7})
        outcome.update(outcome="verified", finished_tick=8,
                       finished_at_utc=utc_now(), latency_seconds=None)
        preserved = {
            "connector_ownership": connector_binding,
            "attempt_outcomes": [outcome],
        }
    original = legacy_checkpoint(checkpoint, version=version, patch=preserved)
    backend = OfflineRocketBackend(
        connector_binding=connector_binding if version == 2 else None)
    provider = OfflineUnavailableProvider()
    loop = HierarchicalLoop(backend, jev=provider, policy="deterministic",
                            target="rocket_launch", checkpoint=str(checkpoint),
                            resume_controller=True, tick_seconds=0)
    circuit = loop.jev
    questions = {"ready": {"type": "choice", "criteria": {"yes": "ready"},
                            "instructions": "choose yes"}}

    with pytest.raises(ProviderBlocked):
        circuit.evaluate({"offline": True}, questions)
    assert circuit.state["phase"] == "cooldown"
    assert checkpoint.read_bytes() == original
    provider_state = deepcopy(circuit.state)

    with EventWriter(tmp_path / f"dashboard-v{version}.jsonl") as writer:
        attach(loop, writer)
        record = loop.step()

    assert record["action"] == "observe"
    assert backend.actions == []
    assert provider.calls == 1
    assert circuit.state == provider_state
    assert loop.memory.status == "blocked" and loop.memory.reason == LEGACY_REASON
    if version == 2:
        assert loop.memory.connector_ownership == connector_binding
        assert loop.memory.attempt_outcomes == preserved["attempt_outcomes"]
    assert any(row.get("kind") == "legacy_history_witness" for row in loop.memory.history)
    assert not any(event.get("kind") == "legacy_capability_block_resumed"
                   for event in loop.memory.history)
    saved = json.loads(checkpoint.read_bytes())
    assert saved["status"] == "blocked" and saved["reason"] == LEGACY_REASON
    assert saved["pending"] is None and saved["attempt"] is None
    assert saved["reservations"] == {}
    if version == 2:
        assert saved["connector_ownership"] == connector_binding
        assert saved["attempt_outcomes"] == preserved["attempt_outcomes"]
    assert any(row.get("kind") == "legacy_history_witness" for row in saved["history"])
    assert not any(row.get("kind") == "legacy_capability_block_resumed"
                   for row in saved["history"])


@pytest.mark.parametrize("version", [1, 2], ids=["v1", "v2"])
def test_dashboard_wrapped_healthy_provider_allows_qualified_legacy_resume(tmp_path, version):
    class OfflineProvider:
        uses_http_provider = True
        model = "offline-healthy-model"
        answer_quantum = 0

        def evaluate(self, state, questions):
            raise AssertionError("deterministic resume must not call the provider")

    checkpoint = tmp_path / f"dashboard-provider-healthy-v{version}.json"
    legacy_checkpoint(checkpoint, version=version)
    backend = OfflineRocketBackend()
    loop = HierarchicalLoop(backend, jev=OfflineProvider(), policy="deterministic",
                            target="rocket_launch", checkpoint=str(checkpoint),
                            resume_controller=True, tick_seconds=0)
    circuit = loop.jev
    assert circuit.state["phase"] == "healthy"

    with EventWriter(tmp_path / f"dashboard-healthy-v{version}.jsonl") as writer:
        attach(loop, writer)
        record = loop.step()

    assert record["action"] == "factory_launch_pad"
    assert record["requested_model"] == "offline-healthy-model"
    assert [action for action, _ in backend.actions] == ["factory_launch_pad"]
    assert circuit.state["phase"] == "healthy"
    saved = json.loads(checkpoint.read_bytes())
    assert saved["status"] == "running"
    assert [event.get("kind") for event in saved["history"]].count(
        "legacy_capability_block_resumed") == 1


@pytest.mark.parametrize(
    "replacement", ["observer-client", "loop-provider", "unregistered-wrapper"]
)
def test_dashboard_provider_binding_rejects_post_attach_client_drift(
    tmp_path, replacement
):
    checkpoint = tmp_path / f"dashboard-provider-drift-{replacement}.json"
    legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    provider = DirectCircuitProvider()
    circuit = ProviderCircuit(provider, _provider_path(checkpoint))
    prior_state = deepcopy(circuit.state)
    prior_sidecar = circuit.path.read_bytes() if circuit.path.exists() else None
    loop = HierarchicalLoop(
        backend, jev=circuit, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    with EventWriter(tmp_path / f"dashboard-provider-drift-{replacement}.jsonl") as writer:
        attach(loop, writer)
        observer = loop.jev
        assert observer is not circuit
        if replacement == "observer-client":
            failing = DirectCircuitProvider(
                requests.Timeout("offline replacement circuit"),
                model="replacement-provider-model")
            replacement_circuit = ProviderCircuit(
                failing, tmp_path / "replacement-provider.json", clock=AdvancingClock())
            questions = {"ready": {"type": "choice", "criteria": {"yes": "ready"},
                                    "instructions": "choose yes"}}
            with pytest.raises(ProviderBlocked):
                replacement_circuit.evaluate({"offline": True}, questions)
            replacement_state = deepcopy(replacement_circuit.state)
            replacement_sidecar = replacement_circuit.path.read_bytes()
            observer.model_client = replacement_circuit
        elif replacement == "loop-provider":
            failing = DirectCircuitProvider(
                requests.Timeout("offline replacement provider"),
                model="replacement-provider-model")
            replacement_circuit = ProviderCircuit(
                failing, tmp_path / "replacement-provider.json", clock=AdvancingClock())
            questions = {"ready": {"type": "choice", "criteria": {"yes": "ready"},
                                    "instructions": "choose yes"}}
            with pytest.raises(ProviderBlocked):
                replacement_circuit.evaluate({"offline": True}, questions)
            replacement_state = deepcopy(replacement_circuit.state)
            replacement_sidecar = replacement_circuit.path.read_bytes()
            loop.jev = replacement_circuit
        else:
            failing = DirectCircuitProvider(
                requests.Timeout("offline wrapped replacement"),
                model="replacement-provider-model")
            replacement_circuit = ProviderCircuit(
                failing, tmp_path / "replacement-provider.json", clock=AdvancingClock())
            questions = {"ready": {"type": "choice", "criteria": {"yes": "ready"},
                                    "instructions": "choose yes"}}
            with pytest.raises(ProviderBlocked):
                replacement_circuit.evaluate({"offline": True}, questions)
            replacement_state = deepcopy(replacement_circuit.state)
            replacement_sidecar = replacement_circuit.path.read_bytes()

            class UnregisteredObserver:
                def __init__(self, model_client):
                    self.model_client = model_client

                def __getattr__(self, name):
                    return getattr(self.model_client, name)

            loop.jev = UnregisteredObserver(replacement_circuit)

        record = loop.step()

    assert record["action"] == "observe"
    assert record["requested_model"] is None
    assert backend.actions == []
    assert provider.calls == 0 and failing.calls == 1
    assert circuit.state == prior_state
    if prior_sidecar is None:
        assert not circuit.path.exists()
    else:
        assert circuit.path.read_bytes() == prior_sidecar
    assert replacement_circuit.state == replacement_state
    assert replacement_circuit.path.read_bytes() == replacement_sidecar
    _assert_legacy_block_is_preserved(checkpoint)


def test_dashboard_registration_is_scoped_to_each_loop_instance(tmp_path):
    loops = []
    for index in range(2):
        checkpoint = tmp_path / f"dashboard-independent-{index}.json"
        legacy_checkpoint(checkpoint, version=2)
        backend = OfflineRocketBackend()
        provider = DirectCircuitProvider(model=f"offline-model-{index}")
        circuit = ProviderCircuit(provider, _provider_path(checkpoint))
        loop = HierarchicalLoop(
            backend, jev=circuit, policy="deterministic", target="rocket_launch",
            checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)
        loops.append((loop, backend, circuit, provider))

    with (EventWriter(tmp_path / "dashboard-independent-0.jsonl") as writer0,
          EventWriter(tmp_path / "dashboard-independent-1.jsonl") as writer1):
        attach(loops[0][0], writer0)
        attach(loops[1][0], writer1)
        records = [loops[0][0].step(), loops[1][0].step()]

    for index, ((loop, backend, circuit, provider), record) in enumerate(zip(loops, records)):
        assert record["action"] == "factory_launch_pad"
        assert record["requested_model"] == f"offline-model-{index}"
        assert [action for action, _ in backend.actions] == ["factory_launch_pad"]
        assert provider.calls == 0
        assert circuit.state["phase"] == "healthy"


@pytest.mark.parametrize("replacement", ["observer-client", "loop-provider"])
def test_dashboard_drift_from_unwrapped_provider_fails_closed_without_recursion(
    tmp_path, replacement
):
    class LocalProvider:
        model = "offline-local-model"
        last_model = "offline-local-model"
        last_usage = None

        def evaluate(self, state, questions):
            raise AssertionError("deterministic rejection must not call the provider")

    checkpoint = tmp_path / f"dashboard-unwrapped-drift-{replacement}.json"
    legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    original_provider = LocalProvider()
    loop = HierarchicalLoop(
        backend, jev=original_provider, policy="deterministic",
        target="rocket_launch", checkpoint=str(checkpoint),
        resume_controller=True, tick_seconds=0)
    cycle = ProviderCircuit(original_provider, tmp_path / "unbound-cycle.json")
    cycle.client = cycle

    with EventWriter(tmp_path / f"dashboard-unwrapped-drift-{replacement}.jsonl") as writer:
        attach(loop, writer)
        if replacement == "observer-client":
            loop.jev.model_client = cycle
        else:
            loop.jev = cycle
        record = loop.step()

    assert record["action"] == "observe"
    assert record["requested_model"] is None
    assert backend.actions == []
    assert cycle.state["phase"] == "healthy"
    assert not cycle.path.exists()
    _assert_legacy_block_is_preserved(checkpoint)


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


def _provider_path(checkpoint):
    return safety_dir(checkpoint) / "provider.json"


def _assert_legacy_block_is_preserved(checkpoint):
    saved = json.loads(checkpoint.read_bytes())
    assert saved["status"] == "blocked"
    assert saved["reason"] == LEGACY_REASON
    assert any(row.get("kind") == "legacy_history_witness" for row in saved["history"])
    assert not any(row.get("kind") == "legacy_capability_block_resumed"
                   for row in saved["history"])


class DirectCircuitProvider:
    uses_http_provider = True
    base_url = "https://offline.invalid/v1"
    model = "legacy-450-offline"
    answer_quantum = 0

    def __init__(self, error=None, *, model=None):
        self.error = error
        self.calls = 0
        self.last_usage = None
        self.last_model = None
        if model is not None:
            self.model = model

    def evaluate(self, state, questions):
        self.calls += 1
        if self.error is not None:
            raise self.error
        raise AssertionError("legacy capability migration must not call the provider")


class AdvancingClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def reach_next_probe(self, circuit):
        self.now = max(self.now, float(circuit.state["next_probe_at"]) + 1)


def _http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(response=response)


@pytest.mark.parametrize("version", [1, 2], ids=["v1", "v2"])
def test_checkpoint_bound_healthy_provider_circuit_is_reused_and_can_resume(
    tmp_path, version
):
    checkpoint = tmp_path / f"direct-circuit-healthy-v{version}.json"
    original = legacy_checkpoint(checkpoint, version=version)
    backend = OfflineRocketBackend()
    provider = DirectCircuitProvider()
    circuit = ProviderCircuit(provider, _provider_path(checkpoint))
    before_state = deepcopy(circuit.state)

    loop = HierarchicalLoop(
        backend, jev=circuit, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    assert loop.jev is circuit
    assert loop._provider_health_circuits == (circuit,)
    assert loop._provider_health_chain_valid is True
    assert loop._legacy_provider_health_ready() is True
    assert checkpoint.read_bytes() == original
    assert not circuit.path.exists()

    with EventWriter(tmp_path / f"direct-circuit-healthy-v{version}.jsonl") as writer:
        attach(loop, writer)
        record = loop.step()

    assert record["action"] == "factory_launch_pad"
    assert record["requested_model"] == provider.model
    assert [action for action, _ in backend.actions] == ["factory_launch_pad"]
    assert provider.calls == 0
    assert loop._provider_health_circuit is circuit
    assert circuit.state == before_state
    assert not circuit.path.exists()
    saved = json.loads(checkpoint.read_bytes())
    assert saved["status"] == "running"
    assert [row["kind"] for row in saved["history"]].count(
        "legacy_capability_block_resumed") == 1


@pytest.mark.parametrize(
    "failure,expected_phase,attempts",
    [
        (requests.Timeout("offline timeout"), "cooldown", 1),
        (_http_error(401), "exhausted", 3),
    ],
    ids=["cooldown-is-retained", "auth-budget-is-retained"],
)
@pytest.mark.parametrize("version", [1, 2], ids=["v1", "v2"])
def test_supplied_circuit_holds_legacy_migration_without_resetting_state(
    tmp_path, failure, expected_phase, attempts, version
):
    checkpoint = tmp_path / f"direct-circuit-{expected_phase}-v{version}.json"
    original = legacy_checkpoint(checkpoint, version=version)
    backend = OfflineRocketBackend()
    provider = DirectCircuitProvider(failure)
    clock = AdvancingClock()
    circuit = ProviderCircuit(provider, _provider_path(checkpoint), clock=clock)
    questions = {"ready": {"type": "choice", "criteria": {"yes": "ready"},
                            "instructions": "choose yes"}}

    for _ in range(attempts):
        if circuit.state["phase"] != "healthy":
            clock.reach_next_probe(circuit)
        with pytest.raises(ProviderBlocked):
            circuit.evaluate({"offline": True}, questions)

    assert circuit.state["phase"] == expected_phase
    assert circuit.state["attempts"] == attempts
    assert circuit.state["in_flight"] is None
    prior_state = deepcopy(circuit.state)
    prior_sidecar = circuit.path.read_bytes()
    assert checkpoint.read_bytes() == original

    loop = HierarchicalLoop(
        backend, jev=circuit, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)
    assert loop.jev is circuit
    assert loop._provider_health_circuits == (circuit,)

    with EventWriter(tmp_path / f"direct-circuit-hold-v{version}.jsonl") as writer:
        attach(loop, writer)
        record = loop.step()

    assert record["action"] == "observe"
    assert backend.actions == []
    assert provider.calls == attempts
    assert loop.memory.status == "blocked" and loop.memory.reason == LEGACY_REASON
    assert circuit.state == prior_state
    assert circuit.path.read_bytes() == prior_sidecar
    _assert_legacy_block_is_preserved(checkpoint)
    assert not any(row.get("kind") == "legacy_capability_block_resumed"
                   for row in loop.memory.history)


def test_nested_typed_provider_circuits_retain_inner_health_and_both_sidecars(tmp_path):
    checkpoint = tmp_path / "nested-provider.json"
    original = legacy_checkpoint(checkpoint, version=2)
    inner_path = tmp_path / "inner-provider.json"
    backend = OfflineRocketBackend()
    provider = DirectCircuitProvider(requests.Timeout("offline inner timeout"))
    clock = AdvancingClock()
    inner = ProviderCircuit(provider, inner_path, clock=clock)
    questions = {"ready": {"type": "choice", "criteria": {"yes": "ready"},
                            "instructions": "choose yes"}}
    with pytest.raises(ProviderBlocked):
        inner.evaluate({"offline": True}, questions)
    outer = ProviderCircuit(inner, _provider_path(checkpoint))
    inner_state = deepcopy(inner.state)
    outer_state = deepcopy(outer.state)
    inner_sidecar = inner_path.read_bytes()
    checkpoint_sidecar_existed = outer.path.exists()

    loop = HierarchicalLoop(
        backend, jev=outer, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    assert loop.jev is outer
    assert loop._provider_health_circuits == (outer, inner)
    assert loop._legacy_provider_health_ready() is False
    assert checkpoint.read_bytes() == original
    record = loop.step()

    assert record["action"] == "observe"
    assert record["requested_model"] == provider.model
    assert backend.actions == []
    assert provider.calls == 1
    assert outer.state == outer_state
    assert inner.state == inner_state
    assert inner_path.read_bytes() == inner_sidecar
    assert outer.path.exists() is checkpoint_sidecar_existed
    _assert_legacy_block_is_preserved(checkpoint)
    assert loop.memory.status == "blocked" and loop.memory.reason == LEGACY_REASON


def test_healthy_nested_typed_provider_chain_remains_eligible(tmp_path):
    checkpoint = tmp_path / "nested-provider-healthy.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    provider = DirectCircuitProvider()
    inner = ProviderCircuit(provider, tmp_path / "inner-healthy-provider.json")
    outer = ProviderCircuit(inner, _provider_path(checkpoint))
    inner_state = deepcopy(inner.state)
    outer_state = deepcopy(outer.state)

    loop = HierarchicalLoop(
        backend, jev=outer, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    assert loop._provider_health_chain_valid is True
    assert loop._legacy_provider_health_ready() is True
    assert checkpoint.read_bytes() == original
    record = loop.step()

    assert record["action"] == "factory_launch_pad"
    assert [action for action, _ in backend.actions] == ["factory_launch_pad"]
    assert provider.calls == 0
    assert outer.state == outer_state
    assert inner.state == inner_state
    assert not outer.path.exists()
    assert not inner.path.exists()
    saved = json.loads(checkpoint.read_bytes())
    assert saved["status"] == "running"
    assert [row["kind"] for row in saved["history"]].count(
        "legacy_capability_block_resumed") == 1


def test_post_construction_same_identity_client_replacement_fails_closed(tmp_path):
    checkpoint = tmp_path / "replaced-terminal-client.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    original_client = DirectCircuitProvider()
    circuit = ProviderCircuit(original_client, _provider_path(checkpoint))
    loop = HierarchicalLoop(
        backend, jev=circuit, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)
    replacement_client = DirectCircuitProvider()
    prior_state = deepcopy(circuit.state)

    # A new client with the same serialized provider identity is still a
    # different authority from the one retained at construction.
    circuit.client = replacement_client

    assert loop._legacy_provider_health_ready() is False
    record = loop.step()

    assert record["action"] == "observe"
    assert record["requested_model"] is None
    assert backend.actions == []
    assert original_client.calls == replacement_client.calls == 0
    assert loop._provider_health_client is original_client
    assert circuit.client is replacement_client
    assert circuit.state == prior_state
    assert not circuit.path.exists()
    _assert_legacy_block_is_preserved(checkpoint)
    assert checkpoint.read_bytes() != original  # ordinary blocked observation is saved


def test_post_construction_unhealthy_nested_circuit_replacement_fails_closed(tmp_path):
    checkpoint = tmp_path / "replaced-nested-client.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    original_client = DirectCircuitProvider()
    outer = ProviderCircuit(original_client, _provider_path(checkpoint))
    loop = HierarchicalLoop(
        backend, jev=outer, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    replacement_client = DirectCircuitProvider(
        requests.Timeout("offline nested replacement timeout"),
        model="replacement-provider-identity",
    )
    replacement_path = tmp_path / "replacement-inner-provider.json"
    replacement_clock = AdvancingClock()
    inner = ProviderCircuit(replacement_client, replacement_path, clock=replacement_clock)
    questions = {"ready": {"type": "choice", "criteria": {"yes": "ready"},
                            "instructions": "choose yes"}}
    with pytest.raises(ProviderBlocked):
        inner.evaluate({"offline": True}, questions)
    inner_state = deepcopy(inner.state)
    inner_sidecar = replacement_path.read_bytes()
    outer_state = deepcopy(outer.state)
    outer.client = inner

    assert loop._legacy_provider_health_ready() is False
    record = loop.step()

    assert record["action"] == "observe"
    assert record["requested_model"] is None
    assert backend.actions == []
    assert original_client.calls == 0
    assert replacement_client.calls == 1  # only the offline cooldown setup call
    assert outer.client is inner
    assert outer.state == outer_state
    assert inner.state == inner_state
    assert replacement_path.read_bytes() == inner_sidecar
    _assert_legacy_block_is_preserved(checkpoint)
    assert checkpoint.read_bytes() != original  # ordinary blocked observation is saved


def test_post_construction_provider_circuit_cycle_fails_closed(tmp_path):
    checkpoint = tmp_path / "replaced-circuit-cycle.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    provider = DirectCircuitProvider()
    circuit = ProviderCircuit(provider, _provider_path(checkpoint))
    loop = HierarchicalLoop(
        backend, jev=circuit, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)
    prior_state = deepcopy(circuit.state)
    circuit.client = circuit

    assert loop._legacy_provider_health_ready() is False
    record = loop.step()

    assert record["action"] == "observe"
    assert record["requested_model"] is None
    assert backend.actions == []
    assert provider.calls == 0
    assert circuit.client is circuit
    assert circuit.state == prior_state
    assert not circuit.path.exists()
    _assert_legacy_block_is_preserved(checkpoint)
    assert checkpoint.read_bytes() != original  # ordinary blocked observation is saved


def test_generic_wrapper_cannot_hide_a_typed_provider_circuit_hold(tmp_path):
    class ProviderWrapper:
        uses_http_provider = True

        def __init__(self, client):
            self.client = client
            # Arbitrary wrapper state is not a provider-health authority.
            self.state = {"phase": "healthy"}

        def __getattr__(self, name):
            return getattr(self.client, name)

        def evaluate(self, state, questions):
            return self.client.evaluate(state, questions)

    checkpoint = tmp_path / "wrapped-provider.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    provider = DirectCircuitProvider(requests.Timeout("offline wrapped timeout"))
    inner_path = tmp_path / "wrapped-inner-provider.json"
    inner = ProviderCircuit(provider, inner_path, clock=AdvancingClock())
    questions = {"ready": {"type": "choice", "criteria": {"yes": "ready"},
                            "instructions": "choose yes"}}
    with pytest.raises(ProviderBlocked):
        inner.evaluate({"offline": True}, questions)
    prior_state = deepcopy(inner.state)
    prior_sidecar = inner_path.read_bytes()
    wrapper = ProviderWrapper(inner)

    loop = HierarchicalLoop(
        backend, jev=wrapper, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    assert isinstance(loop.jev, ProviderCircuit)
    assert loop._provider_health_chain_valid is False
    assert loop._legacy_provider_health_ready() is False
    assert checkpoint.read_bytes() == original
    record = loop.step()

    assert record["action"] == "observe"
    assert backend.actions == []
    assert provider.calls == 1
    assert wrapper.state == {"phase": "healthy"}
    assert inner.state == prior_state
    assert inner_path.read_bytes() == prior_sidecar
    _assert_legacy_block_is_preserved(checkpoint)


def test_healthy_phase_with_live_provider_reservation_does_not_resume(tmp_path):
    class CrashAfterReservation(DirectCircuitProvider):
        def evaluate(self, state, questions):
            self.calls += 1
            raise SystemExit("offline simulated process crash after reservation")

    checkpoint = tmp_path / "provider-reservation.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    provider = CrashAfterReservation()
    circuit = ProviderCircuit(provider, _provider_path(checkpoint), clock=lambda: 1234.0)
    questions = {"ready": {"type": "choice", "criteria": {"yes": "ready"},
                            "instructions": "choose yes"}}

    with pytest.raises(SystemExit, match="simulated process crash"):
        circuit.evaluate({"offline": True}, questions)
    assert circuit.state["phase"] == "healthy"
    assert circuit.state["in_flight"]["healthy_start"] is True
    prior_state = deepcopy(circuit.state)
    prior_sidecar = circuit.path.read_bytes()

    loop = HierarchicalLoop(
        backend, jev=circuit, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    assert loop._legacy_provider_health_ready() is False
    assert checkpoint.read_bytes() == original
    record = loop.step()

    assert record["action"] == "observe"
    assert backend.actions == []
    assert provider.calls == 1
    assert loop.memory.status == "blocked" and loop.memory.reason == LEGACY_REASON
    assert circuit.state == prior_state
    assert circuit.path.read_bytes() == prior_sidecar
    _assert_legacy_block_is_preserved(checkpoint)


def test_circuit_bound_to_another_checkpoint_is_not_silently_rebound(tmp_path):
    checkpoint = tmp_path / "controller.json"
    original = legacy_checkpoint(checkpoint, version=2)
    other_path = tmp_path / "different-run" / "provider.json"
    other_path.parent.mkdir()
    backend = OfflineRocketBackend()
    provider = DirectCircuitProvider()
    circuit = ProviderCircuit(provider, other_path)
    prior_state = deepcopy(circuit.state)

    loop = HierarchicalLoop(
        backend, jev=circuit, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    assert loop.jev is circuit
    assert loop._legacy_provider_health_ready() is False
    assert checkpoint.read_bytes() == original
    record = loop.step()

    assert record["action"] == "observe"
    assert backend.actions == []
    assert provider.calls == 0
    assert loop.memory.status == "blocked" and loop.memory.reason == LEGACY_REASON
    assert circuit.state == prior_state
    assert not other_path.exists()
    _assert_legacy_block_is_preserved(checkpoint)


def test_stale_healthy_circuit_cannot_override_newer_sidecar_hold(tmp_path):
    checkpoint = tmp_path / "stale-sidecar.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    provider_path = _provider_path(checkpoint)
    stale_client = DirectCircuitProvider()
    stale_circuit = ProviderCircuit(stale_client, provider_path)
    failing_client = DirectCircuitProvider(requests.Timeout("offline newer timeout"))
    newer_circuit = ProviderCircuit(failing_client, provider_path, clock=AdvancingClock())
    questions = {"ready": {"type": "choice", "criteria": {"yes": "ready"},
                            "instructions": "choose yes"}}

    with pytest.raises(ProviderBlocked):
        newer_circuit.evaluate({"offline": True}, questions)
    newer_state = deepcopy(newer_circuit.state)
    assert stale_circuit.state["phase"] == "healthy"
    assert newer_state["phase"] == "cooldown"
    assert newer_state["identity"] == stale_circuit.identity
    sidecar_bytes = newer_circuit.path.read_bytes()
    assert checkpoint.read_bytes() == original

    loop = HierarchicalLoop(
        backend, jev=stale_circuit, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    assert loop._legacy_provider_health_ready() is False
    record = loop.step()

    assert record["action"] == "observe"
    assert backend.actions == []
    assert stale_client.calls == 0
    assert failing_client.calls == 1
    assert newer_circuit.path.read_bytes() == sidecar_bytes
    assert stale_circuit.state["phase"] == "healthy"
    _assert_legacy_block_is_preserved(checkpoint)


def test_provider_sidecar_identity_mismatch_fails_closed_without_rebinding(tmp_path):
    checkpoint = tmp_path / "sidecar-identity.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    provider = DirectCircuitProvider()
    circuit = ProviderCircuit(provider, _provider_path(checkpoint))
    bad_sidecar = deepcopy(circuit.state)
    bad_sidecar["identity"] = "0" * 64
    atomic_json(circuit.path, bad_sidecar)
    sidecar_bytes = circuit.path.read_bytes()
    original_memory = deepcopy(circuit.state)

    loop = HierarchicalLoop(
        backend, jev=circuit, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    assert loop._legacy_provider_health_ready() is False
    assert checkpoint.read_bytes() == original
    record = loop.step()

    assert record["action"] == "observe"
    assert backend.actions == []
    assert provider.calls == 0
    assert circuit.state == original_memory
    assert circuit.path.read_bytes() == sidecar_bytes
    _assert_legacy_block_is_preserved(checkpoint)


def test_provider_identity_change_after_circuit_creation_fails_closed(tmp_path):
    checkpoint = tmp_path / "provider-identity.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    provider = DirectCircuitProvider()
    circuit = ProviderCircuit(provider, _provider_path(checkpoint))
    prior_state = deepcopy(circuit.state)
    provider.model = "changed-provider-identity"

    loop = HierarchicalLoop(
        backend, jev=circuit, policy="deterministic", target="rocket_launch",
        checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    assert loop.jev is circuit
    assert loop._legacy_provider_health_ready() is False
    assert checkpoint.read_bytes() == original
    record = loop.step()

    assert record["action"] == "observe"
    assert backend.actions == []
    assert provider.calls == 0
    assert loop.memory.status == "blocked" and loop.memory.reason == LEGACY_REASON
    assert circuit.state == prior_state
    _assert_legacy_block_is_preserved(checkpoint)


@pytest.mark.parametrize("shape", ["cycle", "too-deep"], ids=["cycle", "depth-bound"])
def test_malformed_provider_circuit_chain_fails_closed(tmp_path, shape):
    checkpoint = tmp_path / f"provider-chain-{shape}.json"
    original = legacy_checkpoint(checkpoint, version=2)
    backend = OfflineRocketBackend()
    provider = DirectCircuitProvider()
    if shape == "cycle":
        circuit = ProviderCircuit(provider, _provider_path(checkpoint))
        circuit.client = circuit
    else:
        circuit = provider
        for index in range(9):
            circuit = ProviderCircuit(
                circuit,
                _provider_path(checkpoint) if index == 8 else None,
            )

    with pytest.raises(ValueError, match="cyclic or too deep"):
        HierarchicalLoop(
            backend, jev=circuit, policy="deterministic", target="rocket_launch",
            checkpoint=str(checkpoint), resume_controller=True, tick_seconds=0)

    assert backend.actions == []
    assert checkpoint.read_bytes() == original
