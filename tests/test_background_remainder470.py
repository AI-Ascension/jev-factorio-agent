"""Production-path regressions for authenticated gather remainder retries."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict

import pytest

from test_factory import catalog as factory_catalog, machine, snapshot as factory_snapshot
from test_planner_consistency import production_fixture
from jev_factorio.background import BackgroundMemory, BackgroundWorkLoop
from jev_factorio.craft_jobs import CraftJob
from jev_factorio.controller import HierarchicalLoop
from jev_factorio.planning.factory import FactoryPlanner
from jev_factorio.skills import Plan, Step
from jev_factorio.telemetry import make_attempt


class OfflineBackend:
    """Real observation shape with all game mutation boundaries disabled."""
    craft_jobs_supported = True

    def __init__(self, state, catalog):
        self.state = state
        self.catalog = catalog
        self.observations = 0
        self.actions = []

    def enable_factory(self):
        return self.catalog

    def observe(self):
        self.observations += 1
        return deepcopy(self.state)

    def execute(self, action, parameters):
        self.actions.append((action, deepcopy(parameters)))
        raise AssertionError("Offline regression must not execute a game action")

    def act(self, action):
        self.actions.append((action,))
        raise AssertionError("Offline regression must not advance a game")


def _active_job_loop(tmp_path):
    state, catalog, _row, job = production_fixture(available=0)
    job.session_id = state.session_id
    job.baseline = {"science": 0}
    job.finished = 0
    job.last_progress_tick = job.started_tick
    actor = {**job.actor, "session_id": state.session_id}
    state.factory.update(
        craft_jobs_protocol=1, craft_job_actor=actor, player_connected=True,
        player_bound=True, crafting_queue=1,
        craft_job={
            **actor, "id": job.parameters["receipt"], "recipe": job.parameters["recipe"],
            "requested": job.parameters["batches"], "accepted": job.parameters["batches"],
            "finished": 0, "started_tick": job.started_tick,
            "last_progress_tick": job.last_progress_tick,
            "inputs": deepcopy(job.inputs), "outputs": deepcopy(job.outputs),
            "baseline": deepcopy(job.baseline), "status": "running",
            "queue_valid": True, "paid": True,
        },
    )
    # This fixture has no output-buffer protocol; keep the genuine gather gate.
    state.factory.pop("output_buffers", None)
    assert CraftJob.from_dict(job.to_dict()).observe(state) is False

    backend = OfflineBackend(state, catalog)
    checkpoint = tmp_path / "campaign.json"
    loop = BackgroundWorkLoop(
        backend, policy="deterministic", factory_scheduling="ready-work",
        target="rocket_launch", checkpoint=str(checkpoint), tick_seconds=0,
    )
    memory = BackgroundMemory(
        state.session_id, "rocket_launch", active_goal="rocket_launch", last_tick=state.tick,
    )
    memory.background_schema = 3
    memory.background_job = job.to_dict()
    craft_step = Step(
        "factory_craft_job", "craft_job_complete", "science", 10,
        costs=deepcopy(job.inputs), timeout_ticks=job.deadline_tick - job.started_tick,
        parameters=deepcopy(job.parameters),
    )
    craft_plan = Plan(
        job.plan_id, job.goal, "Paid active craft retained during independent gather", (craft_step,)
    )
    craft_pending = {
        "action": "factory_craft_job", "started_tick": job.started_tick,
        "polls": 1, "dispatch": "returned",
    }
    memory.background_step = asdict(craft_step)
    memory.background_attempt = make_attempt(
        state.session_id, "rocket_launch", craft_plan.to_dict(), 0, craft_pending,
        process_id=loop._process_id,
    )
    loop.memory = memory
    memory.save(checkpoint)
    loop.memory = BackgroundMemory.load(checkpoint, state.session_id, "rocket_launch")
    return state, backend, loop, checkpoint, job


def _partial_gather(loop, backend, checkpoint, plan, *, tick, observed_ore):
    snapshot = deepcopy(backend.state)
    snapshot.tick = tick
    snapshot.inventory["iron-ore"] = observed_ore
    loop.memory.last_tick = tick
    loop.memory.active_plan = plan.to_dict()
    loop.memory.step_index = 0
    loop.memory.pending = {
        "action": "factory_gather", "started_tick": tick - 10,
        "polls": 1, "dispatch": "ambiguous",
    }
    loop.memory.attempt = make_attempt(
        loop.memory.session_id, loop.target, loop.memory.active_plan, 0,
        loop.memory.pending, process_id=loop._process_id,
    )
    # Validate the full producer checkpoint before each public pending verifier call.
    loop.memory.save(checkpoint)
    loop.memory = BackgroundMemory.load(checkpoint, snapshot.session_id, loop.target)
    backend.state = snapshot
    record = loop.step()
    # The paid craft receipt and the newly appended partial receipt survive reload.
    loop.memory = BackgroundMemory.load(checkpoint, snapshot.session_id, loop.target)
    return snapshot, record


def _initial_gather(loop, state):
    plans, blocker = loop._compile_candidates(state)
    assert blocker == "" and len(plans) == 1
    plan = plans[0]
    assert plan.steps[0].action == "factory_gather"
    assert plan.steps[0].parameters["quantity"] == 20
    return plan


def test_public_steps_scope_each_authenticated_partial_gather_after_checkpoint_reload(tmp_path):
    state, backend, loop, checkpoint, job = _active_job_loop(tmp_path)
    canonical = _initial_gather(loop, state)

    first_snapshot, first_record = _partial_gather(
        loop, backend, checkpoint, canonical, tick=210, observed_ore=6,
    )
    assert first_record["action"] == "reconcile"
    assert loop.memory.background_job["parameters"]["receipt"] == job.parameters["receipt"]
    partials = [row for row in loop.memory.history if row.get("kind") == "gather_partial_progress"]
    assert len(partials) == 1 and partials[0]["observed_inventory"] == 6

    first_remainder, blocker = loop._compile_candidates(first_snapshot)
    assert blocker == "" and len(first_remainder) == 1
    plan14 = first_remainder[0]
    assert plan14.steps[0].parameters["quantity"] == 14
    assert plan14.id == f"{canonical.id}:remainder-quantity:14"

    second_snapshot, second_record = _partial_gather(
        loop, backend, checkpoint, plan14, tick=220, observed_ore=7,
    )
    assert second_record["action"] == "reconcile"
    assert loop.memory.background_job["parameters"]["receipt"] == job.parameters["receipt"]
    partials = [row for row in loop.memory.history if row.get("kind") == "gather_partial_progress"]
    assert [row["observed_inventory"] for row in partials] == [6, 7]
    assert loop.memory.failures[plan14.id] == 1

    second_remainder, blocker = loop._compile_candidates(second_snapshot)
    assert blocker == "" and len(second_remainder) == 1
    plan13 = second_remainder[0]
    assert plan13.steps[0].parameters["quantity"] == 13
    assert plan13.id == f"{canonical.id}:remainder-quantity:13"
    assert loop.memory.failures.get(plan13.id, 0) == 0
    assert backend.actions == [] and backend.observations == 2


def test_active_extension_scopes_before_filtering_canonical_budget_but_keeps_scoped_limit(tmp_path):
    state, _backend, loop, _checkpoint, job = _active_job_loop(tmp_path)
    canonical = _initial_gather(loop, state)
    # This is a valid restored state: an authenticated partial receipt has already
    # proved six units, and prior failures on the original request are exhausted.
    state.inventory["iron-ore"] = 6
    loop.memory.history.append({
        "kind": "gather_partial_progress", "plan": canonical.to_dict(), "step_index": 0,
        "tick": state.tick, "started_tick": max(0, state.tick - 1),
        "session_id": state.session_id, "attempt_id": "restored-attempt-1",
        "observed_inventory": 6,
    })
    loop.memory.failures[canonical.id] = 2
    candidates, blocker = loop._compile_candidates(state)
    scoped_id = f"{canonical.id}:remainder-quantity:14"
    assert blocker == "" and len(candidates) == 1 and candidates[0].id == scoped_id
    assert loop.memory.background_job["parameters"]["receipt"] == job.parameters["receipt"]

    # Exhausting the identical scoped identity still blocks it and preserves the job.
    loop.memory.failures[scoped_id] = 2
    exhausted, blocker = loop._compile_candidates(state)
    assert blocker == "" and len(exhausted) == 1
    assert exhausted[0].steps[0].action == "factory_wait"
    assert exhausted[0].id == f"background-wait:{job.parameters['receipt']}"
    assert loop.memory.background_job["parameters"]["receipt"] == job.parameters["receipt"]


@pytest.mark.parametrize("change", [
    "wrong_session", "wrong_site", "wrong_goal", "wrong_item", "wrong_threshold",
    "missing_attempt", "future_tick", "stale_inventory", "unchanged_progress", "malformed_plan",
])
def test_active_extension_does_not_scope_unmatched_partial_progress(change, tmp_path):
    state, backend, loop, checkpoint, job = _active_job_loop(tmp_path)
    canonical = _initial_gather(loop, state)
    snapshot, record = _partial_gather(
        loop, backend, checkpoint, canonical, tick=210, observed_ore=6,
    )
    assert record["action"] == "reconcile"
    receipt = next(row for row in reversed(loop.memory.history)
                   if row.get("kind") == "gather_partial_progress")
    if change == "wrong_session":
        receipt["session_id"] = "different-session"
    elif change == "wrong_site":
        receipt["plan"]["id"] += ":different-site"
    elif change == "wrong_goal":
        receipt["plan"]["goal"] = "steam_power"
    elif change == "wrong_item":
        receipt["plan"]["steps"][0]["item"] = "copper-ore"
    elif change == "wrong_threshold":
        receipt["plan"]["steps"][0]["threshold"] += 1
    elif change == "missing_attempt":
        receipt.pop("attempt_id")
    elif change == "future_tick":
        receipt["tick"] = snapshot.tick + 1
    elif change == "stale_inventory":
        receipt["observed_inventory"] = 5
    elif change == "unchanged_progress":
        snapshot.inventory["iron-ore"] = 0
        backend.state = snapshot
    elif change == "malformed_plan":
        receipt["plan"] = {"steps": None}

    loop.memory.failures[canonical.id] = 2
    candidates, blocker = loop._compile_candidates(snapshot)
    assert blocker == "" and len(candidates) == 1
    assert candidates[0].id == f"background-wait:{job.parameters['receipt']}"
    assert candidates[0].steps[0].action == "factory_wait"
    assert loop.memory.background_job["parameters"]["receipt"] == job.parameters["receipt"]


def test_research_wait_replacement_scopes_authenticated_gather_before_budget_filter(
        tmp_path, monkeypatch):
    catalog = factory_catalog()
    catalog.technologies["automation"] = {
        "enabled": True, "prerequisites": [], "effects": [],
        "count": 100, "energy_ticks": 600,
        "ingredients": [{"name": "automation-science-pack", "amount": 1},
                         {"name": "logistic-science-pack", "amount": 1}],
    }
    catalog.technologies["rocket-silo"] = {
        "enabled": True, "prerequisites": ["automation"], "effects": [],
        "count": 100, "energy_ticks": 600,
        "ingredients": [{"name": "automation-science-pack", "amount": 1}],
    }
    state = factory_snapshot(inventory={"iron-ore": 6})
    state.factory.update(research="automation", research_progress=0)
    state.factory["entities"] = {
        "utility:water": machine("offshore-pump", fluid_ports=[{"id": 1, "fluid": "water"}]),
        "utility:boiler": machine("boiler", fuel={"coal": 10},
                                  fluid_ports=[{"id": 1, "fluid": "water"},
                                               {"id": 2, "fluid": "steam"}]),
        "utility:engine": machine("steam-engine", electric_network_id=1,
                                  fluid_ports=[{"id": 2, "fluid": "steam"}]),
        "utility:lab": machine("lab", electric_network_id=1,
                               input={"automation-science-pack": 100,
                                      "logistic-science-pack": 100}),
    }
    backend = OfflineBackend(state, catalog)
    loop = BackgroundWorkLoop(
        backend, policy="deterministic", factory_scheduling="ready-work",
        target="rocket_launch", checkpoint=str(tmp_path / "campaign.json"), tick_seconds=0,
    )
    loop.memory = BackgroundMemory(
        state.session_id, "rocket_launch", active_goal="rocket_launch", last_tick=state.tick,
    )
    parent, blocker = HierarchicalLoop._compile_candidates(loop, state)
    assert blocker == "" and parent
    assert parent[0].steps[0].action == "factory_wait"
    assert parent[0].steps[0].effect == "research_progress"

    original_state = deepcopy(state)
    original_state.inventory["iron-ore"] = 0
    original = FactoryPlanner(catalog, original_state, "rocket_launch")._need("iron-ore", 20)
    candidate = FactoryPlanner(catalog, state, "rocket_launch")._need("iron-ore", 20)
    assert original.steps[0].parameters["quantity"] == 20
    assert candidate.steps[0].parameters["quantity"] == 14
    loop.memory.failures[original.id] = 2
    loop.memory.event(
        "gather_partial_progress", session_id=state.session_id,
        plan=original.to_dict(), step_index=0, observed_inventory=6,
        tick=state.tick, started_tick=max(0, state.tick - 1),
        attempt_id="persisted-gather-attempt",
    )
    # Keep the real research compiler and real FactoryPlanner result; isolate only
    # which independent candidates the replacement branch receives.
    monkeypatch.setattr(
        "jev_factorio.background.independent_candidates", lambda *args: [candidate],
    )
    replaced, blocker = loop._compile_candidates(state)
    assert blocker == "" and len(replaced) == 1
    assert replaced[0].id == f"{original.id}:remainder-quantity:14"
    assert replaced[0].steps[0].parameters["quantity"] == 14
