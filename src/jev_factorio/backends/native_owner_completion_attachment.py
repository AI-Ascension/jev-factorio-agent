"""Non-mutating native projection for a completed, existing campaign owner.

This path deliberately does not call campaign.observe, ObservedFactory, or any
module observer. Those producer callbacks can refresh surveys, sample flow,
settle successors, or mark faults. The query projects only already-retained
native records and uses the input route's documented read-only ``inspect``
method for its current geometry/topology boundary.
"""
from __future__ import annotations

import json
import math
from copy import deepcopy
import time

from ..iteration_timing import decode_native
from ..state import GameSnapshot
from .native_attachment import readback
from .native_current_attachment import DIRECT_MODULE_PROFILES, source_bound_direct_profile


# Only the explicitly enumerated source-generated direct compositions are
# eligible. This is intentionally not an optional-module subset rule.
COMPLETION_PROFILES = frozenset(DIRECT_MODULE_PROFILES)


def completion_query_command(attachment: dict) -> str:
    """Build one bounded query over the exact metadata-qualified installation."""
    profile = source_bound_direct_profile(attachment)
    if profile not in COMPLETION_PROFILES:
        raise ValueError('Native feature composition has no read-only completion query')
    native = attachment.get('native_installation')
    if (not isinstance(native, dict) or native.get('profile') is not False
            or not isinstance(native.get('assets'), dict)):
        raise ValueError('Completion query requires a pinned direct native installation')
    expected_modules = json.dumps(attachment['modules'], sort_keys=True, separators=(',', ':'))
    expected_assets = json.dumps(native['assets'], sort_keys=True, separators=(',', ':'))
    expected_session = json.dumps(attachment['session_id'])
    expected_actor = str(attachment['actor_unit'])
    body = r'''local rt=assert(jev_fle_runtime);local c=assert(rt.campaign);local f=assert(rt.fair)
local n=assert(rt.native_installation);local cb=assert(n.callbacks)
local a=assert(rt.agent_characters and rt.agent_characters[1]);local p=assert(f.actor())
local expected_modules=helpers.json_to_table(__MODULES__)
local expected_assets=helpers.json_to_table(__ASSETS__)
assert(rt.jev_session_id==__SESSION__ and n.session_id==__SESSION__
    and n.actor_unit==__ACTOR__ and n.schema=="jev.native-installation.v2" and not n.profile)
assert(a.valid and a.unit_number==__ACTOR__ and p.character==a and p.connected
    and p.force==a.force and p.surface==a.surface and game.speed==1 and not game.tick_paused
    and not p.cheat_mode)
assert(cb.observe==c.observe and cb.fair_tick==f.tick_handler
    and cb.connector_observe==c.observe_connector_ownership)
local modules={fair_actions=true,factory=c~=nil,launch_readiness=rt.launch_readiness~=nil,
 observation=c.observation_snapshot~=nil,observation_v2=c.observation_snapshot_v2~=nil,
 craft_jobs=c.craft_jobs~=nil,output_buffers=rt.output_buffers~=nil,input_routes=rt.input_routes~=nil,
 production_sites=rt.production_sites~=nil,mining_outposts=rt.mining_outposts~=nil,
 solid_routes=rt.solid_routes~=nil,coal_supply=rt.coal_supply~=nil,successors=rt.successors~=nil,
 connector_ownership=c.connector_ledger~=nil,coal_manual_journal_v1=rt.coal_manual_journal_v1~=nil,
 coal_manual_cycle_v2=rt.coal_manual_cycle_v2~=nil,
 connector_observer_bridge_v1=rt.connector_observer_bridge_v1~=nil,
 bootstrap_output_v1=rt.bootstrap_output_v1~=nil}
local function same(x,y)
 if type(x)~=type(y) then return false end
 if type(x)~="table" then return x==y end
 for k,v in pairs(x) do if not same(v,y[k]) then return false end end
 for k in pairs(y) do if x[k]==nil then return false end end
 return true
end
assert(same(modules,expected_modules) and same(n.assets,expected_assets))
assert(type(c.entities)=="table" and type(c.connector_ledger)=="table"
    and c.connector_ledger.protocol==1 and c.connector_ledger.active==nil)
local function point(v)
 assert(type(v)=="table" and type(v.x or v[1])=="number" and type(v.y or v[2])=="number")
 return {x=v.x or v[1],y=v.y or v[2]}
end
local function same(a,b)
 return math.abs(a.x-b.x)<0.01 and math.abs(a.y-b.y)<0.01
end
local budget=0
local function plain(v,depth)
 depth=depth or 0;budget=budget+1
 assert(depth<=12 and budget<=30000,"Completion projection exceeds bound")
 local kind=type(v)
 if kind=="nil" or kind=="string" or kind=="boolean" then return v end
 if kind=="number" then assert(v==v and math.abs(v)<math.huge,"Non-finite completion value");return v end
 assert(kind=="table","Native completion projection contains non-data")
 local out={}
 for k,x in pairs(v) do
  assert(type(k)=="string" or type(k)=="number","Invalid completion key")
  out[k]=plain(x,depth+1)
 end
 return out
end
local function paid_parts(parts)
 local out={}
 for key,v in pairs(parts or {}) do
  assert(type(key)=="string" and type(v)=="table")
  out[key]={role=v.role,unit_number=v.unit_number,receipt=v.receipt,paid=v.paid}
 end
 return out
end
local function entity_rows()
 local rows={};local count=0
 for role,e in pairs(c.entities) do
  count=count+1;assert(count<=2048 and type(role)=="string" and #role<=128)
  if e and e.valid then
   local row={name=e.name,unit_number=e.unit_number,position=point(e.position)}
   if type(e.get_recipe)=="function" then local recipe=e.get_recipe();row.recipe=recipe and recipe.name or "" end
   if type(e.products_finished)=="number" then row.products_finished=e.products_finished end
   if type(e.electric_network_id)=="number" then row.electric_network_id=e.electric_network_id end
   if type(e.energy)=="number" then row.energy=e.energy end
   rows[role]=row
  end
 end
 return rows
end
local tick=game.tick;local factory={tick=tick,entities=entity_rows(),acceptance_runtime={
 player_index=p.index,surface_index=p.surface.index,force_index=p.force.index}}
-- A pending path or mining request still owns the actor even when the last
-- controller checkpoint says completed. Never renew its lease or stop it here.
assert(type(p.walking_state)=="table" and p.walking_state.walking==false
    and type(p.mining_state)=="table" and p.mining_state.mining==false,
    "Native actor controls require reconciliation")
assert(not f.quarantined and f.mining_target==nil,
    "Native actor ownership requires reconciliation")
if f.job~=nil then
 assert(type(f.job)=="table" and (f.job.kind=="walk" or f.job.kind=="mine")
     and (f.job.status=="completed" or f.job.status=="failed")
     and f.job.unit==a.unit_number and f.job.cleanup_result=="cleared",
     "Native actor job requires reconciliation")
end
-- Metadata qualification alone does not settle an existing native craft. The
-- completion path must inspect it without invoking the mutating job observer.
assert(type(p.crafting_queue_size)=="number" and p.crafting_queue_size==0,
    "Native craft queue requires reconciliation")
assert(p.crafting_queue==nil or type(p.crafting_queue)=="table" and next(p.crafting_queue)==nil,
    "Native craft queue requires reconciliation")
if c.craft_jobs then
 local jobs=c.craft_jobs
 assert(not jobs.submitting,"Native craft submission requires reconciliation")
 factory.craft_jobs_protocol=1
 factory.craft_job_actor={session_id=rt.jev_session_id,player_index=p.index,
     unit_number=a.unit_number,surface_index=p.surface.index,force_index=p.force.index}
 factory.player_connected=true;factory.player_bound=true
 if jobs.job~=nil then
  assert(type(jobs.job)=="table" and jobs.job.status=="completed",
      "Native craft receipt requires reconciliation")
  factory.craft_job=plain(jobs.job)
 end
end
if rt.output_buffers then
 local owner=rt.output_buffers
 local data={protocol=1,session_id=rt.jev_session_id,tick=tick,sources={}}
 local handler_ok=script.get_event_handler(defines.events.on_tick)==owner.tick_handler
 local count=0
 local function project_output(source,cell,proposed)
  count=count+1;assert(count<=5 and type(cell)=="table")
  assert(type(cell.parts)=="table")
  local entity=c.entities[source];local arm=cell.parts.inserter and cell.parts.inserter.entity
  local chest=cell.parts.chest and cell.parts.chest.entity
  local linked=arm and chest and arm.pickup_target==entity and arm.drop_target==chest
  local valid=entity and entity.valid and entity==cell.entity and entity.unit_number==cell.source_unit
      and entity.name=="stone-furnace" and entity.force==p.force and entity.surface==p.surface
      and same(point(entity.position),cell.source_position) and not cell.fault and handler_ok
  local recipe=entity and type(entity.get_recipe)=="function" and entity.get_recipe()
  recipe=recipe and recipe.name or ""
  valid=valid and (recipe=="" or recipe==string.sub(source,8))
  for part,entry in pairs(cell.parts) do
   local e=entry.entity
   valid=valid and e and e.valid and c.entities[entry.role]==e and e.unit_number==entry.unit_number
       and entry.paid==1 and entry.receipt and (part=="chest" and e.name=="wooden-chest"
       or part=="inserter" and e.name=="burner-inserter")
   valid=valid and e.force==entity.force and e.surface==entity.surface
       and same(point(e.position),cell[part.."_position"])
   if part=="inserter" then valid=valid and e.direction==cell.direction end
  end
  local hand=arm and arm.valid and arm.held_stack
  local held=hand and hand.valid_for_read and hand.name==cell.item and hand.count or 0
  data.sources[source]={source=source,source_unit=cell.source_unit,item=cell.item,layout=cell.layout,
      chest_role=cell.chest_role,parts=paid_parts(cell.parts),state=not valid and "fault"
      or (proposed and "proposed" or (cell.parts.inserter and linked and "ready"
          or (next(cell.parts) and "building" or "proposed"))),
      topology=linked==true,fault=cell.fault,held=held,flow=plain(cell.flow or {})}
 end
 for source,cell in pairs(owner.cells) do project_output(source,cell,false) end
 for source,cell in pairs(owner.offers) do
  if not owner.cells[source] then project_output(source,cell,true) end
 end
 factory.output_buffers=data
end
if rt.input_routes then
 local data={protocol=1,session_id=rt.jev_session_id,tick=tick,sources={},diagnostics={}}
 local count=0;local owner=assert(rt.input_routes)
 assert(type(owner.inspect)=="function","Read-only input route inspector missing")
 local function project_input(source,cell,proposed)
  count=count+1;assert(count<=4 and type(cell)=="table")
  assert(type(cell.parts)=="table")
  local inspected=proposed and {} or owner.inspect(source)
  local valid=not proposed and inspected.source==source and inspected.session_id==rt.jev_session_id
      and inspected.tick==tick and inspected.geometry_valid==true
      and inspected.topology_valid==true and not inspected.fault
      and not cell.fault and cell.source_unit==inspected.source_unit
  data.sources[source]={source=source,source_unit=cell.source_unit,item=cell.item,ore=cell.ore,
      layout=cell.layout,steps=plain(cell.steps),parts=paid_parts(cell.parts),
      reserve_belts=cell.reserve_belts,topology=valid,
      state=proposed and "proposed" or (not valid and "fault" or (cell.flow and "ready" or "building")),
      flow=plain(cell.flow or {})}
 end
 for source,cell in pairs(owner.cells) do project_input(source,cell,false) end
 for source,cell in pairs(owner.offers) do
  if not owner.cells[source] then project_input(source,cell,true) end
 end
 local roles={"recipe:iron-plate","recipe:copper-plate"}
 if c.successors_enabled then roles[#roles+1]="growth:iron-plate";roles[#roles+1]="growth:copper-plate" end
 local ores={ ["recipe:iron-plate"]="iron-ore",["recipe:copper-plate"]="copper-ore",
   ["growth:iron-plate"]="iron-ore",["growth:copper-plate"]="copper-ore" }
 for _,role in ipairs(roles) do
  local source=c.entities[role];local output=rt.output_buffers.cells[role]
  local saved=owner.survey_diagnostics and owner.survey_diagnostics[role]
  local detail=plain(saved or {});local reason=detail.reason or "survey_not_due"
  local route=data.sources[role]
  if route then
   reason=route.state=="fault" and "route_fault" or (owner.cells[role] and "owned_route" or "route_available")
  elseif not source or not source.valid then reason="producer_missing"
  elseif rt.mining_outposts and rt.mining_outposts.cells[ores[role]] then reason="outpost_conflict"
  elseif not output or not output.flow or output.fault then reason="output_not_commissioned"
  elseif saved and (saved.source_unit~=source.unit_number or saved.output_layout~=output.layout
      or not saved.source_position or not same(point(saved.source_position),point(source.position))) then
   reason="stale_source_evidence"
  end
  if owner.cells[role] then
   local inspected=owner.inspect(role)
   detail.fault=inspected.fault;detail.topology_reason=inspected.reason
  end
  detail.reason=reason;detail.fallback="batched_manual_supply";detail.max_belts=64
  detail.survey_radius=40;detail.observed_tick=tick
  detail.cached=saved~=nil and saved.survey_tick~=tick
  local last=owner.last_survey and owner.last_survey[role]
  detail.next_survey_tick=last and last+300 or tick
  data.diagnostics[role]=detail
 end
 factory.input_routes=data
end
if rt.production_sites then
 local data={protocol=1,session_id=rt.jev_session_id,tick=tick,sources={}}
 local owner=rt.production_sites
 local roles={"recipe:iron-plate","recipe:copper-plate"}
 if c.successors_enabled then roles[#roles+1]="growth:iron-plate";roles[#roles+1]="growth:copper-plate" end
 for _,role in ipairs(roles) do
  local site=owner.owned and owner.owned[role];local state="rejected"
  local reason=owner.reasons and owner.reasons[role]
  if site then
   local entity=c.entities[role]
   local valid=site.surface==p.surface and site.force==p.force and entity and entity.valid
       and entity==site.entity and entity.unit_number==site.source_unit and entity.name=="stone-furnace"
       and same(point(entity.position),point(site.position))
   state=valid and "owned" or "fault"
   reason=valid and (reason or "joint_layout_owned") or "production_source_identity_changed"
  elseif string.sub(role,1,7)=="growth:" and not c.entities["recipe:"..string.sub(role,8)] then
   reason="predecessor_missing"
  elseif c.entities[role] then
   reason="existing_manual_cell"
  else
   site=owner.offers and owner.offers[role]
   if site then state="proposed";reason="joint_layout_available"
   else
    local checked=owner.checked and owner.checked[role]
    assert(type(checked)=="number" and checked<=tick and tick-checked<300,
        "Production-site survey is due; completion query will not survey")
    reason=reason or "survey_not_due"
   end
  end
  local row={state=state,reason=reason or "survey_not_due",fallback="batched_manual_supply"}
  if site then
   row.anchor,row.position,row.belt_count=site.anchor,point(site.position),site.belt_count
   row.source_unit=site.source_unit
   row.bill={["stone-furnace"]=1,["burner-mining-drill"]=1,
       ["burner-inserter"]=2,["wooden-chest"]=1,["transport-belt"]=site.belt_count}
   row.components=plain(site.specs or {});row.checks=site.checks
  end
  data.sources[role]=row
 end
 factory.production_sites=data
end
if rt.mining_outposts then
 local owner=rt.mining_outposts;local data={protocol=1,session_id=rt.jev_session_id,tick=tick,sources={},diagnostics=plain(owner.reasons or {})}
 local function project_outpost(resource,cell,proposed)
  local remaining=0;assert(type(cell.patch)=="table","Outpost resource observation is unavailable")
  for _,ore in pairs(cell.patch) do if ore.valid then
   assert(ore.name==resource and type(ore.amount)=="number" and ore.amount>=0)
   remaining=remaining+ore.amount
  end end
  local chest=cell.parts.chest and cell.parts.chest.entity
  local drill=cell.parts.drill and cell.parts.drill.entity
  local linked=chest and drill and drill.drop_target==chest
  local valid=not cell.fault
  for _,part in pairs(cell.parts) do
   local e=part.entity;valid=valid and e and e.valid and c.entities[part.role]==e
       and e.unit_number==part.unit_number and part.paid==1
  end
  data.sources[resource]={resource=resource,layout=cell.layout,surface_index=cell.surface.index,
      force_index=cell.force.index,steps=plain(cell.steps),parts=paid_parts(cell.parts),remaining=remaining,
      flow=plain(cell.flow or {}),topology=linked==true,state=proposed and "proposed" or (not valid and "fault"
      or (linked and (remaining==0 and cell.flow and "depleted" or "ready") or "building"))}
 end
 for resource,cell in pairs(owner.cells) do project_outpost(resource,cell,false) end
 for resource,cell in pairs(owner.offers) do
  if not owner.cells[resource] then project_outpost(resource,cell,true) end
 end
 factory.mining_outposts=data
end
if rt.successors then
 local owner=rt.successors;local data={protocol=1,session_id=rt.jev_session_id,tick=tick,sources={}}
 assert(owner.pending_use==nil,"Successor use receipt is still pending")
 for role,m in pairs(owner.records) do
  assert(type(m)=="table")
  local input=rt.input_routes.cells[role];local output=rt.output_buffers.cells[role]
  local remaining=0
  if input and type(input.patch)=="table" then
   for _,ore in pairs(input.patch) do if ore.valid then
    assert(ore.name==input.ore and type(ore.amount)=="number" and ore.amount>=0)
    remaining=remaining+ore.amount
   end end
  else error("Successor current ore evidence is unavailable") end
  local qualification=plain(m.qualification or {});local use=plain(m.use or {})
  local phase="reserved"
  if m.source_unit>0 then phase="output_building" end
  if output and next(output.parts) and not next(output.flow or {}) then phase="output_commissioning" end
  if output and next(output.flow or {}) then phase="input_building" end
  if input and input.state=="ready" and input.topology and next(input.flow or {}) then phase="producing" end
  if next(qualification) then phase="preferred" end
  if m.fault then phase="fault" end
  data.sources[role]={source=role,item=string.sub(role,8),predecessor="recipe:"..string.sub(role,8),
      anchor=m.anchor,predecessor_unit=m.predecessor_unit,source_unit=m.source_unit,paid=m.paid,
      phase=phase,seeded=m.seeded,trial_collected=m.trial_collected,credit=m.credit,
      attribution_resets=m.attribution_resets,started_tick=m.started_tick,remaining_ore=remaining,
      use=use,qualification=qualification,fault=m.fault}
 end
 factory.successors=data
end
local solid_completion_sample=nil
if rt.solid_routes then
 local owner=rt.solid_routes;local data={protocol=1,session_id=rt.jev_session_id,tick=tick,
   actor_index=p.index,surface_index=p.surface.index,force_index=p.force.index,routes={},diagnostics={}}
 local count=0;local checker=owner.coal_api and owner.coal_api.topology
 assert(type(checker)=="function","Read-only solid topology checker missing")
  local function project_solid(id,cell,proposed)
   count=count+1;assert(count<=4 and type(cell)=="table")
   local ok,linked=true,false
   if not proposed then ok,linked=pcall(checker,cell) end
   local valid=proposed or ok and linked==true and not cell.fault and not cell.pending
   local parts=paid_parts(cell.parts)
   data.routes[id]={route=cell.route,layout=cell.layout,item=cell.item,source=plain(cell.source),
       target=plain(cell.target),steps=plain(cell.steps),parts=parts,
       state=proposed and "proposed" or (not valid and "fault" or (cell.flow and "ready" or "building")),
       topology=not proposed and valid or false,flow={},reason=cell.reason or (proposed and "proposal" or "building"),
       pending=cell.pending and {part=cell.pending.part,receipt=cell.pending.receipt,phase=cell.pending.phase} or {}}
  end
  local occupied={}
  for id,cell in pairs(owner.cells) do
   project_solid(id,cell,false)
   occupied[cell.source.role]=true;occupied[cell.target.role]=true
  end
  for id,cell in pairs(owner.offers) do
   if not owner.cells[id] then project_solid(id,cell,true) end
  end
  solid_completion_sample=owner.completion_sample(owner)
  for id,row in pairs(data.routes) do
   local sampled=solid_completion_sample.routes[id]
   if sampled and not sampled.proposed then
    if sampled.counters.ready then
     row.state="ready";row.topology=true;row.reason="observing_flow"
    else
     row.state="fault";row.topology=false;row.reason="building"
    end
   end
  end
  data.diagnostics=plain(solid_completion_sample.diagnostics)
  data.diagnostics_tick=solid_completion_sample.diagnostics_tick
  data.completion_sample=plain(solid_completion_sample)
  factory.solid_routes=data
end
if rt.coal_supply then
 local owner=rt.coal_supply;local data={protocol=owner.admission_evidence and 2 or 1,
   session_id=rt.jev_session_id,tick=tick,actor_index=p.index,surface_index=p.surface.index,
   force_index=p.force.index,targets=plain(owner.targets),committed=owner.committed==true,
   sources={},reason=owner.reason or "building"}
 for target,row in pairs(owner.rows) do
  local route=nil
  for _,candidate in pairs(rt.solid_routes.cells) do
   if candidate.source.role=="coal:"..target..":chest" then route=candidate.route end
  end
  local parts=paid_parts(row.parts)
  local state=not owner.committed and "proposed" or (row.parts.drill and row.flow
      and (row.remaining==0 and "depleted" or "ready") or "building")
  if row.fault then state="fault" end
  local pending=row.pending and {part=row.pending.part,receipt=row.pending.receipt,phase=row.pending.phase} or {}
  data.sources[target]={target=plain(row.target),layout=row.layout,steps=plain(row.steps),
      corridor=plain(row.corridor),chest_bounds=plain(row.chest_bounds),drill_bounds=plain(row.drill_bounds),
      mining_area=plain(row.mining_area),parts=parts,state=state,remaining=row.remaining,
      power=plain(row.power),route=route or "",flow={},
      reason=row.fault or row.reason or (owner.committed and "building" or "proposal"),
      pending=pending,manual_pending=plain(row.manual_pending or {})}
 end
 local coal_completion_sample=owner.completion_sample(owner,assert(solid_completion_sample).routes)
 for target,row in pairs(data.sources) do
  local sampled=coal_completion_sample.sources[target]
  if sampled and sampled.status=="ready" then
   row.remaining=sampled.counters.remaining;row.route=sampled.route
   row.state=row.remaining==0 and "depleted" or "ready";row.reason="observing_flow"
  end
 end
 data.completion_sample=plain(coal_completion_sample)
 if owner.admission_evidence then data.admission={protocol=1,session_id=data.session_id,tick=tick,
   actor_index=p.index,surface_index=p.surface.index,force_index=p.force.index,qualified=false,
   reason="electric_conversion_and_construction_cost_unknown"} end
 factory.coal_supply=data
end
local ledger=c.connector_ledger;local routes={}
for id,row in pairs(ledger.routes) do
 local count=0;local valid=storage.jev_session_id==row.session_id and row.actor_unit==a.unit_number
     and row.surface_index==p.surface.index and row.force_index==p.force.index and row.pending==nil
 local source,target=c.entities[row.source],c.entities[row.target]
 valid=valid and source and source.valid and source.unit_number==row.source_unit
     and target and target.valid and target.unit_number==row.target_unit
 for _,cell in ipairs(row.cells) do
  count=count+1
  if cell.unit_number then
   local entity=p.surface.find_entity(row.kind,cell.position)
   valid=valid and entity and entity.valid and entity.unit_number==cell.unit_number
  end
 end
 routes[id]={id=id,source=row.source,target=row.target,source_unit=row.source_unit,
   target_unit=row.target_unit,kind=row.kind,fluid=row.fluid,actor_unit=row.actor_unit,
   surface_index=row.surface_index,force_index=row.force_index,session_id=row.session_id,
   state=valid and row.state or "fault",pending=row.pending,paid=row.paid,external=row.external,
   owned=valid and row.owned or false,cell_count=count}
end
factory.connector_ownership={protocol=1,session_id=rt.jev_session_id,tick=tick,routes=routes}
assert(game.tick==tick,"Native game tick changed during completion query")
rcon.print(helpers.table_to_json({schema=1,session_id=rt.jev_session_id,actor_unit=a.unit_number,
 tick=tick,world_kind="fle",factory=factory,inventory={}}))'''
    body = (body.replace('__MODULES__', json.dumps(expected_modules))
            .replace('__ASSETS__', json.dumps(expected_assets))
            .replace('__SESSION__', expected_session).replace('__ACTOR__', expected_actor))
    return '/sc ' + body


def _direct_connector_details(client, attachment, snapshot, checkpoint_binding):
    from ..connector_checkpoint import capture, validate_binding
    from .native_attachment import prepare_install_command

    binding = checkpoint_binding
    if binding is None:
        return {}
    routes = validate_binding(binding, snapshot.session_id)['routes']
    observed = snapshot.factory.get('connector_ownership', {}).get('routes', {})
    if set(routes) != set(observed):
        raise RuntimeError('Native connector ownership changed during completion query')

    class Native:
        def command(self, script):
            prepared = prepare_install_command(script, attachment)
            return client.send_command('/sc ' + prepared)

    native = Native()
    result = {}
    for receipt, saved in routes.items():
        current = observed.get(receipt)
        if not isinstance(current, dict):
            raise RuntimeError('Native connector route is missing')
        detail = capture(native, receipt, current)
        if detail != saved:
            raise RuntimeError('Native connector paid cells differ from checkpoint')
        result[receipt] = detail
    return result


_CAPTURE_CLOCK_FIELDS = {"tick", "diagnostics_tick", "observed_tick", "next_survey_tick",
                         "last_tick", "last_positive_tick"}
_CAPTURE_NONDECREASING_FIELDS = {
    "positive_samples", "sent", "received", "unattributed_loss", "mined",
    "delivered_lower", "manual_inserted", "burned_lower_joules", "produced",
    "consumed", "delivered", "new_plates", "products_finished",
}
_CAPTURE_NONINCREASING_FIELDS = {"remaining", "remaining_ore"}


def _same_completion_capture(first, second) -> bool:
    """Compare reads while allowing only monotonic clock/measurement progress.

    Identity, paid prefixes, receipts, actor/session/source bindings, topology,
    pending state and all other fields remain exact. The retained checkpoint is
    independently checked against the resulting snapshot after capture.
    """
    def walk(left, right, key=None):
        if isinstance(left, dict) or isinstance(right, dict):
            if not isinstance(left, dict) or not isinstance(right, dict) or set(left) != set(right):
                return False
            return all(walk(left[name], right[name], name) for name in left)
        if isinstance(left, list) or isinstance(right, list):
            return (isinstance(left, list) and isinstance(right, list)
                    and len(left) == len(right)
                    and all(walk(a, b) for a, b in zip(left, right)))
        if key in _CAPTURE_CLOCK_FIELDS:
            return (type(left) is int and type(right) is int
                    and 0 <= left <= right <= 2**53 - 1)
        if key in _CAPTURE_NONDECREASING_FIELDS:
            return (type(left) in (int, float) and type(right) in (int, float)
                    and math.isfinite(left) and math.isfinite(right)
                    and 0 <= left <= right)
        if key in _CAPTURE_NONINCREASING_FIELDS:
            return (type(left) in (int, float) and type(right) in (int, float)
                    and math.isfinite(left) and math.isfinite(right)
                    and 0 <= right <= left)
        if key in {"energy", "held"}:
            return (type(left) in (int, float) and type(right) in (int, float)
                    and math.isfinite(left) and math.isfinite(right)
                    and left >= 0 and right >= 0)
        if key == "cached":
            return type(left) is bool and type(right) is bool
        return type(left) is type(right) and left == right

    if (not isinstance(first, dict) or not isinstance(second, dict)
            or first.get("schema") != second.get("schema")
            or first.get("session_id") != second.get("session_id")
            or first.get("actor_unit") != second.get("actor_unit")
            or first.get("world_kind") != second.get("world_kind")
            or type(first.get("tick")) is not int or type(second.get("tick")) is not int
            or second["tick"] < first["tick"]):
        return False
    return walk(first, second)


def _completion_sample_series(samples, feature, snapshot_rows):
    """Validate the direct-query sample headers against each returned snapshot."""
    if not samples or len(samples) != len(snapshot_rows):
        raise RuntimeError('Native completion sample series is incomplete')
    if feature == 'solid_routes':
        fields = {'schema', 'producer', 'session_id', 'tick', 'actor_index', 'actor_unit',
                  'surface_index', 'force_index', 'routes', 'diagnostics', 'diagnostics_tick',
                  'diagnostics_source'}
        producer = 'solid_routes_readonly_completion_v1'
        mapping = 'routes'
    elif feature == 'coal_supply':
        fields = {'schema', 'producer', 'session_id', 'tick', 'actor_index', 'actor_unit',
                  'surface_index', 'force_index', 'sources'}
        producer = 'coal_supply_readonly_completion_v1'
        mapping = 'sources'
    else:
        raise ValueError('Unknown completion sample family')
    first = samples[0]
    for sample, snapshot_row in zip(samples, snapshot_rows):
        if (type(sample) is not dict or set(sample) != fields
                or type(sample.get('schema')) is not int or sample['schema'] != 1
                or sample.get('producer') != producer
                or sample.get('session_id') != snapshot_row.get('session_id')
                or type(sample.get('tick')) is not int or sample['tick'] != snapshot_row.get('tick')
                or type(sample.get('actor_index')) is not int or sample['actor_index'] < 1
                or type(sample.get('actor_unit')) is not int or sample['actor_unit'] != snapshot_row.get('actor_unit')
                or type(sample.get('surface_index')) is not int or sample['surface_index'] < 1
                or type(sample.get('force_index')) is not int or sample['force_index'] < 1
                or type(sample.get(mapping)) is not dict):
            raise RuntimeError(f'Native {feature} completion sample is malformed or unbound')
        owner = snapshot_row['factory'].get(feature)
        if (not isinstance(owner, dict) or owner.get('session_id') != sample['session_id']
                or owner.get('tick') != sample['tick']
                or owner.get('actor_index') != sample['actor_index']
                or owner.get('surface_index') != sample['surface_index']
                or owner.get('force_index') != sample['force_index']):
            raise RuntimeError(f'Native {feature} sample disagrees with its owner projection')
        if feature == 'solid_routes':
            if (type(sample.get('diagnostics_tick')) is not int
                    or sample['diagnostics_tick'] > sample['tick']
                    or type(sample.get('diagnostics_source')) is not str):
                raise RuntimeError('Native solid diagnostics are malformed')
        if sample[mapping].keys() != first[mapping].keys():
            raise RuntimeError(f'Native {feature} owner set changed during sampling')
        for key in ('session_id', 'actor_index', 'actor_unit', 'surface_index', 'force_index'):
            if sample[key] != first[key]:
                raise RuntimeError(f'Native {feature} identity changed during sampling')
    ticks = [sample['tick'] for sample in samples]
    if (any(right <= left for left, right in zip(ticks, ticks[1:]))
            or ticks[-1] - ticks[0] < 120 or ticks[-1] - ticks[0] > 600):
        raise RuntimeError(f'Native {feature} completion sampling window is outside 120..600 ticks')
    return mapping


def _solid_baseline(row, route, sample_route, sample_tick):
    from .. import solid_routes

    baseline = sample_route.get('baseline_flow')
    if baseline == {}:
        return None
    if type(baseline) is not dict:
        raise RuntimeError('Retained solid flow baseline is malformed')
    checked = deepcopy(row)
    checked['flow'] = baseline
    solid_routes.validate_row(checked, route)
    if baseline['last_tick'] > sample_tick:
        raise RuntimeError('Retained solid flow baseline is future-dated')
    return baseline


def _coal_baseline(row, target, sample_source, sample_tick):
    from .. import coal_supply

    baseline = sample_source.get('baseline_flow')
    if baseline == {}:
        return None
    if type(baseline) is not dict:
        raise RuntimeError('Retained coal flow baseline is malformed')
    checked = deepcopy(row)
    checked['flow'] = baseline
    coal_supply.validate_row(checked, target)
    if baseline['last_tick'] > sample_tick:
        raise RuntimeError('Retained coal flow baseline is future-dated')
    return baseline


def _same_baseline_identity(first, later, keys):
    if first is None or later is None:
        return first is None and later is None
    return all(first.get(key) == later.get(key) for key in keys)


def _project_completion_flows(snapshot, sample_history, snapshot_rows):
    """Build fresh completion flows from nonmutating native counter reads.

    Retained producer counters are historical context only. Every projected
    flow field describes this bounded sample window; historical totals are
    used only to bind identity, monotonicity, and conservation. Completion
    still requires the producer's three positive samples in the current window.
    """
    from .. import coal_supply, solid_routes

    factory = deepcopy(snapshot.factory)
    solid_owner = factory.get('solid_routes')
    coal_owner = factory.get('coal_supply')
    if not isinstance(solid_owner, dict):
        return snapshot
    solid_samples = [row.get('solid_routes') for row in sample_history]
    _completion_sample_series(solid_samples, 'solid_routes', snapshot_rows)
    coal_samples = None
    if isinstance(coal_owner, dict):
        coal_samples = [row.get('coal_supply') for row in sample_history]
        _completion_sample_series(coal_samples, 'coal_supply', snapshot_rows)

    sample_ticks = [value['tick'] for value in solid_samples]
    first_tick, last_tick = sample_ticks[0], sample_ticks[-1]
    solid_flow_by_route = {}
    coal_flow_by_target = {}

    if coal_samples is not None:
        final_coal_sample = coal_samples[-1]
        source_rows = coal_owner.get('sources', {})
        for target, row in source_rows.items():
            # A persisted flow is historical context, not proof that the
            # completion query observed current mined delivery.
            row['flow'] = {}
            sampled = final_coal_sample['sources'].get(target)
            if not isinstance(sampled, dict) or sampled.get('status') != 'ready':
                continue
            entries = [value['sources'].get(target) for value in coal_samples]
            if any(not isinstance(entry, dict) or entry.get('status') != 'ready'
                   for entry in entries):
                raise RuntimeError('Coal completion owner changed during counter sampling')
            static_keys = ('target', 'layout', 'route', 'route_layout', 'source_first_tick',
                           'initial_delivered', 'status', 'target_unit', 'chest_unit',
                           'drill_unit', 'parts', 'base_ore', 'base_energy', 'base_manual',
                           'fuel_value')
            if any(any(entry.get(key) != sampled.get(key) for key in static_keys)
                   for entry in entries):
                raise RuntimeError('Coal paid source identity changed during counter sampling')
            counters = [entry.get('counters') for entry in entries]
            expected_counter_keys = {'tick', 'remaining', 'mined', 'contained', 'chest', 'pipe',
                                     'energy', 'manual', 'delivered_lower',
                                     'burned_lower_joules'}
            if any(type(value) is not dict or set(value) != expected_counter_keys
                   or value.get('tick') != entry.get('counters', {}).get('tick')
                   for value, entry in zip(counters, entries)):
                raise RuntimeError('Coal current counters are malformed')
            for entry, counter, sample in zip(entries, counters, coal_samples):
                if (type(counter['tick']) is not int or counter['tick'] != sample['tick']
                        or counter['tick'] != entry['counters']['tick']
                        or type(counter['remaining']) is not int or counter['remaining'] < 0
                        or type(counter['mined']) is not int or counter['mined'] < 0
                        or type(counter['contained']) is not int or counter['contained'] < 0
                        or type(counter['chest']) is not int or type(counter['pipe']) is not int
                        or counter['chest'] + counter['pipe'] != counter['contained']
                        or type(counter['manual']) is not int or counter['manual'] < 0
                        or type(counter['delivered_lower']) is not int or counter['delivered_lower'] < 0
                        or any(type(counter[name]) not in (int, float)
                               or not math.isfinite(counter[name]) or counter[name] < 0
                               for name in ('energy', 'delivered_lower', 'burned_lower_joules'))
                        or counter['mined'] != sampled['base_ore'] - counter['remaining']
                        or counter['delivered_lower'] != max(
                            entry['initial_delivered'], counter['mined']-counter['contained']-1, 0)):
                    raise RuntimeError('Coal counter violates its retained source accounting')
            for left, right in zip(counters, counters[1:]):
                if (right['mined'] < left['mined'] or right['remaining'] > left['remaining']
                        or right['manual'] < left['manual']
                        or right['delivered_lower'] < left['delivered_lower']):
                    raise RuntimeError('Coal completion counters regressed')
            # Manual insertion is separately receipted but is not mined-route
            # progress, so this interval cannot use it to prove fresh delivery.
            if any(right['manual'] != left['manual'] for left, right in zip(counters, counters[1:])):
                raise RuntimeError('Coal manual transfer changed during completion sampling')
            positive_ticks = []
            for left, right in zip(counters, counters[1:]):
                if right['delivered_lower'] > left['delivered_lower']:
                    positive_ticks.append(right['tick'])
            baseline_history = [
                _coal_baseline(row, target, entry, sample['tick'])
                for entry, sample in zip(entries, coal_samples)
            ]
            baseline_start, baseline_end = baseline_history[0], baseline_history[-1]
            baseline_identity = ('layout', 'route_layout', 'method', 'drill_unit', 'chest_unit',
                                 'target_unit', 'initial_fuel_joules', 'fuel_value_joules',
                                 'in_transit_uncertainty', 'self_fuel')
            for candidate in baseline_history:
                if not _same_baseline_identity(baseline_start, candidate, baseline_identity):
                    raise RuntimeError('Coal retained flow identity changed during counter sampling')
            for left, right in zip(baseline_history, baseline_history[1:]):
                if left is not None and right is not None and (
                        right['first_tick'] != left['first_tick']
                        or right['last_tick'] < left['last_tick']
                        or right['last_positive_tick'] < left['last_positive_tick']
                        or right['positive_samples'] < left['positive_samples']
                        or right['mined'] < left['mined']
                        or right['delivered_lower'] < left['delivered_lower']
                        or right['manual_inserted'] < left['manual_inserted']):
                    raise RuntimeError('Coal retained flow history regressed during sampling')
            final_counter = counters[-1]
            fresh_delivered = final_counter['delivered_lower'] - counters[0]['delivered_lower']
            fresh_mined = final_counter['mined'] - counters[0]['mined']
            fresh_manual = final_counter['manual'] - counters[0]['manual']
            if fresh_delivered < 0 or fresh_mined < fresh_delivered or fresh_manual != 0:
                raise RuntimeError('Coal fresh delivery proof is invalid')
            fuel_value = sampled['fuel_value']
            fresh_burned = max(
                0, counters[0]['energy'] + (fresh_delivered + fresh_manual) * fuel_value
                - final_counter['energy'])
            flow = {
                'layout': sampled['layout'], 'route_layout': sampled['route_layout'],
                'method': coal_supply.METHOD,
                'first_tick': first_tick,
                'last_tick': last_tick,
                'last_positive_tick': max(positive_ticks) if positive_ticks else first_tick,
                'positive_samples': len(positive_ticks),
                'mined': fresh_mined,
                'delivered_lower': fresh_delivered,
                'manual_inserted': fresh_manual,
                'drill_unit': sampled['drill_unit'], 'chest_unit': sampled['chest_unit'],
                'target_unit': sampled['target_unit'],
                'burned_lower_joules': fresh_burned,
                'initial_fuel_joules': counters[0]['energy'],
                'target_fuel_joules': final_counter['energy'],
                'fuel_value_joules': fuel_value,
                'in_transit_uncertainty': 1, 'self_fuel': 0,
            }
            if (flow['first_tick'] > first_tick or flow['first_tick'] > flow['last_tick']
                    or flow['last_positive_tick'] > flow['last_tick']):
                raise RuntimeError('Coal flow timestamps are future-dated')
            if fresh_delivered > 0 and len(positive_ticks) >= 3:
                coal_flow_by_target[target] = flow

    for route, row in solid_owner.get('routes', {}).items():
        if row.get('state') != 'ready':
            continue
        entries = [value['routes'].get(route) for value in solid_samples]
        if any(not isinstance(entry, dict) or entry.get('proposed')
               or entry.get('counters', {}).get('ready') is not True for entry in entries):
            raise RuntimeError('Solid completion route changed during counter sampling')
        static_keys = ('route', 'layout', 'item', 'source_role', 'source_unit',
                       'target_role', 'target_unit', 'target_inventory',
                       'source_first_tick', 'parts', 'proposed')
        if any(any(entry.get(key) != entries[0].get(key) for key in static_keys)
               for entry in entries[1:]):
            raise RuntimeError('Solid paid route identity changed during counter sampling')
        target_kind = row['target']['inventory']
        # Never carry an old completion flow into the current projection.
        # A route earns new flow evidence only from counter movement below.
        row['flow'] = {}
        if target_kind == 'fuel':
            matching = [target for target, candidate in coal_flow_by_target.items()
                        if row['source']['role'] == f'coal:{target}:chest'
                        and candidate['target_unit'] == row['target']['unit_number']]
            if len(matching) != 1:
                continue
            target = matching[0]
            fuel_flow = coal_flow_by_target[target]
            flow = {'layout': row['layout'], 'source_unit': row['source']['unit_number'],
                    'target_unit': row['target']['unit_number'], 'method': coal_supply.METHOD,
                    'first_tick': fuel_flow['first_tick'], 'last_tick': fuel_flow['last_tick'],
                    'last_positive_tick': fuel_flow['last_positive_tick'],
                    'positive_samples': fuel_flow['positive_samples'],
                    'sent': fuel_flow['mined'], 'received': fuel_flow['delivered_lower'],
                    'unattributed_loss': 0}
            row['flow'] = flow
            row['reason'] = 'observing_flow'
            solid_flow_by_route[route] = flow
            continue
        if target_kind != 'input':
            continue
        counters = [entry['counters'] for entry in entries]
        expected_counter_keys = {'ready', 'tick', 'available', 'produced', 'pipe',
                                 'target_stock', 'consumed', 'initial_pipe'}
        if any(type(value) is not dict or set(value) != expected_counter_keys
               or value.get('ready') is not True
               or type(value.get('tick')) is not int or value['tick'] != sample['tick']
               or any(type(value[name]) is not int or value[name] < 0
                      for name in ('tick', 'available', 'produced', 'pipe',
                                   'target_stock', 'consumed', 'initial_pipe'))
               for value, sample in zip(counters, solid_samples)):
            raise RuntimeError('Solid current counters are malformed')
        if len({value['initial_pipe'] for value in counters}) != 1:
            raise RuntimeError('Solid initial pipeline inventory changed')
        sent_delta = received_delta = loss_delta = positive_count = 0
        fresh_last_positive = None
        for left, right in zip(counters, counters[1:]):
            if right['produced'] < left['produced'] or right['consumed'] < left['consumed']:
                raise RuntimeError('Solid production counters regressed')
            sent = left['available'] - right['available'] + right['produced'] - left['produced']
            arrived = sent - (right['pipe'] - left['pipe'])
            received = right['target_stock'] - left['target_stock'] + right['consumed'] - left['consumed']
            if sent < 0 or arrived < 0 or received < 0 or arrived != received:
                raise RuntimeError('Solid route counter conservation changed')
            sent_delta += sent;received_delta += received;loss_delta += arrived-received
            if received > 0:
                positive_count += 1;fresh_last_positive = right['tick']
        baseline_history = [
            _solid_baseline(row, route, entry, sample['tick'])
            for entry, sample in zip(entries, solid_samples)
        ]
        base_start, base_end = baseline_history[0], baseline_history[-1]
        for candidate in baseline_history:
            if not _same_baseline_identity(
                    base_start, candidate, ('layout', 'source_unit', 'target_unit', 'method')):
                raise RuntimeError('Solid retained flow identity changed during counter sampling')
        for left, right in zip(baseline_history, baseline_history[1:]):
            if left is not None and right is not None and (
                    right['first_tick'] != left['first_tick']
                    or right['last_tick'] < left['last_tick']
                    or right['last_positive_tick'] < left['last_positive_tick']
                    or right['positive_samples'] < left['positive_samples']
                    or right['sent'] < left['sent']
                    or right['received'] < left['received']
                    or right['unattributed_loss'] < left['unattributed_loss']):
                raise RuntimeError('Solid retained flow history regressed during sampling')
        flow = {'layout': row['layout'], 'source_unit': row['source']['unit_number'],
                'target_unit': row['target']['unit_number'], 'method': 'stoichiometric_balance',
                'first_tick': first_tick, 'last_tick': last_tick,
                'last_positive_tick': fresh_last_positive or first_tick,
                'positive_samples': positive_count,
                'sent': sent_delta, 'received': received_delta,
                'unattributed_loss': loss_delta}
        if flow['first_tick'] > first_tick or flow['last_positive_tick'] > last_tick:
            raise RuntimeError('Solid flow timestamps are future-dated')
        if received_delta > 0 and positive_count >= 3:
            row['flow'] = flow;row['reason'] = 'observing_flow'
            solid_flow_by_route[route] = flow

    if isinstance(coal_owner, dict):
        for target, row in coal_owner.get('sources', {}).items():
            flow = coal_flow_by_target.get(target)
            if flow is not None:
                row['flow'] = flow
                row['route'] = coal_samples[-1]['sources'][target]['route']
                row['reason'] = 'observing_flow'
    return GameSnapshot(tick=snapshot.tick, session_id=snapshot.session_id,
                        world_kind=snapshot.world_kind, factory=factory,
                        inventory=deepcopy(snapshot.inventory))


def observe_current_owner_snapshot(client, *, checkpoint_binding=None):
    """Return a current typed projection without invoking composed observers."""
    attachment = readback(client, completion_query=True)
    profile = source_bound_direct_profile(attachment)
    if profile not in COMPLETION_PROFILES:
        raise RuntimeError('Native completion profile is not source-reviewed')
    command = completion_query_command(attachment)

    def decode_snapshot(raw):
        row = decode_native(raw)
        fields = {'schema', 'session_id', 'actor_unit', 'tick', 'world_kind', 'factory', 'inventory'}
        if (not isinstance(row, dict) or set(row) != fields or type(row.get('schema')) is not int
                or row['schema'] != 1 or row.get('session_id') != attachment['session_id']
                or type(row.get('actor_unit')) is not int or row['actor_unit'] != attachment['actor_unit']
                or type(row.get('tick')) is not int or row['tick'] < 1 or row.get('world_kind') != 'fle'
                or not isinstance(row.get('factory'), dict) or not isinstance(row.get('inventory'), dict)
                or row['factory'].get('tick') != row['tick']):
            raise RuntimeError('Native owner completion projection is malformed or stale')
        samples = {}
        for feature in ('solid_routes', 'coal_supply'):
            projection = row['factory'].get(feature)
            if projection is not None and 'completion_sample' in projection:
                samples[feature] = projection.pop('completion_sample')
            else:
                samples[feature] = None
        return row, GameSnapshot(tick=row['tick'], session_id=row['session_id'], world_kind='fle',
                                 factory=row['factory'], inventory=row['inventory']), samples

    first, snapshot, first_samples = decode_snapshot(client.send_command(command))
    # Connector detail pages are read-only but separately bounded. Bind them
    # to the same checkpoint bytes and to the direct query's route summary.
    details = _direct_connector_details(client, attachment, snapshot, checkpoint_binding)
    rows = [first]
    snapshots = [snapshot]
    sample_history = [first_samples]

    def needs_sampling(samples):
        solid_sample = samples.get('solid_routes')
        if isinstance(solid_sample, dict):
            if any(isinstance(route, dict) and route.get('proposed') is False
                   and isinstance(route.get('counters'), dict)
                   and route['counters'].get('ready') is True
                   for route in solid_sample.get('routes', {}).values()):
                return True
        coal_sample = samples.get('coal_supply')
        return (isinstance(coal_sample, dict)
                and any(isinstance(source, dict) and source.get('status') == 'ready'
                        for source in coal_sample.get('sources', {}).values()))

    target_reads = 5 if needs_sampling(first_samples) else 2
    while len(rows) < target_reads:
        if target_reads > 2:
            # Four bounded gaps allow ordinary speed-1 simulation to supply a
            # current 120..600 tick measurement window without any observer or
            # state mutation inside the native query itself.
            time.sleep(0.55)
        current, current_snapshot, current_samples = decode_snapshot(client.send_command(command))
        if not _same_completion_capture(rows[-1], current):
            raise RuntimeError('Native owner changed during the completion query')
        rows.append(current);snapshots.append(current_snapshot);sample_history.append(current_samples)
        if needs_sampling(current_samples):
            target_reads = max(target_reads, 5)

    if needs_sampling(sample_history[-1]):
        while snapshots[-1].tick - snapshots[0].tick < 120 and len(rows) < 7:
            time.sleep(0.55)
            current, current_snapshot, current_samples = decode_snapshot(client.send_command(command))
            if not _same_completion_capture(rows[-1], current):
                raise RuntimeError('Native owner changed during the completion query')
            rows.append(current);snapshots.append(current_snapshot);sample_history.append(current_samples)
        if snapshots[-1].tick - snapshots[0].tick < 120:
            raise RuntimeError('Native completion reads did not span a measured flow interval')
        snapshot = _project_completion_flows(snapshots[-1], sample_history, rows)
    else:
        snapshot = snapshots[-1]
    # A final metadata-only probe and source-built identity check detects an
    # attachment/session replacement during detail-page capture. It never runs
    # either the campaign or feature observers.
    final = readback(client, completion_query=True)
    if (final.get('session_id') != attachment.get('session_id')
            or final.get('actor_unit') != attachment.get('actor_unit')
            or final.get('modules') != attachment.get('modules')
            or final.get('native_installation') != attachment.get('native_installation')):
        raise RuntimeError('Native owner attachment changed during completion capture')
    return snapshot, details
