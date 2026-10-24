"""Paid, owned straight solid-item corridors. Layout and placement are not flow.

The initial topology is deliberately narrow: two ordinary electric inserters and
1..24 ordinary belts in one cardinal line. Unsupported turns/joins fail closed.
Native Lua owns collision, inventory, power, payment and transport observations.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import math

COMMAND = "factory_solid_build"
FIELDS = {"route", "layout", "part", "receipt"}
EFFECTS = {"solid_component", "solid_flow"}
MAX_BELTS = 24
MAX_ROUTES = 4
MAX_FLOW_IDLE_TICKS = 600
DIAGNOSTIC_REASONS = {"paid_or_pending_route", "ready_layout", "endpoint_unavailable", "aliased_identity",
                      "mixed_source_items", "missing_owned_power", "obstructed_corridor", "foreign_transport",
                      "incompatible_item_or_inventory", "unsupported_endpoint_or_recipe", "survey_bound",
                      "qualification_failed", "no_supported_corridor", "reserved_corridor"}
DIRECTIONS = {0: (0, -1), 4: (1, 0), 8: (0, 1), 12: (-1, 0)}
SOURCE_NAMES = {"wooden-chest", "iron-chest", "steel-chest", "assembling-machine-1", "assembling-machine-2"}
TARGET_NAMES = {"assembling-machine-1", "assembling-machine-2", "stone-furnace", "steel-furnace",
                "burner-mining-drill", "burner-inserter", "boiler"}


def text(value, limit=128) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit and all(32 <= ord(c) < 127 for c in value)


def integer(value, low=0, high=2**53-1) -> bool:
    return type(value) is int and low <= value <= high


def point(value) -> tuple[float, float]:
    if (not isinstance(value, dict) or set(value) != {"x", "y"}
            or any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 1_000_000
                   for v in value.values())):
        raise ValueError("Invalid solid-route point")
    return value["x"], value["y"]


def validate(parameters: dict) -> None:
    if (not isinstance(parameters, dict) or set(parameters) != FIELDS
            or any(not text(parameters[key]) for key in FIELDS)
            or parameters["part"] not in {"receive", "send", *[f"belt:{i}" for i in range(1, MAX_BELTS+1)]}):
        raise ValueError("Invalid solid-route command")


def _endpoint(value, source: bool) -> None:
    if (not isinstance(value, dict)
            or set(value) != {"role", "unit_number", "name", "inventory", "position", "bounds", "recipe"}
            or not text(value["role"]) or not integer(value["unit_number"], 1)
            or value["name"] not in (SOURCE_NAMES if source else TARGET_NAMES)
            or value["inventory"] not in ({"output", "chest"} if source else {"input", "fuel"})
            or (value["recipe"] != "" and not text(value["recipe"]))):
        raise ValueError("Invalid solid-route endpoint")
    x, y = point(value["position"])
    bounds = value["bounds"]
    if not isinstance(bounds, dict) or set(bounds) != {"left_top", "right_bottom"}:
        raise ValueError("Invalid endpoint bounds")
    left, top = point(bounds["left_top"])
    right, bottom = point(bounds["right_bottom"])
    if not (left < x < right and top < y < bottom and right-left <= 4 and bottom-top <= 4):
        raise ValueError("Invalid endpoint footprint")
    if source:
        if (value["inventory"] == "chest") != value["name"].endswith("chest"):
            raise ValueError("Wrong source inventory")
    elif (value["inventory"] == "input") != value["name"].startswith("assembling-machine"):
        raise ValueError("Wrong target inventory")
    if value["inventory"] in {"input", "output"} and not value["recipe"]:
        raise ValueError("Crafting endpoint needs a fixed recipe")


def _inside(position, endpoint):
    x, y = position
    a, b = endpoint["bounds"]["left_top"], endpoint["bounds"]["right_bottom"]
    return a["x"] < x < b["x"] and a["y"] < y < b["y"]


def validate_row(row: dict, route: str) -> None:
    keys = {"route", "layout", "item", "source", "target", "steps", "parts", "state", "topology", "flow", "reason", "pending"}
    if (not isinstance(row, dict) or set(row) != keys or not text(route) or row["route"] != route
            or not text(row["layout"]) or not text(row["item"], 64)
            or row["state"] not in {"proposed", "building", "ready", "fault"}
            or type(row["topology"]) is not bool or not isinstance(row["flow"], dict)
            or not text(row["reason"]) or not isinstance(row["parts"], dict)
            or not isinstance(row["steps"], list) or not 3 <= len(row["steps"]) <= MAX_BELTS+2):
        raise ValueError("Invalid solid-route row")
    _endpoint(row["source"], True)
    _endpoint(row["target"], False)
    if row["source"]["unit_number"] == row["target"]["unit_number"] or row["source"]["role"] == row["target"]["role"]:
        raise ValueError("Solid route endpoints must be distinct")
    if row["target"]["inventory"] == "fuel" and row["item"] != "coal":
        raise ValueError("Only coal fuel routes are supported")
    count = len(row["steps"])-2
    expected = ["receive", *[f"belt:{i}" for i in range(count, 0, -1)], "send"]
    if [s.get("part") if isinstance(s, dict) else None for s in row["steps"]] != expected:
        raise ValueError("Invalid downstream-first solid construction order")
    positions = {}
    direction = None
    for step in row["steps"]:
        if (set(step) != {"part", "name", "position", "direction"}
                or step["name"] != ("transport-belt" if step["part"].startswith("belt:") else "inserter")
                or type(step["direction"]) is not int or step["direction"] not in DIRECTIONS):
            raise ValueError("Invalid solid component specification")
        position = point(step["position"])
        if any(v % 1 != 0.5 for v in position) or position in positions.values():
            raise ValueError("Solid components overlap or are off grid")
        positions[step["part"]] = position
        # Inserter direction points towards pickup, opposite belt motion.
        flow_direction = step["direction"] if step["name"] == "transport-belt" else (step["direction"]+8) % 16
        if direction is not None and direction != flow_direction:
            raise ValueError("Turns and reversed components are unsupported")
        direction = flow_direction
    dx, dy = DIRECTIONS[direction]
    forward = ["send", *[f"belt:{i}" for i in range(1, count+1)], "receive"]
    for a, b in zip(forward, forward[1:]):
        if positions[b] != (positions[a][0]+dx, positions[a][1]+dy):
            raise ValueError("Disconnected solid corridor")
    send, receive = positions["send"], positions["receive"]
    if not _inside((send[0]-dx, send[1]-dy), row["source"]) or not _inside((receive[0]+dx, receive[1]+dy), row["target"]):
        raise ValueError("Solid corridor misses endpoint inventories")
    if any(_inside(p, endpoint) for p in positions.values() for endpoint in (row["source"], row["target"])):
        raise ValueError("Solid corridor intersects endpoint footprint")
    if set(row["parts"]) != set(expected[:len(row["parts"])]):
        raise ValueError("Non-contiguous solid receipt prefix")
    units = {row["source"]["unit_number"], row["target"]["unit_number"]}
    roles = {row["source"]["role"], row["target"]["role"]}
    receipts = set()
    for paid in row["parts"].values():
        if (not isinstance(paid, dict) or set(paid) != {"role", "receipt", "unit_number", "paid"}
                or not text(paid["role"]) or not text(paid["receipt"])
                or not integer(paid["unit_number"], 1) or not integer(paid["paid"], 1, 1)
                or paid["unit_number"] in units or paid["role"] in roles or paid["receipt"] in receipts):
            raise ValueError("Invalid or duplicate paid solid component")
        units.add(paid["unit_number"]); roles.add(paid["role"]); receipts.add(paid["receipt"])
    full = len(row["parts"]) == len(row["steps"])
    if ((row["state"] == "proposed" and (row["parts"] or row["topology"] or row["flow"]))
            or (row["state"] == "ready" and (not full or not row["topology"]))
            or (row["topology"] and not full) or (row["flow"] and not full)):
        raise ValueError("Inconsistent solid-route phase")
    pending = row["pending"]
    if not isinstance(pending, dict):
        raise ValueError("Invalid solid native pending state")
    if pending and (set(pending) != {"part", "receipt", "phase"}
                    or not text(pending["receipt"]) or pending["phase"] not in {"prepared", "dispatching", "placed"}
                    or full or pending["part"] != expected[len(row["parts"])] or row["state"] == "proposed"):
        raise ValueError("Invalid solid native pending binding")
    if row["flow"]:
        flow = row["flow"]
        if (set(flow) != {"layout", "source_unit", "target_unit", "method", "first_tick", "last_tick",
                         "last_positive_tick", "positive_samples", "sent", "received", "unattributed_loss"}
                or flow["layout"] != row["layout"]
                or not integer(flow["source_unit"], 1) or flow["source_unit"] != row["source"]["unit_number"]
                or not integer(flow["target_unit"], 1) or flow["target_unit"] != row["target"]["unit_number"]
                or any(not integer(flow[key]) for key in ("first_tick", "last_tick", "last_positive_tick", "positive_samples", "sent", "received", "unattributed_loss"))
                or not flow["first_tick"] <= flow["last_positive_tick"] <= flow["last_tick"]
                or flow["method"] not in ({"exclusive_fuel_lower_bound", "exclusive_mined_coal_lower_bound"} if row["target"]["inventory"] == "fuel" else {"stoichiometric_balance"})
                or (flow["method"] == "stoichiometric_balance" and flow["unattributed_loss"] != 0)
                or flow["received"] > flow["sent"]):
            raise ValueError("Invalid solid-flow evidence")



def validate_corridor_reservations(rows: dict) -> None:
    """Check full footprints of already validated observations or commitments.

    Unpaid proposals may overlap as alternatives. Once a route is prepared,
    building, ready or faulted, all of its planned component cells and a one-tile
    join clearance remain reserved. Missing state means a durable commitment,
    not a proposal. This cannot release a pending/paid route or authorize a build.
    """
    values = list(rows.values())
    for index, left in enumerate(values):
        for right in values[index + 1:]:
            if left.get("state") == right.get("state") == "proposed":
                continue
            for a in left["steps"]:
                for b in right["steps"]:
                    distance = sum(abs(a["position"][axis] - b["position"][axis])
                                   for axis in ("x", "y"))
                    if distance <= 1.01:
                        raise ValueError("Solid corridor reservation conflict")


def _routes(snapshot, *, completion: bool) -> dict:
    data = snapshot.factory.get("solid_routes")
    fields = {"protocol", "session_id", "tick", "actor_index", "surface_index", "force_index", "routes", "diagnostics"}
    if completion:
        fields.add("diagnostics_tick")
    if (not isinstance(data, dict) or set(data) != fields
            or not integer(data["protocol"], 1, 1) or data["session_id"] != snapshot.session_id
            or not integer(data["tick"]) or data["tick"] != snapshot.tick
            or any(not integer(data[key], 1) for key in ("actor_index", "surface_index", "force_index"))
            or not isinstance(data["routes"], dict) or len(data["routes"]) > MAX_ROUTES):
        raise ValueError("Missing or stale solid-route snapshot")
    if completion and (not integer(data["diagnostics_tick"])
                       or data["diagnostics_tick"] > snapshot.tick
                       or snapshot.tick - data["diagnostics_tick"] > MAX_FLOW_IDLE_TICKS):
        raise ValueError("Solid diagnostic evidence is future-dated or expired")
    diagnostics = data["diagnostics"]
    if not isinstance(diagnostics, list) or not 0 <= len(diagnostics) <= MAX_ROUTES:
        raise ValueError("Invalid solid diagnostic cardinality")
    for index, value in enumerate(diagnostics, 1):
        if (not isinstance(value, dict) or set(value) != {"intent_index", "state", "reason"}
                or not integer(value["intent_index"], index, index)
                or value["state"] not in {"proposed", "committed", "unavailable"}
                or value["reason"] not in DIAGNOSTIC_REASONS):
            raise ValueError("Invalid solid diagnostic vocabulary")
    runtime = snapshot.factory.get("acceptance_runtime")
    if isinstance(runtime, dict):
        for a, b in (("actor_index", "player_index"), ("surface_index", "surface_index"), ("force_index", "force_index")):
            if not integer(runtime.get(b), 1) or data[a] != runtime[b]:
                raise ValueError("Solid snapshot actor epoch disagrees with native runtime")
    endpoints, components, receipts = set(), set(), set()
    for key, row in data["routes"].items():
        validate_row(row, key)
        if row["flow"].get("method") == "exclusive_mined_coal_lower_bound":
            from .coal_supply import binds_solid_flow
            if not binds_solid_flow(row, snapshot, completion=completion):
                raise ValueError("Mined coal route lacks matching source evidence")
        # Initial topology permits one corridor per endpoint; no silent branches.
        ids = {row["source"]["unit_number"], row["target"]["unit_number"]}
        new = {p["unit_number"] for p in row["parts"].values()}
        new_receipts = {p["receipt"] for p in row["parts"].values()}
        if ids & (endpoints | components) or new & (endpoints | components) or new_receipts & receipts:
            raise ValueError("Solid routes share an endpoint, component or receipt")
        endpoints.update(ids); components.update(new); receipts.update(new_receipts)
        if row["flow"]:
            flow_tick = row["flow"]["last_tick"]
            if completion:
                if flow_tick > snapshot.tick:
                    raise ValueError("Solid flow evidence is from the future")
            elif flow_tick != snapshot.tick:
                raise ValueError("Solid flow evidence is not from this observation")
    validate_corridor_reservations(data["routes"])
    return data["routes"]


def routes(snapshot) -> dict:
    """Validate a route for ordinary planning at this exact observation tick."""
    return _routes(snapshot, completion=False)


def completion_routes(snapshot) -> dict:
    """Validate retained routes for completion without refreshing telemetry.

    A completion query is read-only and can span ordinary game ticks. It may
    therefore consume an earlier flow sample, but only when the outer route
    observation is current and the sample is not from the future. Callers must
    still require :func:`completion_flow_complete`, whose existing 600-tick
    positive-flow bound and ownership checks determine whether the sample is
    sufficiently fresh for completion.
    """
    return _routes(snapshot, completion=True)


def current(row, snapshot) -> bool:
    if row["state"] == "fault":
        return False
    entities = snapshot.factory.get("entities", {})
    for endpoint in (row["source"], row["target"]):
        entity = entities.get(endpoint["role"], {})
        if (entity.get("unit_number") != endpoint["unit_number"] or type(entity.get("unit_number")) is not int
                or entity.get("name") != endpoint["name"] or entity.get("position") != endpoint["position"]
                or (bool(endpoint["recipe"]) and entity.get("recipe") != endpoint["recipe"])):
            return False
    identities = {e["unit_number"]: e["role"] for e in (row["source"], row["target"])}
    identities.update({v["unit_number"]: v["role"] for v in row["parts"].values()})
    for role, entity in entities.items():
        unit = entity.get("unit_number") if isinstance(entity, dict) else None
        if type(unit) is int and unit in identities and identities[unit] != role:
            return False
    for step in row["steps"]:
        paid = row["parts"].get(step["part"])
        if paid:
            entity = entities.get(paid["role"], {})
            if (entity.get("unit_number") != paid["unit_number"] or type(entity.get("unit_number")) is not int
                    or entity.get("name") != step["name"] or entity.get("position") != step["position"]):
                return False
    return True


def remaining(row) -> dict[str, int]:
    return dict(Counter(s["name"] for s in row["steps"] if s["part"] not in row["parts"]))


def allowed(parameters, snapshot) -> bool:
    validate(parameters)
    try:
        row = routes(snapshot).get(parameters["route"])
        todo = [s for s in row["steps"] if s["part"] not in row["parts"]] if row else []
        return bool(row and current(row, snapshot) and row["layout"] == parameters["layout"] and todo
                    and todo[0]["part"] == parameters["part"]
                    and (not row["pending"] or row["pending"] == {"part": parameters["part"], "receipt": parameters["receipt"], "phase": "prepared"})
                    and all(type(snapshot.inventory.get(k, 0)) is int and snapshot.inventory.get(k, 0) >= v for k, v in remaining(row).items())
                    and snapshot.factory.get("player_bound") is True and snapshot.factory.get("player_connected") is True
                    and snapshot.factory.get("crafting_queue") == 0)
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def component_complete(parameters, snapshot) -> bool:
    validate(parameters)
    try:
        row = routes(snapshot).get(parameters["route"])
        return bool(row and current(row, snapshot) and row["layout"] == parameters["layout"]
                    and row["parts"].get(parameters["part"], {}).get("receipt") == parameters["receipt"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def flow_complete(route, layout, snapshot) -> bool:
    try:
        row = routes(snapshot).get(route)
        if (not row or not current(row, snapshot) or row["layout"] != layout or row["state"] != "ready" or not row["topology"]
                or row["reason"] != "observing_flow"):
            return False
        flow = row["flow"]
        return bool(flow and flow["last_tick"]-flow["first_tick"] >= 120
                    and min(flow["positive_samples"], flow["sent"], flow["received"]) >= 3
                    and flow["last_tick"]-flow["last_positive_tick"] <= MAX_FLOW_IDLE_TICKS)
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def completion_flow_complete(route, layout, snapshot) -> bool:
    """Completion-only counterpart that never relaxes ordinary route planning."""
    try:
        row = completion_routes(snapshot).get(route)
        if (not row or not current(row, snapshot) or row["layout"] != layout or row["state"] != "ready" or not row["topology"]
                or row["reason"] != "observing_flow"):
            return False
        flow = row["flow"]
        return bool(flow and flow["last_tick"] <= snapshot.tick
                    and flow["last_tick"]-flow["first_tick"] >= 120
                    and min(flow["positive_samples"], flow["sent"], flow["received"]) >= 3
                    and flow["last_tick"]-flow["last_positive_tick"] <= MAX_FLOW_IDLE_TICKS
                    and snapshot.tick-flow["last_positive_tick"] <= MAX_FLOW_IDLE_TICKS)
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def permits(action, parameters, snapshot) -> bool:
    """Lock connected flow; retain sequential manual service before sender takeover."""
    try:
        for row in routes(snapshot).values():
            if not current(row, snapshot):
                return False
            if row["state"] == "proposed":
                continue
            role = parameters.get("role")
            if role in {p["role"] for p in row["parts"].values()} and action in {"factory_insert", "factory_extract", "factory_configure"}:
                return False
            if role in {row["source"]["role"], row["target"]["role"]}:
                if action == "factory_configure":
                    return False
                if action in {"factory_insert", "factory_extract"} and parameters.get("item") == row["item"]:
                    # The sender is paid last. Before it exists, a partial route
                    # cannot withdraw from the source and has no flow baseline.
                    # Retain useful manual service without relaxing a pending
                    # mutation, recipe lock, or connected-route accounting.
                    if not (row["state"] == "building" and not row["pending"]
                            and "send" not in row["parts"]):
                        from .coal_supply import manual_permitted
                        if (row["pending"] or role != row["target"]["role"]
                                or not manual_permitted(action, parameters, snapshot)):
                            return False
        return True
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def commitment(row: dict) -> dict:
    """Capture exact geometry/identities and paid prefix, excluding advisory flow."""
    return deepcopy({key: row[key] for key in ("route", "layout", "item", "source", "target", "steps", "parts")})


def validate_commitment(saved: dict, route: str) -> None:
    if not isinstance(saved, dict) or set(saved) != {"route", "layout", "item", "source", "target", "steps", "parts"}:
        raise ValueError("Invalid solid checkpoint")
    validate_row({**saved, "state": "building", "topology": False, "flow": {}, "reason": "checkpoint", "pending": {}}, route)


def reconciles(saved: dict, row: dict) -> bool:
    actual = commitment(row)
    return (row["state"] != "proposed" and all(saved[k] == actual[k] for k in saved if k != "parts")
            and all(actual["parts"].get(k) == v for k, v in saved["parts"].items()))
