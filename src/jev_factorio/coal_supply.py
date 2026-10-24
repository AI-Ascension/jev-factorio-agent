"""Opt-in paid coal supply: dedicated electric source/corridor per consumer.

A proposal is not placement, a connected route is not flow, and resource-balance
lower bounds are not native acceptance. The controller and Lua extension retain
both source and corridor ownership; neither module invents a shared splitter.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import math

from . import solid_routes as solid

COMMAND = "factory_coal_build"
FIELDS = {"target", "layout", "part", "receipt"}
EFFECTS = {"coal_component", "coal_network_flow"}
PARTS = {"chest": "wooden-chest", "drill": "electric-mining-drill"}
MAX_TARGETS = 4
MIN_ORE = 100
METHOD = "exclusive_mined_coal_lower_bound"
REASONS = {"proposal", "building", "awaiting_corridor", "source_unbuilt", "observing_flow",
           "no_power", "backpressure", "depleted", "qualification_failed", "no_supported_bundle",
           "identity_topology_or_balance_mismatch", "ambiguous_dispatch", "receipt_reconciliation_failed",
           "manual_transfer_ambiguous"}
ROW_KEYS = {"target", "layout", "steps", "corridor", "chest_bounds", "drill_bounds", "mining_area",
            "parts", "state", "remaining", "power", "route", "flow", "reason", "pending", "manual_pending"}
COMMITMENT_KEYS = {"target", "layout", "steps", "corridor", "chest_bounds", "drill_bounds", "mining_area", "parts"}
FLOW_INTS = {"first_tick", "last_tick", "last_positive_tick", "positive_samples", "mined",
             "delivered_lower", "manual_inserted", "drill_unit", "chest_unit", "target_unit"}
FLOW_NUMBERS = {"burned_lower_joules", "initial_fuel_joules", "target_fuel_joules", "fuel_value_joules"}


def validate_targets(targets) -> list[str]:
    if (not isinstance(targets, (list, tuple)) or not 2 <= len(targets) <= MAX_TARGETS
            or any(not solid.text(t, 48) or t.startswith("coal:") for t in targets)
            or len(set(targets)) != len(targets)):
        raise ValueError("Coal supply needs two to four distinct explicit consumer roles")
    return list(targets)


def role(target: str, part: str) -> str:
    if not solid.text(target, 48) or part not in PARTS:
        raise ValueError("Invalid coal source role")
    return f"coal:{target}:{part}"


def intents(targets) -> list[dict[str, str]]:
    return [{"source": role(t, "chest"), "target": t, "item": "coal", "destination": "fuel"}
            for t in validate_targets(targets)]


def validate_transport_intents(targets, transport_intents) -> list[dict[str, str]]:
    """Bind every coal consumer inside one bounded, disjoint transport treatment.

    Additional input routes are allowed, but cannot borrow a coal endpoint or its
    reserved role namespace. The complete ordered intent list remains immutable
    in the existing solid checkpoint and native binding; this is not migration.
    """
    from .backends.solid_routes import validate_intents
    checked = validate_intents(transport_intents)
    required = intents(targets)
    if any(intent not in checked for intent in required):
        raise ValueError("Coal treatment is missing an exact dedicated fuel corridor")
    for intent in checked:
        if intent in required:
            continue
        if (intent["destination"] != "input" or intent["item"] == "coal"
                or any(intent[key].startswith("coal:") for key in ("source", "target"))):
            raise ValueError("Extra transport must be independent downstream input work")
    return checked


def validate(parameters: dict) -> None:
    if (not isinstance(parameters, dict) or set(parameters) != FIELDS
            or any(not solid.text(v) for v in parameters.values())
            or not solid.text(parameters["target"], 48) or parameters["part"] not in PARTS):
        raise ValueError("Invalid paid coal source command")


def _number(value, low=0, high=2**53 - 1) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def _bounds(value, center=None, maximum=6):
    if not isinstance(value, dict) or set(value) != {"left_top", "right_bottom"}:
        raise ValueError("Invalid coal geometry bounds")
    a, b = solid.point(value["left_top"]), solid.point(value["right_bottom"])
    if not (0 < b[0] - a[0] <= maximum and 0 < b[1] - a[1] <= maximum):
        raise ValueError("Unsupported coal footprint")
    if center is not None and not (a[0] < center[0] < b[0] and a[1] < center[1] < b[1]):
        raise ValueError("Coal footprint misses its center")
    return a, b


def _overlap(a, b) -> bool:
    return (a[0][0] < b[1][0] and b[0][0] < a[1][0]
            and a[0][1] < b[1][1] and b[0][1] < a[1][1])


def _corridor(row, target):
    """Reuse the exact solid path validator for hypothetical geometry only.

    The temporary distinct identifier is not published, returned, or used for
    permission. Only native paid parts have physical identities in this module.
    """
    source = {"role": role(target, "chest"), "unit_number": 1 if row["target"]["unit_number"] != 1 else 2,
              "name": PARTS["chest"], "inventory": "chest", "position": row["steps"][0]["position"],
              "bounds": row["chest_bounds"], "recipe": ""}
    solid.validate_row({"route": "geometry-only", "layout": row["layout"], "item": "coal",
        "source": source, "target": row["target"], "steps": row["corridor"], "parts": {},
        "state": "proposed", "topology": False, "flow": {}, "pending": {}, "reason": "geometry-only"}, "geometry-only")
    drill = _bounds(row["drill_bounds"], solid.point(row["steps"][1]["position"]), 3)
    if _overlap(drill, _bounds(row["chest_bounds"], maximum=1)) or _overlap(drill, _bounds(row["target"]["bounds"], maximum=4)):
        raise ValueError("Coal source footprints overlap")
    for step in row["corridor"]:
        x, y = solid.point(step["position"])
        if _overlap(drill, ((x-.49, y-.49), (x+.49, y+.49))):
            raise ValueError("Coal drill obstructs its receiving corridor")


def validate_row(row: dict, target: str) -> None:
    if (not isinstance(row, dict) or set(row) != ROW_KEYS
            or not solid.text(target, 48) or not solid.text(row["layout"])
            or row["state"] not in {"proposed", "building", "ready", "depleted", "fault"}
            or not solid.integer(row["remaining"]) or row["reason"] not in REASONS
            or not isinstance(row["steps"], list) or len(row["steps"]) != 2
            or not isinstance(row["parts"], dict)
            or set(row["parts"]) not in (set(), {"chest"}, {"chest", "drill"})
            or not isinstance(row["flow"], dict) or not isinstance(row["pending"], dict)
            or not isinstance(row["manual_pending"], dict)
            or (row["route"] != "" and not solid.text(row["route"]))):
        raise ValueError("Invalid coal source row")
    solid._endpoint(row["target"], False)
    if row["target"]["role"] != target or row["target"]["inventory"] != "fuel":
        raise ValueError("Coal supply targets a distinct owned fuel inventory")
    for spec, part in zip(row["steps"], PARTS):
        if (not isinstance(spec, dict) or set(spec) != {"part", "name", "position", "direction"}
                or spec["part"] != part or spec["name"] != PARTS[part]
                or type(spec["direction"]) is not int
                or spec["direction"] not in ({0} if part == "chest" else solid.DIRECTIONS)):
            raise ValueError("Invalid coal source part specification")
        if any(v % 1 != .5 for v in solid.point(spec["position"])):
            raise ValueError("Coal source is off the normal three/one-tile grid")
    _bounds(row["chest_bounds"], solid.point(row["steps"][0]["position"]), 1)
    _bounds(row["mining_area"], solid.point(row["steps"][1]["position"]), 6)
    _corridor(row, target)
    power = row["power"]
    if (not isinstance(power, dict) or set(power) != {"pole_role", "pole_unit", "network_id", "witness_role", "witness_unit", "energized"}
            or not solid.text(power["pole_role"]) or not solid.text(power["witness_role"])
            or any(not solid.integer(power[k], 1) for k in ("pole_unit", "network_id", "witness_unit"))
            or power["pole_unit"] == power["witness_unit"] or type(power["energized"]) is not bool):
        raise ValueError("Missing qualified owned electric network")
    units, receipts = {row["target"]["unit_number"]}, set()
    for part, paid in row["parts"].items():
        if (not isinstance(paid, dict) or set(paid) != {"role", "unit_number", "receipt", "paid"}
                or paid["role"] != role(target, part) or not solid.integer(paid["unit_number"], 1)
                or not solid.integer(paid["paid"], 1, 1) or not solid.text(paid["receipt"])
                or paid["unit_number"] in units or paid["receipt"] in receipts):
            raise ValueError("Invalid coal payment or aliased ownership")
        units.add(paid["unit_number"]); receipts.add(paid["receipt"])
    full = len(row["parts"]) == 2
    if (row["state"] == "proposed" and (row["parts"] or row["flow"] or row["pending"] or row["manual_pending"])
            or row["state"] in {"ready", "depleted"} and (not full or not row["route"])
            or row["state"] == "depleted" and row["remaining"] != 0
            or row["flow"] and (not full or not row["route"])):
        raise ValueError("Inconsistent coal source phase")
    pending = row["pending"]
    if pending and (set(pending) != {"part", "receipt", "phase"}
                    or full or pending["part"] != ("drill" if row["parts"] else "chest")
                    or not solid.text(pending["receipt"])
                    or pending["phase"] not in {"prepared", "dispatching", "placed"}):
        raise ValueError("Invalid coal write-ahead journal")
    manual = row["manual_pending"]
    if manual and (set(manual) != {"receipt", "quantity", "phase", "unit_number"}
                   or not solid.text(manual["receipt"]) or not solid.integer(manual["quantity"], 1, 200)
                   or manual["phase"] not in {"dispatching", "applied"}
                   or manual["unit_number"] != row["target"]["unit_number"]):
        raise ValueError("Invalid manual coal journal")
    flow = row["flow"]
    if not flow:
        return
    if (set(flow) != FLOW_INTS | FLOW_NUMBERS | {"layout", "route_layout", "method", "in_transit_uncertainty", "self_fuel"}
            or flow["layout"] != row["layout"] or not solid.text(flow["route_layout"])
            or flow["method"] != METHOD or type(flow["in_transit_uncertainty"]) is not int or flow["in_transit_uncertainty"] != 1
            or type(flow["self_fuel"]) is not int or flow["self_fuel"] != 0
            or any(not solid.integer(flow[k], 1 if k.endswith("_unit") else 0) for k in FLOW_INTS)
            or any(not _number(flow[k]) for k in FLOW_NUMBERS) or flow["fuel_value_joules"] <= 0
            or flow["drill_unit"] != row["parts"]["drill"]["unit_number"]
            or flow["chest_unit"] != row["parts"]["chest"]["unit_number"]
            or flow["target_unit"] != row["target"]["unit_number"]
            or not flow["first_tick"] <= flow["last_positive_tick"] <= flow["last_tick"]
            or flow["delivered_lower"] > flow["mined"]
            or flow["burned_lower_joules"] > flow["initial_fuel_joules"] + (flow["mined"] + flow["manual_inserted"])*flow["fuel_value_joules"] + 1):
        raise ValueError("Invalid coal source/consumer lower-bound evidence")


def corridor_reservations_clear(cell, rows) -> bool:
    """Full future corridors and source footprints remain exclusive after payment."""
    for target, row in rows.items():
        if cell["source"]["role"] == role(target, "chest"):
            continue
        for step in cell["steps"]:
            x, y = solid.point(step["position"])
            if any(abs(x - other["position"]["x"]) + abs(y - other["position"]["y"]) <= 1.01
                   for other in row["corridor"]):
                return False
            footprint = ((x - .49, y - .49), (x + .49, y + .49))
            if any(_overlap(footprint, _bounds(row[key])) for key in ("chest_bounds", "drill_bounds")):
                return False
    return True


def _sources(snapshot, *, completion: bool) -> dict:
    data = snapshot.factory.get("coal_supply")
    base_fields = {"protocol", "session_id", "tick", "actor_index", "surface_index", "force_index", "targets", "committed", "sources", "reason"}
    if not isinstance(data, dict) or type(data.get("protocol")) is not int or data["protocol"] not in (1, 2):
        raise ValueError("Missing or stale coal supply observation")
    if (set(data) != (base_fields if data["protocol"] == 1 else base_fields | {"admission"})
            or data["session_id"] != snapshot.session_id
            or not solid.integer(data["tick"]) or data["tick"] != snapshot.tick
            or any(not solid.integer(data[k], 1) for k in ("actor_index", "surface_index", "force_index"))
            or type(data["committed"]) is not bool or not isinstance(data["sources"], dict)
            or data["reason"] not in REASONS):
        raise ValueError("Missing or stale coal supply observation")
    if data["protocol"] == 2:
        admission = data["admission"]
        bound = {"session_id", "tick", "actor_index", "surface_index", "force_index"}
        if (not isinstance(admission, dict)
                or set(admission) != bound | {"protocol", "qualified", "reason"}
                or type(admission["protocol"]) is not int or admission["protocol"] != 1
                or any(admission[key] != data[key] for key in bound)
                or admission["qualified"] is not False
                or admission["reason"] != "electric_conversion_and_construction_cost_unknown"):
            raise ValueError("Coal admission evidence is incomplete or unbound")
    targets = validate_targets(data["targets"])
    rows = data["sources"]
    if (rows and set(rows) != set(targets)) or (data["committed"] and not rows):
        raise ValueError("Coal bundle lost a configured consumer")
    epoch = snapshot.factory.get("solid_routes", {})
    if any(epoch.get(k) != data[k] for k in ("actor_index", "surface_index", "force_index")):
        raise ValueError("Coal and solid actor epochs disagree")
    units, receipts, layouts, areas = set(), set(), set(), []
    previous_rows = {}
    for target, row in rows.items():
        validate_row(row, target)
        if not corridor_reservations_clear({"source": {"role": role(target, "chest")},
                                            "steps": row["corridor"]}, previous_rows):
            raise ValueError("Coal branch corridor reservation conflict")
        previous_rows[target] = row
        if (row["state"] == "proposed") is data["committed"]:
            raise ValueError("Coal bundle commitment phase disagrees")
        layout = row["layout"]; layouts.add(layout)
        ids = {row["target"]["unit_number"], *[p["unit_number"] for p in row["parts"].values()]}
        new_receipts = {p["receipt"] for p in row["parts"].values()}
        if units & ids or receipts & new_receipts:
            raise ValueError("Coal sources or consumers are not independent")
        units.update(ids); receipts.update(new_receipts)
        area = _bounds(row["mining_area"])
        if any(_overlap(area, prior) for prior in areas):
            raise ValueError("Coal branches share a resource accounting area")
        areas.append(area)
        if row["flow"]:
            flow_tick = row["flow"]["last_tick"]
            if completion:
                if flow_tick > snapshot.tick:
                    raise ValueError("Coal flow evidence is from the future")
            elif flow_tick != snapshot.tick:
                raise ValueError("Coal flow does not belong to this observation")
    for cell in epoch.get("routes", {}).values():
        solid.validate_row(cell, cell["route"])
        if (cell["state"] != "proposed" or cell["pending"]) and not corridor_reservations_clear(cell, rows):
            raise ValueError("Coal/solid committed corridor reservation conflict")
    if len(layouts) > 1:
        raise ValueError("Coal bundle mixes source generations")
    return rows


def sources(snapshot) -> dict:
    """Validate source telemetry for ordinary exact-tick controller use."""
    return _sources(snapshot, completion=False)


def completion_sources(snapshot) -> dict:
    """Validate retained paid sources for read-only completion preflight."""
    return _sources(snapshot, completion=True)


def private_source_roles(snapshot) -> frozenset[str]:
    """Source buffers belong to their coal network, not actor-haulable supply.

    Resolve the same native identities as the source contract on every snapshot.
    Aliases of a paid source are excluded too; the controller separately rejects
    aliased ownership. A malformed/stale present extension is not a legacy mode.
    This neither unlocks inventory nor changes source/corridor commitments.
    """
    if "coal_supply" not in snapshot.factory:
        return frozenset()
    rows = sources(snapshot)
    reserved_roles = {role(target, part) for target in snapshot.factory["coal_supply"]["targets"]
                      for part in PARTS}
    units = {paid["unit_number"] for row in rows.values() for paid in row["parts"].values()}
    entities = snapshot.factory.get("entities", {})
    if not isinstance(entities, dict):
        raise ValueError("Invalid coal source entity inventory")
    return frozenset(reserved_roles | {
        name for name, machine in entities.items()
        if isinstance(machine, dict) and type(machine.get("unit_number")) is int
        and machine["unit_number"] in units})


def current(row, snapshot) -> bool:
    if row["state"] == "fault" or row["manual_pending"]:
        return False
    entities = snapshot.factory.get("entities", {})
    if not isinstance(entities, dict):
        return False
    power = row["power"]
    for name, unit in ((power["pole_role"], power["pole_unit"]), (power["witness_role"], power["witness_unit"])):
        actual = entities.get(name, {})
        if (not isinstance(actual, dict) or type(actual.get("unit_number")) is not int
                or actual["unit_number"] != unit or actual.get("electric_network_id") != power["network_id"]
                or any(other != name and isinstance(e, dict) and e.get("unit_number") == unit for other, e in entities.items())):
            return False
    witness = entities[power["witness_role"]]
    if power["energized"] and (not _number(witness.get("energy")) or witness["energy"] <= 0):
        return False
    owned = {row["target"]["role"]: row["target"]}
    for spec in row["steps"]:
        paid = row["parts"].get(spec["part"])
        if paid:
            owned[paid["role"]] = {**spec, **paid}
    for name, expected in owned.items():
        actual = entities.get(name, {})
        if (not isinstance(actual, dict) or type(actual.get("unit_number")) is not int
                or actual.get("unit_number") != expected["unit_number"]
                or actual.get("name") != expected["name"] or actual.get("position") != expected["position"]):
            return False
        if any(other != name and isinstance(e, dict) and e.get("unit_number") == expected["unit_number"]
               for other, e in entities.items()):
            return False
    return True


def route_for(row, snapshot, *, completion: bool = False):
    route_rows = solid.completion_routes(snapshot) if completion else solid.routes(snapshot)
    matches = [r for r in route_rows.values()
               if r["source"]["role"] == role(row["target"]["role"], "chest")]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("Coal source has more than one corridor")
    r = matches[0]
    if (r["target"] != row["target"] or r["steps"] != row["corridor"] or r["item"] != "coal"
            or r["source"]["unit_number"] != row["parts"].get("chest", {}).get("unit_number")
            or r["source"]["position"] != row["steps"][0]["position"]
            or r["source"]["bounds"] != row["chest_bounds"] or not solid.current(r, snapshot)):
        raise ValueError("Coal corridor differs from the frozen source bundle")
    return r


def reserved_components(commitments, solid_commitments) -> dict[str, int]:
    """Coal locks not already counted by committed solid corridors."""
    bill = Counter()
    corridor_sources = {saved["source"]["role"] for saved in solid_commitments.values()}
    for target, saved in commitments.items():
        bill.update(spec["name"] for spec in saved["steps"] if spec["part"] not in saved["parts"])
        if role(target, "chest") not in corridor_sources:
            bill.update(spec["name"] for spec in saved["corridor"])
    return dict(bill)


def remaining_kit(rows, snapshot) -> dict[str, int]:
    bill = Counter()
    for row in rows.values():
        bill.update(spec["name"] for spec in row["steps"] if spec["part"] not in row["parts"])
        r = route_for(row, snapshot)
        bill.update(solid.remaining(r) if r else Counter(s["name"] for s in row["corridor"]))
    return dict(bill)


def allowed(parameters, snapshot) -> bool:
    validate(parameters)
    try:
        rows = sources(snapshot); row = rows.get(parameters["target"])
        if not row or not all(current(r, snapshot) for r in rows.values()):
            return False
        todo = [part for part in PARTS if part not in row["parts"]]
        if (not todo or todo[0] != parameters["part"] or row["layout"] != parameters["layout"]
                or row["remaining"] < MIN_ORE or not row["power"]["energized"]
                or row["pending"] and row["pending"] != {"part": parameters["part"], "receipt": parameters["receipt"], "phase": "prepared"}
                or snapshot.factory.get("player_bound") is not True or snapshot.factory.get("player_connected") is not True
                or snapshot.factory.get("crafting_queue") != 0):
            return False
        if parameters["part"] == "drill":
            route = route_for(row, snapshot)
            if not route or route["state"] != "ready" or not route["topology"] or route["pending"]:
                return False
        return all(solid.integer(snapshot.inventory.get(k, 0)) and snapshot.inventory.get(k, 0) >= v
                   for k, v in remaining_kit(rows, snapshot).items())
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def component_complete(parameters, snapshot) -> bool:
    validate(parameters)
    try:
        row = sources(snapshot).get(parameters["target"])
        return bool(row and current(row, snapshot) and row["layout"] == parameters["layout"]
                    and row["parts"].get(parameters["part"], {}).get("receipt") == parameters["receipt"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def is_network_route(parameters, snapshot) -> bool:
    try:
        r = solid.routes(snapshot).get(parameters.get("route"))
        return bool(r and any(role(t, "chest") == r["source"]["role"]
                              and row["target"] == r["target"] for t, row in sources(snapshot).items()))
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def manual_permitted(action, parameters, snapshot) -> bool:
    if action != "factory_insert" or parameters.get("item") != "coal":
        return False
    try:
        row = sources(snapshot).get(parameters.get("role"))
        return bool(row and current(row, snapshot) and not row["pending"] and not row["manual_pending"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def permits(action, parameters, snapshot) -> bool:
    try:
        rows = sources(snapshot)
        if any(not current(row, snapshot) for row in rows.values()):
            return False
        if not snapshot.factory["coal_supply"]["committed"]:
            return True
        if action == solid.COMMAND:
            cell = solid.routes(snapshot).get(parameters.get("route"))
            if not cell or not corridor_reservations_clear(cell, rows):
                return False
        selected = parameters.get("role")
        for target, row in rows.items():
            if selected in {role(target, part) for part in PARTS}:
                return False  # Source output is exclusively owned, never actor bootstrap coal.
            if selected == target and action == "factory_configure":
                return False
            if selected == target and parameters.get("item") == "coal" and action in {"factory_insert", "factory_extract"}:
                return manual_permitted(action, parameters, snapshot)
        return not str(selected or "").startswith("coal:")
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def _flow_complete(snapshot, *, completion: bool) -> bool:
    try:
        rows = completion_sources(snapshot) if completion else sources(snapshot)
        if len(rows) < 2:
            return False
        for row in rows.values():
            f = row["flow"]; route = route_for(row, snapshot, completion=completion)
            if (not current(row, snapshot) or not row["power"]["energized"] or row["reason"] == "no_power"
                    or row["state"] not in {"ready", "depleted"} or not f or not route
                    or f["route_layout"] != route["layout"] or not route["topology"]
                    or f["last_tick"] > snapshot.tick
                    or f["last_tick"] - f["first_tick"] < 120 or f["positive_samples"] < 3
                    or f["last_tick"]-f["last_positive_tick"] > solid.MAX_FLOW_IDLE_TICKS
                    or snapshot.tick - f["last_positive_tick"] > solid.MAX_FLOW_IDLE_TICKS
                    or f["delivered_lower"] < 3 or f["burned_lower_joules"] <= 0):
                return False
        return True
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def flow_complete(snapshot) -> bool:
    return _flow_complete(snapshot, completion=False)


def completion_flow_complete(snapshot) -> bool:
    """Completion-only flow validator; ordinary controller checks stay exact-tick."""
    return _flow_complete(snapshot, completion=True)


def commitment(row) -> dict:
    return deepcopy({k: row[k] for k in COMMITMENT_KEYS})


def reconciles(saved, row) -> bool:
    return (row["state"] != "proposed" and all(saved[k] == row[k] for k in COMMITMENT_KEYS - {"parts"})
            and all(row["parts"].get(k) == v for k, v in saved["parts"].items()))


def validate_commitment(saved, target: str) -> None:
    """Validate frozen paid-prefix state without inventing live readiness."""
    if not isinstance(saved, dict) or set(saved) != COMMITMENT_KEYS:
        raise ValueError("Incomplete coal source checkpoint commitment")
    # Power/flow are fresh evidence and deliberately not persisted permissions.
    validate_row({**deepcopy(saved), "state": "building", "remaining": 0,
                  "power": {"pole_role": "checkpoint:power", "pole_unit": 1, "network_id": 1,
                            "witness_role": "checkpoint:witness", "witness_unit": 2, "energized": False},
                  "route": "", "flow": {}, "reason": "building", "pending": {}, "manual_pending": {}}, target)


def binds_solid_flow(route: dict, snapshot, *, completion: bool = False) -> bool:
    """Cross-bind mined-flow telemetry without recursively calling solid.routes."""
    try:
        rows = completion_sources(snapshot) if completion else sources(snapshot)
        row = rows[route["target"]["role"]]
        f, g = row["flow"], route["flow"]
        return bool(current(row, snapshot) and f and row["route"] == route["route"]
            and route["source"]["role"] == role(row["target"]["role"], "chest")
            and route["target"] == row["target"] and route["steps"] == row["corridor"]
            and route["source"]["unit_number"] == f["chest_unit"]
            and route["source"]["bounds"] == row["chest_bounds"]
            and route["source"]["position"] == row["steps"][0]["position"]
            and g["layout"] == f["route_layout"] and g["source_unit"] == f["chest_unit"]
            and g["target_unit"] == f["target_unit"] and g["method"] == f["method"]
            and all(g[k] == f[k] for k in ("first_tick", "last_tick", "last_positive_tick", "positive_samples"))
            and g["sent"] == f["mined"] and g["received"] == f["delivered_lower"]
            and g["unattributed_loss"] == 0)
    except (ValueError, KeyError, TypeError, AttributeError):
        return False
