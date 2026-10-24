"""Offline proof whether union loading checks solid/coal without output schema."""
from copy import deepcopy
from dataclasses import asdict
import json

from jev_factorio.acceptance_io import canonical
from jev_factorio.coal_controller import coal_loop_type
from jev_factorio.controller import HierarchicalLoop
from jev_factorio.memory import load_checkpoint_bytes
from jev_factorio.solid_controller import solid_loop_type

from test_cross_family_paid_identity import _coal_memory_with_shared_solid_endpoint


source, coal_chest = _coal_memory_with_shared_solid_endpoint()
memory_type = coal_loop_type(solid_loop_type(HierarchicalLoop)).memory_type
memory = memory_type(source.session_id, source.target, last_tick=source.last_tick)
for key in (
    "solid_routes_schema", "solid_science_policy", "solid_intents", "solid_epoch",
    "solid_commitments", "solid_funding", "solid_funding_catalogs",
    "coal_supply_schema", "coal_kit_policy", "coal_economic_admission", "coal_funding",
    "coal_targets", "coal_epoch", "coal_commitments",
):
    setattr(memory, key, deepcopy(getattr(source, key)))

solid_part = next(iter(memory.solid_commitments.values()))["parts"]["receive"]
solid_part["receipt"] = coal_chest["receipt"]
raw = canonical(asdict(memory))
load_checkpoint_bytes(raw, memory.session_id, memory.target)
print(json.dumps({
    "accepted": True,
    "output_buffers_schema": False,
    "families": ["solid", "coal"],
    "alias_dimension": "receipt",
    "solid_owner_receipt": solid_part["receipt"],
    "coal_owner_receipt": coal_chest["receipt"],
    "classification": "offline composed-loader producer-fixture evidence; no backend/native calls",
}, sort_keys=True))
