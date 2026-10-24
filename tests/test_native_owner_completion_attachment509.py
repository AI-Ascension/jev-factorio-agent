"""Source-bound read-only completion queries for installed native owners."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from lupa.lua52 import LuaRuntime, lua_type

from jev_factorio import solid_routes
from jev_factorio.backends.native_attachment import PROBE
from jev_factorio.backends.native_attachment import (
    NATIVE_SCHEMA, OPTIONAL_ASSETS, PINNED_ASSETS, _asset_source,
    connector_ownership_sha256, prepare_install_command,
)
from jev_factorio.backends.native_owner_completion_attachment import (
    observe_current_owner_snapshot,
)
from jev_factorio.backends.native_current_attachment import DIRECT_MODULE_PROFILES
from test_native_optional_profiles import _case, _install


def _json_value(value):
    """Mirror the captured Factorio JSON contract: an empty Lua table is an object."""
    if lua_type(value) != "table":
        return value
    pairs = list(value.items())
    if not pairs:
        return {}
    if all(type(key) is int and key >= 1 for key, _ in pairs):
        ordered = sorted(pairs)
        if [key for key, _ in ordered] == list(range(1, len(ordered) + 1)):
            return [_json_value(item) for _, item in ordered]
    return {key: _json_value(item) for key, item in pairs}


def _make_native_json_faithful(lua):
    lua.globals().helpers.table_to_json = lambda value: json.dumps(
        _json_value(value), sort_keys=True, separators=(",", ":"))


class _LuaRconClient:
    def __init__(self, lua, output):
        self.lua = lua
        self.output = output
        self.commands = []

    def send_command(self, command):
        self.commands.append(command)
        self.output.clear()
        assert command.startswith("/sc ")
        self.lua.execute(command.removeprefix("/sc "))
        assert len(self.output) == 1
        return self.output.pop()


class _CompletionProfileClient:
    def __init__(self, lua, output):
        self.lua = lua
        self.output = output
        self.commands = []

    def send_command(self, command):
        self.commands.append(command)
        self.output.clear()
        assert command.startswith("/sc ")
        self.lua.execute(command.removeprefix("/sc "))
        assert len(self.output) == 1
        return self.output.pop()


def _solid_state(lua):
    return json.loads(lua.eval("helpers.table_to_json((function() "
                    "local s=jev_fle_runtime.solid_routes;local actor=jev_fle_runtime.agent_characters[1];"
                    "local entities={};for role,e in pairs(jev_fle_runtime.campaign.entities) do "
                    "entities[role]={valid=e.valid,unit_number=e.unit_number,name=e.name,position=e.position} end;"
                    "return {cells=s.cells,offers=s.offers,intents=s.intents,binding=s.binding,"
                    "cache=s.completion_diagnostics,entities=entities,actor_unit=actor.unit_number,"
                    "actor_index=actor.surface.index,force_index=actor.force.index,tick=game.tick} end)())"))


def _without_capture_clock(value):
    if isinstance(value, dict):
        return {key: _without_capture_clock(item) for key, item in value.items()
                if key not in {"tick", "diagnostics_tick", "observed_tick", "next_survey_tick",
                               "last_tick", "last_positive_tick", "cached"}}
    if isinstance(value, list):
        return [_without_capture_clock(item) for item in value]
    return value


def _observe_solid(lua):
    result = lua.eval("jev_fle_runtime.campaign.observe()")
    return _json_value(result["solid_routes"])


class _AdvanceOneTickBetweenCompletionCaptures:
    """Keep state fixed while Factorio's ordinary simulation clock advances."""

    def __init__(self, client, lua, *, advance_on_capture=2):
        self.client = client
        self.lua = lua
        self.advance_on_capture = advance_on_capture
        self.capture_count = 0

    def send_command(self, command):
        if "expected_modules=helpers.json_to_table" in command:
            self.capture_count += 1
            if self.capture_count == self.advance_on_capture:
                self.lua.execute("game.tick=game.tick+1")
        return self.client.send_command(command)


def _advance_paid_coal_world(lua, *, tick_delta=33):
    """Advance the offline engine-shaped world, without calling an observer.

    One mined unit is reflected in the retained resource and delivered to its
    paid target while a small amount of already-burning fuel is consumed. This
    is deterministic fixture progression, not Factorio physics; it does not
    invoke a controller action or mutate owner receipt tables.
    """
    lua.execute(f"game.tick=game.tick+{tick_delta}; " + r"""
      for target,row in pairs(storage.coal_supply.rows) do
        local ore=assert(row.resources[1]);assert(ore.amount>0);ore.amount=ore.amount-1
        local entity=assert(storage.campaign.entities[row.target.role])
        local fuel=entity.get_fuel_inventory();fuel.values.coal=(fuel.values.coal or 0)+1
        assert(entity.burner.remaining_burning_fuel>=100000)
        entity.burner.remaining_burning_fuel=entity.burner.remaining_burning_fuel-100000
      end
    """)


def _paid_coal_world_state(lua):
    return json.loads(lua.eval(r"""helpers.table_to_json((function()
      local result={tick=game.tick,ore={},fuel={}}
      for target,row in pairs(storage.coal_supply.rows) do
        result.ore[target]=row.resources[1].amount
        result.fuel[target]=storage.campaign.entities[row.target.role].get_fuel_inventory().values.coal or 0
      end
      return result
    end)())"""))


class _AdvancePaidCoalBetweenCompletionCaptures:
    """Model natural speed-1 mining/delivery while completion reads are sent."""

    def __init__(self, client, lua, *, progress_intervals=(1, 2, 3)):
        self.client, self.lua = client, lua
        self.progress_intervals = set(progress_intervals)
        self.capture_count = 0

    def send_command(self, command):
        if "expected_modules=helpers.json_to_table" in command:
            self.capture_count += 1
            if self.capture_count > 1:
                if self.capture_count - 1 in self.progress_intervals:
                    _advance_paid_coal_world(self.lua)
                else:
                    self.lua.execute("game.tick=game.tick+33")
        return self.client.send_command(command)


def _source_generated_paid_coal_query_client():
    """Build a pinned direct profile with paid solid/coal owners from real Lua producers.

    Factorio APIs and placement are deterministic offline doubles; the installed
    FLE producer, projection, attachment, and Python parser are repository code.
    """
    from jev_factorio.backends.native_attachment import PROBE
    from jev_factorio.backends.native_current_attachment import DIRECT_MODULE_PROFILES
    from test_native_optional_profiles import _asset_text as optional_profile_asset

    lua = LuaRuntime(unpack_returned_tuples=True)
    fixture_root = Path(__file__).parent
    for relative in ("fixtures/solid_routes_runtime.lua",
                     "fixtures/coal_supply_runtime.lua"):
        lua.execute((fixture_root / relative).read_text(encoding="utf-8"))
    lua.execute(r"""
        jev_fle_runtime=storage
        jev_fle_runtime.jev_bound_player_index=1
        game.get_player=function(index) if index==1 then return player end end
        local handlers, nth = {}, {}
        script={active_mods={base="2.0.77",core="2.0.77"}}
        function script.on_event(event,handler) handlers[event]=handler end
        function script.get_event_handler(event) return handlers[event] end
        function script.on_nth_tick(tick,handler) nth[tick]=handler end
        defines.events={on_tick=1,on_script_path_request_finished=2,on_player_mined_entity=3,
          on_pre_player_crafted_item=4,on_player_cancelled_crafting=5,on_player_crafted_item=6}
        defines.controllers={character=1,god=2,editor=3}
        defines.direction={north=0,east=4,south=8,west=12,northeast=2,southeast=6,southwest=10,northwest=14}
        defines.inventory.fuel=3;defines.inventory.assembling_machine_input=2
        defines.inventory.assembling_machine_output=4;defines.inventory.chest=1
        defines.inventory.furnace_source=5;defines.inventory.furnace_result=6
        defines.inventory.lab_input=7;defines.rocket_silo_status={rocket_ready=1};defines.entity_status={}
        game.speed=1;game.tick_paused=false
        force.rockets_launched=0;force.technologies={}
        function force.get_item_production_statistics()
          return {get_input_count=function() return 0 end,get_output_count=function() return 0 end}
        end
        prototypes.item.coal={fuel_value=4000000}
        local raw_inv=inv
        inv=function(values)
          local value=raw_inv(values)
          function value.get_contents()
            local rows={}
            for name,count in pairs(values) do if count>0 then rows[#rows+1]={name=name,count=count,quality={name="normal"}} end end
            return rows
          end
          function value.find_item_stack(name)
            local count=values[name] or 0
            if count<=0 then return nil end
            return {valid_for_read=true,name=name,count=count,quality={name="normal"}}
          end
          return value
        end
        local function enrich(e)
          e.input=inv(e.input.values);e.output=inv(e.output.values);e.fuel=inv(e.fuel.values)
          local lines={};for i,line in ipairs(e.lines or {}) do lines[i]=inv(line.values) end;e.lines=lines
          e.get_inventory=function(kind)
            if kind==defines.inventory.chest then return e.output end
            if kind==defines.inventory.fuel then return e.fuel end
            if kind==defines.inventory.assembling_machine_output then return e.output end
            return e.input
          end
          e.fluidbox=setmetatable({},{__len=function() return 0 end})
          e.fluidbox.get_filter=function() return nil end
          e.fluidbox.get_fluid_segment_id=function() return nil end
          e.fluidbox.get_pipe_connections=function() return {} end
          return e
        end
        for _,e in ipairs(all_entities) do enrich(e) end
        local raw_entity=entity
        entity=function(... ) return enrich(raw_entity(...)) end
        player.position={x=0,y=0};player.build_distance=64;player.cheat_mode=false
        player.walking_state={walking=false};player.mining_state={mining=false}
        player.get_item_count=function(name) return stock[name] or 0 end
        player.cursor_stack={valid_for_read=false}
        player.cursor_stack.transfer_stack=function(stack)
          if not stack or not stack.valid_for_read then return false end
          player.cursor_stack.valid_for_read=true;player.cursor_stack.name=stack.name
          player.cursor_stack.count=stack.count;return true
        end
        player.clear_cursor=function()
          player.cursor_stack.valid_for_read=false;player.cursor_stack.name=nil;player.cursor_stack.count=0
          return true
        end
        player.can_build_from_cursor=function(spec)
          return player.cursor_stack.valid_for_read
            and surface.can_place_entity{name=player.cursor_stack.name,position=spec.position,direction=spec.direction}
        end
        player.build_from_cursor=function(spec)
          local name=player.cursor_stack.name;assert(name and (stock[name] or 0)>0)
          stock[name]=stock[name]-1
          local kind=name=="wooden-chest" and "container" or name=="electric-mining-drill" and "mining-drill"
            or name=="inserter" and "inserter" or "transport-belt"
          local half=name=="wooden-chest" and .35 or name=="electric-mining-drill" and 1.49 or .4
          local e=entity(name,kind,spec.position.x,spec.position.y,half);e.direction=spec.direction
          if kind=="mining-drill" then e.mining_area={left_top={x=e.position.x-2.49,y=e.position.y-2.49},
            right_bottom={x=e.position.x+2.49,y=e.position.y+2.49}} end
          update_topology();player.cursor_stack.valid_for_read=false
        end
    """)

    # Install the exact direct source profile used by ordinary attachment.
    lua.globals().helpers.json_to_table = lambda raw: lua.table_from(
        json.loads(raw), recursive=True)
    for name in ("fair_actions", "factory", "connector_ownership", "launch_readiness",
                 "solid_routes", "coal_supply"):
        try:
            source = optional_profile_asset(name)
            prepared = prepare_install_command(source)
            if prepared == source:
                lua.execute(source)
            else:
                prefix, receipt = prepared.split("\nlocal rt=assert(jev_fle_runtime); ", 1)
                lua.execute(prefix)
                lua.execute("local rt=assert(jev_fle_runtime); " + receipt.replace("; ", ";\n"))
        except Exception as exc:
            raise AssertionError(f"offline source installation failed for {name}") from exc
    lua.execute("burner1.burner.remaining_burning_fuel=1000000;"
                "burner2.burner.remaining_burning_fuel=1000000;"
                "jev_fle_runtime.fair.bind();configure_coal();coal_all();"
                "coal_pulse();coal_pulse();coal_pulse()")
    _make_native_json_faithful(lua)
    output = []
    lua.globals().rcon.print = output.append
    lua.globals().helpers.json_to_table = lambda raw: lua.table_from(json.loads(raw), recursive=True)

    profile = "solid_and_coal"
    module_names = set(PINNED_ASSETS) | {"connector_ownership"} | set(OPTIONAL_ASSETS)
    modules = {name: name in DIRECT_MODULE_PROFILES[profile] for name in module_names}
    assets = {}
    for name, enabled in modules.items():
        if enabled:
            assets[name] = (connector_ownership_sha256() if name == "connector_ownership"
                            else hashlib.sha256(_asset_source(name).read_bytes()).hexdigest())
    session = str(lua.eval("jev_fle_runtime.jev_session_id"))
    actor = int(lua.eval("jev_fle_runtime.agent_characters[1].unit_number"))
    manifest = {"schema": NATIVE_SCHEMA, "session_id": session, "actor_unit": actor,
                "assets": assets, "profile": False}
    lua.globals().manifest_json = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    lua.execute(r"""
      local rt=jev_fle_runtime;local c=rt.campaign;local f=rt.fair
      local n=helpers.json_to_table(manifest_json)
      n.callbacks={fair_tick=f.tick_handler,observe=c.observe,snapshot_v1=c.observation_snapshot,
        snapshot_v2=c.observation_snapshot_v2,transfer=c.transfer,configure=c.configure,
        connector_begin=c.connector_begin,connector_finish=c.connector_finish,
        connector_page=c.connector_page,connector_observe=c.observe_connector_ownership,
        journal_tick=rt.coal_manual_journal_v1 and rt.coal_manual_journal_v1.tick_handler,
        cycle_tick=rt.coal_manual_cycle_v2 and rt.coal_manual_cycle_v2.combined_tick_handler,
        bootstrap_observe=rt.bootstrap_output_v1 and rt.bootstrap_output_v1.observe,
        bootstrap_extract=rt.bootstrap_output_v1 and rt.bootstrap_output_v1.extract,
        bootstrap_place=rt.bootstrap_output_v1 and rt.bootstrap_output_v1.place,
        bootstrap_bind_paid=rt.bootstrap_output_v1 and rt.bootstrap_output_v1.bind_paid,
        bootstrap_reconcile_pending=rt.bootstrap_output_v1 and rt.bootstrap_output_v1.reconcile_pending,
        bootstrap_complete_install=rt.bootstrap_output_v1 and rt.bootstrap_output_v1.complete_install}
      rt.native_installation=n
    """)
    attachment = {
        "schema": 1, "qualified": True, "session_id": session, "actor_unit": actor,
        "modules": modules,
        "solid_intents": _json_value(lua.eval("helpers.table_to_json(jev_fle_runtime.solid_routes.intents)")),
        "coal_targets": _json_value(lua.eval("helpers.table_to_json(jev_fle_runtime.coal_supply.targets)")),
        "coal_admission_evidence": False,
        "connector_observer_bridge_qualified": False,
        "connector_snapshot_qualified": False, "connector_snapshot_tick": 0,
        "connector_snapshot_ownership": False,
        "native_installation": manifest,
    }
    return lua, output, attachment, _LuaRconClient(lua, output), profile


def _paid_solid_coal_storage_state(lua):
    return json.loads(lua.eval(r"""helpers.table_to_json((function()
      local c=jev_fle_runtime.solid_routes;local q=jev_fle_runtime.coal_supply
      local function parts(values)
        local result={}
        for name,value in pairs(values or {}) do
          result[name]={role=value.role,unit_number=value.unit_number,receipt=value.receipt,paid=value.paid}
        end
        return result
      end
      local function cells(values)
        local result={}
        for name,value in pairs(values or {}) do
          local flow=value.flow or {};local previous=value.previous or {}
          result[name]={route=value.route,layout=value.layout,item=value.item,
            source=value.source and value.source.role or false,target=value.target and value.target.role or false,
            parts=parts(value.parts),pending=value.pending and {part=value.pending.part,
              receipt=value.pending.receipt,phase=value.pending.phase} or {},fault=value.fault or false,
            reason=value.reason or false,flow={layout=flow.layout,source_unit=flow.source_unit,
              target_unit=flow.target_unit,method=flow.method,first_tick=flow.first_tick,last_tick=flow.last_tick,
              last_positive_tick=flow.last_positive_tick,positive_samples=flow.positive_samples,
              sent=flow.sent,received=flow.received,unattributed_loss=flow.unattributed_loss},
            previous={tick=previous.tick,available=previous.available,produced=previous.produced,
              pipe=previous.pipe,target_stock=previous.target_stock,consumed=previous.consumed},
            positive=value.positive,received=value.received,sent=value.sent,
            last_positive_tick=value.last_positive_tick}
        end
        return result
      end
      local rows={}
      for target,row in pairs(q.rows or {}) do
        local flow=row.flow or {}
      rows[target]={layout=row.layout,parts=parts(row.parts),remaining=row.remaining,
          pending=row.pending and {part=row.pending.part,receipt=row.pending.receipt,phase=row.pending.phase} or {},
          manual_pending=row.manual_pending and {receipt=row.manual_pending.receipt,
            quantity=row.manual_pending.quantity,phase=row.manual_pending.phase,
            unit_number=row.manual_pending.unit_number} or {},manual_receipts=row.manual_receipts,
          manual_total=row.manual_total,delivered=row.delivered,positive=row.positive,
          last_positive=row.last_positive,fault=row.fault or false,reason=row.reason or false,
          flow={layout=flow.layout,route_layout=flow.route_layout,method=flow.method,
            first_tick=flow.first_tick,last_tick=flow.last_tick,last_positive_tick=flow.last_positive_tick,
            positive_samples=flow.positive_samples,mined=flow.mined,delivered_lower=flow.delivered_lower,
            manual_inserted=flow.manual_inserted,drill_unit=flow.drill_unit,chest_unit=flow.chest_unit,
            target_unit=flow.target_unit,burned_lower_joules=flow.burned_lower_joules,
            initial_fuel_joules=flow.initial_fuel_joules,target_fuel_joules=flow.target_fuel_joules,
            fuel_value_joules=flow.fuel_value_joules,in_transit_uncertainty=flow.in_transit_uncertainty,
            self_fuel=flow.self_fuel}}
      end
      return {tick=game.tick,stock=stock,actor_unit=jev_fle_runtime.agent_characters[1].unit_number,
        solid_cells=cells(c.cells),solid_offers=cells(c.offers),solid_intents=c.intents,
        solid_diagnostics=c.completion_diagnostics,coal_committed=q.committed,
        coal_targets=q.targets,coal_rows=rows,coal_receipts=jev_fle_runtime.campaign.receipts}
    end)())"""))


class _AttachedLuaClient:
    def __init__(self, lua, output, attachment):
        self.lua, self.output, self.attachment = lua, output, attachment
        self.commands = []

    def send_command(self, command):
        self.commands.append(command)
        if command == "/sc " + PROBE:
            return json.dumps(self.attachment, sort_keys=True, separators=(",", ":"))
        self.output.clear()
        assert command.startswith("/sc ")
        self.lua.execute(command.removeprefix("/sc "))
        assert len(self.output) == 1
        return self.output.pop()


def _completion_identity_state(lua):
    return _json_value(lua.eval("helpers.table_to_json((function() "
        "local c=jev_fle_runtime.campaign;local p=game.get_player(1);"
        "local entities={};for role,e in pairs(c.entities) do entities[role]={"
        "valid=e.valid,unit_number=e.unit_number,name=e.name,position=e.position} end;"
        "return {session=jev_fle_runtime.jev_session_id,actor=p.character.unit_number,"
        "surface=p.surface.index,force=p.force.index,entities=entities} end)())"))


def test_actual_solid_diagnostic_is_reused_without_observer_or_storage_mutation():
    lua, output = _case(solid=True)
    _make_native_json_faithful(lua)

    emitted = _observe_solid(lua)
    assert emitted["session_id"] == "offline-optional-profile-session"
    assert emitted["diagnostics"]
    assert emitted["diagnostics"][0]["state"] == "unavailable"
    assert emitted["diagnostics"][0]["reason"] in solid_routes.DIAGNOSTIC_REASONS
    before = _solid_state(lua)

    client = _LuaRconClient(lua, output)
    advancing = _AdvanceOneTickBetweenCompletionCaptures(client, lua)
    snapshot, details = observe_current_owner_snapshot(advancing)

    assert details == {}
    assert snapshot.factory["solid_routes"]["diagnostics"] == emitted["diagnostics"]
    assert solid_routes.completion_routes(snapshot) == {}
    with pytest.raises(ValueError, match="Missing or stale solid-route snapshot"):
        solid_routes.routes(snapshot)
    assert len(client.commands) == 4  # metadata, two identical data queries, final metadata
    query_commands = client.commands[1:3]
    assert query_commands[0] == query_commands[1]
    assert all("campaign.observe()" not in command and "c.observe()" not in command
               for command in query_commands)
    assert _solid_state(lua) == {**before, "tick": before["tick"] + 1}


@pytest.mark.parametrize("mutation", ["missing", "expired", "future", "endpoint_drift", "intent_rebind"])
def test_source_diagnostic_cache_rejects_loss_or_identity_drift_without_mutation(mutation):
    lua, output = _case(solid=True)
    _make_native_json_faithful(lua)
    _observe_solid(lua)
    if mutation == "missing":
        lua.execute("jev_fle_runtime.solid_routes.completion_diagnostics=nil")
    elif mutation == "expired":
        lua.execute("jev_fle_runtime.solid_routes.completion_diagnostics.tick=game.tick-601")
    elif mutation == "future":
        lua.execute("jev_fle_runtime.solid_routes.completion_diagnostics.tick=game.tick+1")
    elif mutation == "endpoint_drift":
        lua.execute("jev_fle_runtime.campaign.entities['recipe:iron-plate']="
                    "{valid=true,unit_number=9071}")
    else:
        lua.execute("jev_fle_runtime.campaign.set_solid_intents(" 
                    "jev_fle_runtime.solid_routes.intents)")
    before = _solid_state(lua)

    client = _LuaRconClient(lua, output)
    with pytest.raises(Exception):
        observe_current_owner_snapshot(client)

    assert _solid_state(lua) == before
    assert len(client.commands) >= 2


def test_current_solid_diagnostic_survives_one_ordinary_tick_without_owner_changes():
    lua, output = _case(solid=True)
    _make_native_json_faithful(lua)
    emitted = _observe_solid(lua)
    before = _solid_state(lua)
    client = _CompletionProfileClient(lua, output)

    snapshot, details = observe_current_owner_snapshot(
        _AdvanceOneTickBetweenCompletionCaptures(client, lua))

    assert details == {}
    assert snapshot.factory["solid_routes"]["diagnostics"] == emitted["diagnostics"]
    assert snapshot.tick == before["tick"] + 1
    assert snapshot.factory["solid_routes"]["diagnostics_tick"] == before["tick"]
    assert _solid_state(lua) == {**before, "tick": before["tick"] + 1}


def test_default_completion_capture_allows_one_tick_between_stable_reads():
    lua, output = _case()
    _make_native_json_faithful(lua)
    before = _completion_identity_state(lua)
    client = _CompletionProfileClient(lua, output)
    advancing = _AdvanceOneTickBetweenCompletionCaptures(client, lua)

    snapshot, details = observe_current_owner_snapshot(advancing)

    assert details == {}
    assert snapshot.tick == 1235
    assert advancing.capture_count == 2
    assert _completion_identity_state(lua) == before


def test_public_owner_preflight_accepts_monotonic_tick_after_initial_identity_probe(tmp_path):
    from jev_factorio.owner_completion import preflight_completed_owner

    lua, client, snapshot, _loaded, checkpoint = _source_generated_successor_checkpoint(tmp_path)
    from jev_factorio.provenance import source_revision

    owner_path = tmp_path / "continuous_owner.py"
    owner_path.write_text("# offline owner contract fixture\n", encoding="utf-8")
    owner_hash = hashlib.sha256(owner_path.read_bytes()).hexdigest()
    source_root = Path(__file__).resolve().parents[1]
    source_pin = source_revision(source_root)
    assert source_pin is not None
    native_identity = {
        "tick": snapshot.tick, "speed": 1, "marked": True, "runtime": True,
        "session_id": snapshot.session_id, "connected": 1,
    }
    advancing = _AdvanceOneTickBetweenCompletionCaptures(
        client, lua, advance_on_capture=1)

    witness = preflight_completed_owner(
        checkpoint, target="rocket_launch", source_root=source_root,
        expected_source_revision=source_pin, startup_source_revision=source_pin,
        owner_path=owner_path, expected_owner_sha256=owner_hash,
        startup_owner_sha256=owner_hash, native_identity=native_identity,
        client=advancing)

    assert witness.observation_sha256
    assert witness.observed_tick == native_identity["tick"]
    assert int(lua.eval("game.tick")) == snapshot.tick + 1


_PROFILE_CASES = [
    pytest.param("default", {}, False, id="default"),
    pytest.param("observation_only", {"observations": True}, False, id="observation-only"),
    pytest.param("buffers_without_background", {"buffers": True}, False, id="buffers"),
    pytest.param("buffers_without_background_with_observations",
                 {"buffers": True, "observations": True}, False, id="buffers-observed"),
    pytest.param("background_work_with_buffers", {"buffers": True, "craft": True}, False,
                 id="background-buffers"),
    pytest.param("background_work_with_buffers_and_observations",
                 {"buffers": True, "craft": True, "observations": True}, False,
                 id="background-buffers-observed"),
    pytest.param("solid_only", {"solid": True}, False, id="solid"),
    pytest.param("solid_only_with_observations", {"solid": True, "observations": True}, False,
                 id="solid-observed"),
    pytest.param("solid_and_coal", {"solid": True, "coal": True}, False, id="solid-coal"),
    pytest.param("solid_and_coal_with_observations",
                 {"solid": True, "coal": True, "observations": True}, False,
                 id="solid-coal-observed"),
    pytest.param("current_full", {"observations": True, "craft": True, "buffers": True,
                                   "inputs": True, "outposts": True}, False, id="current-full"),
    pytest.param("solid_and_coal_with_prerequisites",
                 {"observations": True, "craft": True, "buffers": True, "inputs": True,
                  "outposts": True, "solid": True, "coal": True}, False,
                 id="full-solid-coal"),
    pytest.param("current_successors", {"observations": True, "craft": True,
                                          "buffers": True, "inputs": True}, True,
                 id="successors"),
    pytest.param("successors_with_solid", {"observations": True, "craft": True,
                                            "buffers": True, "inputs": True, "solid": True}, True,
                 id="successors-solid"),
    pytest.param("successors_with_solid_and_coal",
                 {"observations": True, "craft": True, "buffers": True, "inputs": True,
                  "solid": True, "coal": True}, True, id="successors-solid-coal"),
]


@pytest.mark.parametrize("profile,case,successors", _PROFILE_CASES)
def test_every_source_reviewed_composition_runs_read_only_actual_producer_query(
        profile, case, successors):
    assert profile in DIRECT_MODULE_PROFILES
    lua, output = _case(**case)
    _make_native_json_faithful(lua)
    if case.get("coal"):
        # Coal accounting's real public producer requires the one-player list
        # that Factorio supplies; the shared optional-profile harness omits it.
        lua.execute("game.connected_players={game.get_player(1)}")
    if successors:
        _install(lua, "successors")
    emitted = _json_value(lua.eval("jev_fle_runtime.campaign.observe()"))
    initial_tick = lua.eval("game.tick")
    client = _CompletionProfileClient(lua, output)
    advancing = _AdvanceOneTickBetweenCompletionCaptures(client, lua)

    snapshot, details = observe_current_owner_snapshot(advancing)

    assert details == {}
    assert snapshot.tick == initial_tick + 1
    assert advancing.capture_count == 2
    assert len(client.commands) == 4  # metadata, two direct captures, final metadata
    for name in ("output_buffers", "input_routes", "production_sites", "mining_outposts",
                 "successors", "solid_routes", "coal_supply"):
        if name in emitted:
            assert _without_capture_clock(snapshot.factory[name]) == _without_capture_clock(emitted[name]), (profile, name)
        else:
            assert name not in snapshot.factory, (profile, name)
    if successors:
        assert snapshot.factory["successors"]["sources"] == {}
        assert snapshot.factory["production_sites"]["sources"]


def test_paid_solid_corridors_and_coal_sources_use_pinned_public_completion_query():
    from jev_factorio import coal_supply

    lua, output, attachment, client, profile = _source_generated_paid_coal_query_client()
    before = _paid_solid_coal_storage_state(lua)
    assert profile == "solid_and_coal"
    assert attachment["modules"]["solid_routes"] and attachment["modules"]["coal_supply"]

    advancing = _AdvancePaidCoalBetweenCompletionCaptures(client, lua)
    world_before = _paid_coal_world_state(lua)
    snapshot, details = observe_current_owner_snapshot(advancing)

    solid_rows = solid_routes.completion_routes(snapshot)
    coal_rows = coal_supply.completion_sources(snapshot)
    assert details == {}
    assert solid_rows and all(row["parts"] and row["flow"] for row in solid_rows.values())
    assert coal_rows and len(coal_rows) == 2
    assert all(row["parts"] and row["flow"] for row in coal_rows.values())
    assert all(solid_routes.completion_flow_complete(route, row["layout"], snapshot)
               for route, row in solid_rows.items())
    assert coal_supply.completion_flow_complete(snapshot)
    assert snapshot.tick - before["tick"] == 132
    assert advancing.capture_count == 5
    assert len(client.commands) == 7  # metadata, five current reads, final metadata
    assert all(route["flow"]["positive_samples"] == 3
               and route["flow"]["sent"] == 3 and route["flow"]["received"] == 3
               for route in solid_rows.values())
    assert all(row["flow"]["positive_samples"] == 3
               and row["flow"]["mined"] == 3 and row["flow"]["delivered_lower"] == 3
               and row["flow"]["burned_lower_joules"] > 0
               for row in coal_rows.values())
    assert all(route["flow"]["first_tick"] == before["tick"]
               and route["flow"]["last_tick"] == snapshot.tick
               and route["flow"]["last_tick"] - route["flow"]["first_tick"] == 132
               for route in solid_rows.values())
    assert all(row["flow"]["first_tick"] == before["tick"]
               and row["flow"]["last_tick"] == snapshot.tick
               and row["flow"]["last_tick"] - row["flow"]["first_tick"] == 132
               for row in coal_rows.values())
    assert all(route["flow"]["last_positive_tick"] == snapshot.tick - 33
               for route in solid_rows.values())
    assert all(row["flow"]["last_positive_tick"] == snapshot.tick - 33
               for row in coal_rows.values())
    after = _paid_solid_coal_storage_state(lua)
    assert after["tick"] == snapshot.tick
    assert {**before, "tick": after["tick"]} == after
    world_after = _paid_coal_world_state(lua)
    assert world_after["tick"] == snapshot.tick
    assert {target: world_before["ore"][target] - 3 for target in world_before["ore"]} == world_after["ore"]
    assert {target: world_before["fuel"][target] + 3 for target in world_before["fuel"]} == world_after["fuel"]

    # The old native flow already contains many positive samples, but this
    # public query observes only one new delivery. It must not revive those
    # historical counts into a current completion proof.
    retained = _paid_solid_coal_storage_state(lua)
    assert all(row["flow"]["positive_samples"] >= 3
               for row in retained["coal_rows"].values())
    state_before_one = _paid_solid_coal_storage_state(lua)
    world_before_one = _paid_coal_world_state(lua)
    commands_before_one = len(client.commands)
    one_positive, one_positive_details = observe_current_owner_snapshot(
        _AdvancePaidCoalBetweenCompletionCaptures(client, lua, progress_intervals=(1,)))
    assert one_positive_details == {}
    assert len(client.commands) - commands_before_one == 7
    assert all(not row["flow"] for row in solid_routes.completion_routes(one_positive).values())
    assert all(not row["flow"] for row in coal_supply.completion_sources(one_positive).values())
    assert not coal_supply.completion_flow_complete(one_positive)
    assert all(not solid_routes.completion_flow_complete(route, row["layout"], one_positive)
               for route, row in solid_routes.completion_routes(one_positive).items())
    state_after_one = _paid_solid_coal_storage_state(lua)
    assert {**state_before_one, "tick": state_after_one["tick"]} == state_after_one
    world_after_one = _paid_coal_world_state(lua)
    assert {target: world_before_one["ore"][target] - 1
            for target in world_before_one["ore"]} == world_after_one["ore"]
    assert {target: world_before_one["fuel"][target] + 1
            for target in world_before_one["fuel"]} == world_after_one["fuel"]


def test_old_flow_totals_do_not_substitute_for_three_current_positive_samples():
    """A prior complete flow plus one new positive sample is still incomplete."""
    from jev_factorio import coal_supply

    lua, _output, _attachment, client, _profile = _source_generated_paid_coal_query_client()
    retained = _paid_solid_coal_storage_state(lua)
    assert all(row["flow"]["positive_samples"] >= 3
               for row in retained["coal_rows"].values())
    assert all(row["flow"]["positive_samples"] >= 3
               for row in retained["solid_cells"].values())

    snapshot, details = observe_current_owner_snapshot(
        _AdvancePaidCoalBetweenCompletionCaptures(client, lua, progress_intervals=(1,)))

    assert details == {}
    assert all(not row["flow"] for row in solid_routes.completion_routes(snapshot).values())
    assert all(not row["flow"] for row in coal_supply.completion_sources(snapshot).values())
    assert not any(solid_routes.completion_flow_complete(route, row["layout"], snapshot)
                   for route, row in solid_routes.completion_routes(snapshot).items())
    assert not coal_supply.completion_flow_complete(snapshot)


def test_paid_solid_coal_completion_fails_closed_after_normal_thirty_second_owner_wait():
    """Old cumulative flow cannot be refreshed when no current interval gains."""
    from jev_factorio import coal_supply

    lua, _output, _attachment, client, _profile = _source_generated_paid_coal_query_client()
    before = _paid_solid_coal_storage_state(lua)
    world_before = _paid_coal_world_state(lua)
    # There was a genuine early positive, but it is now outside the 600-tick
    # evidence window after the owner's normal 30-second child-exit wait.
    _advance_paid_coal_world(lua, tick_delta=1)
    lua.execute("game.tick=game.tick+1800")
    advancing = _AdvancePaidCoalBetweenCompletionCaptures(client, lua, progress_intervals=())
    snapshot, details = observe_current_owner_snapshot(advancing)
    assert details == {}
    assert advancing.capture_count == 5
    assert len(client.commands) == 7
    solid_rows = solid_routes.completion_routes(snapshot)
    coal_rows = coal_supply.completion_sources(snapshot)
    assert all(not row["flow"] for row in solid_rows.values())
    assert all(not row["flow"] for row in coal_rows.values())
    assert not any(solid_routes.completion_flow_complete(route, row["layout"], snapshot)
                   for route, row in solid_rows.items())
    assert not coal_supply.completion_flow_complete(snapshot)

    after = _paid_solid_coal_storage_state(lua)
    assert after["tick"] == before["tick"] + 1 + 1800 + 132
    assert {**before, "tick": after["tick"]} == after
    world_after = _paid_coal_world_state(lua)
    assert world_after["ore"] == {target: amount - 1 for target, amount in world_before["ore"].items()}
    assert world_after["fuel"] == {target: amount + 1 for target, amount in world_before["fuel"].items()}


def test_public_completed_owner_preflight_accepts_fresh_paid_solid_coal_after_thirty_second_wait(
        tmp_path):
    """A fresh native interval, not the old diagnostic cache, proves current paid flow."""
    from jev_factorio import coal_supply
    from jev_factorio.coal_controller import coal_loop_type
    from jev_factorio.controller import HierarchicalLoop
    from jev_factorio.owner_completion import preflight_completed_owner
    from jev_factorio.provenance import source_revision
    from jev_factorio.solid_controller import solid_loop_type

    lua, output, attachment, client, _profile = _source_generated_paid_coal_query_client()
    # First bind the exact source-generated paid owners into a composed typed
    # checkpoint. This initial query predates the simulated ordinary owner wait.
    initial_client = _AdvancePaidCoalBetweenCompletionCaptures(client, lua)
    initial, details = observe_current_owner_snapshot(initial_client)
    assert details == {}
    solid_rows = solid_routes.completion_routes(initial)
    coal_rows = coal_supply.completion_sources(initial)
    assert len(solid_rows) == 2 and len(coal_rows) == 2
    assert all(row["parts"] and row["flow"] for row in solid_rows.values())
    assert all(row["parts"] and row["flow"] for row in coal_rows.values())

    epoch = {key: initial.factory["solid_routes"][key]
             for key in ("actor_index", "surface_index", "force_index")}
    memory_type = coal_loop_type(solid_loop_type(HierarchicalLoop)).memory_type
    memory = memory_type(
        session_id=initial.session_id,
        target="rocket_launch",
        last_tick=initial.tick,
        status="completed",
        completed_goals={"rocket_launch": initial.tick},
        history=[{"kind": "goal_completed", "goal": "rocket_launch", "tick": initial.tick}],
        solid_intents=json.loads(attachment["solid_intents"]),
        solid_epoch=epoch,
        solid_commitments={key: solid_routes.commitment(row)
                           for key, row in solid_rows.items()},
        coal_targets=json.loads(attachment["coal_targets"]),
        coal_epoch=epoch,
        coal_commitments={key: coal_supply.commitment(row)
                          for key, row in coal_rows.items()},
    )
    checkpoint = tmp_path / "completed-paid-solid-coal.json"
    memory.save(checkpoint)

    # The owner's real 30-second post-child delay is represented by advancing
    # the offline engine tick only. The native query then samples new progress
    # on both paid families without refreshing the old observer diagnostics.
    lua.execute("game.tick=game.tick+1800")
    source_root = Path(__file__).resolve().parents[1]
    source_pin = source_revision(source_root)
    assert source_pin is not None
    owner_path = tmp_path / "continuous_owner.py"
    owner_path.write_text("# offline owner contract fixture\n", encoding="utf-8")
    owner_hash = hashlib.sha256(owner_path.read_bytes()).hexdigest()
    native_identity = {
        "tick": int(lua.eval("game.tick")), "speed": 1, "marked": True,
        "runtime": True, "session_id": initial.session_id, "connected": 1,
    }
    before = _paid_solid_coal_storage_state(lua)
    world_before = _paid_coal_world_state(lua)
    advancing = _AdvancePaidCoalBetweenCompletionCaptures(client, lua)

    witness = preflight_completed_owner(
        checkpoint, target="rocket_launch", source_root=source_root,
        expected_source_revision=source_pin, startup_source_revision=source_pin,
        owner_path=owner_path, expected_owner_sha256=owner_hash,
        startup_owner_sha256=owner_hash, native_identity=native_identity, client=advancing)

    assert witness.observation_sha256 and witness.session_id == initial.session_id
    assert witness.checkpoint_tick == initial.tick
    assert witness.observed_tick == native_identity["tick"]
    assert advancing.capture_count == 5
    assert len(client.commands) == 15  # two 7-call captures plus one attachment readback
    assert int(lua.eval("game.tick")) == initial.tick + 1800 + 132
    after = _paid_solid_coal_storage_state(lua)
    assert {**before, "tick": after["tick"]} == after
    world_after = _paid_coal_world_state(lua)
    assert {target: world_before["ore"][target] - 3
            for target in world_before["ore"]} == world_after["ore"]
    assert {target: world_before["fuel"][target] + 3
            for target in world_before["fuel"]} == world_after["fuel"]

    native_identity["tick"] = int(lua.eval("game.tick"))
    checkpoint_bytes = checkpoint.read_bytes()
    one_positive_storage = _paid_solid_coal_storage_state(lua)
    one_positive_world = _paid_coal_world_state(lua)
    commands_before = len(client.commands)
    one_positive = _AdvancePaidCoalBetweenCompletionCaptures(
        client, lua, progress_intervals=(1,))
    with pytest.raises(ValueError, match="lacks current complete flow"):
        preflight_completed_owner(
            checkpoint, target="rocket_launch", source_root=source_root,
            expected_source_revision=source_pin, startup_source_revision=source_pin,
            owner_path=owner_path, expected_owner_sha256=owner_hash,
            startup_owner_sha256=owner_hash, native_identity=native_identity, client=one_positive)
    assert one_positive.capture_count == 5
    assert len(client.commands) - commands_before == 8
    assert checkpoint.read_bytes() == checkpoint_bytes
    assert {**one_positive_storage, "tick": int(lua.eval("game.tick"))} == _paid_solid_coal_storage_state(lua)
    one_positive_world_after = _paid_coal_world_state(lua)
    assert {target: one_positive_world["ore"][target] - 1
            for target in one_positive_world["ore"]} == one_positive_world_after["ore"]
    assert {target: one_positive_world["fuel"][target] + 1
            for target in one_positive_world["fuel"]} == one_positive_world_after["fuel"]

    # A second public preflight with only elapsed ticks must not reuse the
    # positive flow from the first query as if it were current evidence.
    settled_tick = int(lua.eval("game.tick"))
    native_identity["tick"] = settled_tick
    checkpoint_bytes = checkpoint.read_bytes()
    settled_storage = _paid_solid_coal_storage_state(lua)
    settled_world = _paid_coal_world_state(lua)
    commands_before = len(client.commands)
    stale = _AdvancePaidCoalBetweenCompletionCaptures(client, lua, progress_intervals=())
    with pytest.raises(ValueError, match="lacks current complete flow"):
        preflight_completed_owner(
            checkpoint, target="rocket_launch", source_root=source_root,
            expected_source_revision=source_pin, startup_source_revision=source_pin,
            owner_path=owner_path, expected_owner_sha256=owner_hash,
            startup_owner_sha256=owner_hash, native_identity=native_identity, client=stale)
    assert stale.capture_count == 5
    assert len(client.commands) - commands_before == 8
    assert checkpoint.read_bytes() == checkpoint_bytes
    assert {**settled_storage, "tick": int(lua.eval("game.tick"))} == _paid_solid_coal_storage_state(lua)
    stale_world = _paid_coal_world_state(lua)
    assert {**settled_world, "tick": stale_world["tick"]} == stale_world


def _qualified_successor_lua():
    """Run the real producer lifecycle against its existing offline Lua fixture."""
    import test_successors_lua

    lua = test_successors_lua.runtime.__wrapped__(SimpleNamespace())
    # Factorio character LuaEntities expose the same surface/force as their
    # connected player. The older shared fixture omitted those engine fields.
    lua.execute('surface=source.surface;surface.index=1;force.index=1;player.index=1;'
                'storage.agent_characters[1].surface=surface;'
                'storage.agent_characters[1].force=force')
    lua.execute('flowing_successor(); '
                'c.transfer(output.chest_role,"iron-plate",16,"trial",true); '
                'c.begin_craft_job("use:1","iron-gear-wheel",8); '
                'for i=1,8 do finish_one() end; c.observe(); produce(600); c.observe()')
    assert lua.eval("storage.successors.records['growth:iron-plate'].qualification ~= nil")
    assert lua.eval("storage.successors.records['growth:iron-plate'].use.job_id == 'use:1'")
    return lua


def _lua54_to_json_value(value):
    from lupa.lua54 import lua_type as lua54_type

    if lua54_type(value) != "table":
        return value
    pairs = list(value.items())
    if not pairs:
        return {}
    if all(type(key) is int and key >= 1 for key, _ in pairs):
        ordered = sorted(pairs)
        if [key for key, _ in ordered] == list(range(1, len(ordered) + 1)):
            return [_lua54_to_json_value(item) for _, item in ordered]
    return {key: _lua54_to_json_value(item) for key, item in pairs}


def _install_completion_probe_metadata(lua):
    """Bind the offline source fixture to the exact installed direct profile."""
    from jev_factorio.backends.native_current_attachment import DIRECT_MODULE_PROFILES

    profile = "current_successors"
    module_names = set(PINNED_ASSETS) | {"connector_ownership"} | set(OPTIONAL_ASSETS)
    modules = {name: name in DIRECT_MODULE_PROFILES[profile] for name in module_names}
    assets = {}
    for name, enabled in modules.items():
        if enabled:
            assets[name] = (connector_ownership_sha256() if name == "connector_ownership"
                            else hashlib.sha256(_asset_source(name).read_bytes()).hexdigest())
    session = "input-test"
    actor = int(lua.eval("storage.agent_characters[1].unit_number"))
    lua.execute("jev_fle_runtime=storage; storage.launch_readiness={schema=1}; "
                "storage.campaign.observation_snapshot=function() return {} end; "
                "storage.campaign.observation_snapshot_v2=function() return {} end; "
                "storage.campaign.connector_ledger={protocol=1,routes={}}; "
                "storage.campaign.observe_connector_ownership=function() "
                "return {protocol=1,session_id=storage.jev_session_id,tick=game.tick,routes={}} end")
    encoded_assets = json.dumps(assets, sort_keys=True, separators=(",", ":"))
    lua.globals().helpers.table_to_json = lambda value: json.dumps(
        _lua54_to_json_value(value), sort_keys=True, separators=(",", ":"))
    lua.globals().helpers.json_to_table = lambda raw: lua.table_from(
        json.loads(raw), recursive=True)
    probe = {
        "schema": 1,
        "qualified": True,
        "session_id": session,
        "actor_unit": actor,
        "modules": modules,
        "solid_intents": [],
        "coal_targets": [],
        "coal_admission_evidence": False,
        "connector_observer_bridge_qualified": False,
        "connector_snapshot_qualified": False,
        "connector_snapshot_tick": 0,
        "connector_snapshot_ownership": False,
        "native_installation": {
            "schema": NATIVE_SCHEMA,
            "session_id": session,
            "actor_unit": actor,
            "assets": assets,
            "profile": False,
        },
    }
    lua.globals().probe_json = json.dumps(probe, sort_keys=True, separators=(",", ":"))
    lua.execute("storage.native_installation={schema='" + NATIVE_SCHEMA + "',"
                "session_id=storage.jev_session_id,actor_unit=" + str(actor) + ","
                "assets=helpers.json_to_table(" + json.dumps(encoded_assets) + "),profile=false,"
                "callbacks={observe=storage.campaign.observe,"
                "fair_tick=storage.fair.tick_handler,"
                "connector_observe=storage.campaign.observe_connector_ownership}}")
    return probe


class _SuccessorCompletionClient:
    def __init__(self, lua, probe, output):
        self.lua, self.probe, self.output = lua, probe, output
        self.commands = []

    def send_command(self, command):
        self.commands.append(command)
        if command == "/sc " + PROBE:
            return json.dumps(self.probe, sort_keys=True, separators=(",", ":"))
        assert command.startswith("/sc ")
        self.output.clear()
        self.lua.execute(command.removeprefix("/sc "))
        assert len(self.output) == 1
        return self.output.pop()


def _source_generated_successor_checkpoint(tmp_path):
    from jev_factorio.state import GameSnapshot
    from test_successors import KIND
    from jev_factorio.successors import MAX_PROJECT_TICKS

    lua = _qualified_successor_lua()
    probe = _install_completion_probe_metadata(lua)
    output = []
    lua.globals().rcon.print = output.append
    client = _SuccessorCompletionClient(lua, probe, output)

    snapshot, details = observe_current_owner_snapshot(client)

    assert details == {}
    assert type(snapshot) is GameSnapshot
    assert snapshot.factory["successors"]["sources"]["growth:iron-plate"]["phase"] == "preferred"
    assert snapshot.factory["successors"]["sources"]["growth:iron-plate"]["use"]["job_id"] == "use:1"
    assert snapshot.factory["output_buffers"]["sources"]["growth:iron-plate"]["state"] == "ready"
    assert snapshot.factory["input_routes"]["sources"]["growth:iron-plate"]["state"] == "ready"
    assert len(client.commands) == 4  # probe, two current captures, final probe
    assert all("campaign.observe()" not in command for command in client.commands[1:3])

    row = snapshot.factory["successors"]["sources"]["growth:iron-plate"]
    output_row = snapshot.factory["output_buffers"]["sources"]["growth:iron-plate"]
    input_row = snapshot.factory["input_routes"]["sources"]["growth:iron-plate"]
    memory = KIND.memory_type(snapshot.session_id, "rocket_launch", last_tick=snapshot.tick,
                              status="completed")
    memory.completed_goals = {"rocket_launch": snapshot.tick}
    memory.history = [{"kind": "goal_completed", "goal": "rocket_launch", "tick": snapshot.tick}]
    memory.input_commitments["growth:iron-plate"] = {
        "layout": input_row["layout"], "source_unit": input_row["source_unit"],
        "parts": input_row["parts"],
    }
    memory.successor_projects["growth:iron-plate"] = {
        "anchor": row["anchor"], "predecessor_unit": row["predecessor_unit"],
        "source_unit": row["source_unit"], "started_tick": row["started_tick"],
        "deadline_tick": row["started_tick"] + MAX_PROJECT_TICKS, "status": "qualified",
    }
    memory.successor_receipts["growth:iron-plate"] = {
        "input": input_row["parts"], "input_layout": input_row["layout"],
        "output": output_row["parts"], "output_layout": output_row["layout"],
        "use": row["use"], "qualification": row["qualification"],
    }
    checkpoint = tmp_path / "completed-successor.json"
    memory.save(checkpoint)
    raw = checkpoint.read_bytes()
    # The full composed loader must retain the paid successor and mirrored input owners.
    loaded = KIND.memory_type.from_bytes(raw, snapshot.session_id, "rocket_launch")
    assert loaded.successor_receipts == memory.successor_receipts
    assert loaded.input_commitments == memory.input_commitments
    return lua, client, snapshot, loaded, checkpoint


def test_source_generated_paid_successor_reconciles_through_typed_checkpoint_gate(tmp_path):
    from jev_factorio.owner_completion import _validate_current_owners

    lua, client, snapshot, loaded, _checkpoint = _source_generated_successor_checkpoint(tmp_path)
    _validate_current_owners(loaded, snapshot, {})

    # The producer query may report a changed row, but the typed paid receipt
    # remains the authority and rejects the drift.
    lua.execute("storage.input_routes.cells['growth:iron-plate'].parts.drill.receipt='forged'")
    changed, details = observe_current_owner_snapshot(client)
    assert details == {}
    with pytest.raises(ValueError):
        _validate_current_owners(loaded, changed, {})


def test_source_generated_paid_successor_reconciles_through_public_typed_preflight(tmp_path):
    from jev_factorio.owner_completion import preflight_completed_owner

    lua, client, snapshot, _loaded, checkpoint = _source_generated_successor_checkpoint(tmp_path)

    from jev_factorio.provenance import source_revision
    owner_path = tmp_path / "continuous_owner.py"
    owner_path.write_text("# offline owner contract fixture\n", encoding="utf-8")
    owner_hash = hashlib.sha256(owner_path.read_bytes()).hexdigest()
    source_root = Path(__file__).resolve().parents[1]
    source_pin = source_revision(source_root)
    assert source_pin is not None
    native_identity = {
        "tick": snapshot.tick, "speed": 1, "marked": True, "runtime": True,
        "session_id": snapshot.session_id, "connected": 1,
    }
    witness = preflight_completed_owner(
        checkpoint, target="rocket_launch",
        source_root=source_root,
        expected_source_revision=source_pin, startup_source_revision=source_pin,
        owner_path=owner_path, expected_owner_sha256=owner_hash,
        startup_owner_sha256=owner_hash, native_identity=native_identity, client=client)
    assert witness.observation_sha256 and witness.session_id == snapshot.session_id

    # A changed paid receipt must fail through the same public checkpoint gate.
    lua.execute("storage.input_routes.cells['growth:iron-plate'].parts.drill.receipt='forged'")
    with pytest.raises(ValueError):
        preflight_completed_owner(
            checkpoint, target="rocket_launch",
            source_root=source_root,
            expected_source_revision=source_pin, startup_source_revision=source_pin,
            owner_path=owner_path, expected_owner_sha256=owner_hash,
            startup_owner_sha256=owner_hash, native_identity=native_identity, client=client)
