"""Versioned local alternative census composed with native v5 facts.

This profile keeps the installed v5/v6 assets immutable. It estimates manual
collection options from bounded areas around the actor and copper consumer.
The rest of the current surface and future terrain remain unknown. The result
is evidence only and cannot itself authorize a payment or claim realized
payback.
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
import hashlib
import json
import math

from .coal_economic_observation import (
    NativeEconomics, NativeEconomicsUnavailable, identity, integer, number,
    point, require, rows,
)
from .coal_economic_v6 import (
    CYCLE_SOURCE_SHA256, Census, CycleRow, V6Observation,
    decode_v6, digest, diagnostic_stock_upper_bounds,
)

SCHEMA = 'jev.coal-native-economics.v7'
PROFILE = 'jev.coal-native-economics-profile.local-alternatives-v7'
MARKER = '    out.query_status="observed";out.reason="bounded_native_projection"'
BASE_SOURCE_SHA256 = '1581fa9877df13d9a2e70c07cd121b57ceb3eda90e09820bb71a1c866a356e1b'
V7_CENSUS_SOURCE_SHA256 = 'bc7172d3f5e605e3f9726a590bcfadeb2d2ac78e54422702e72a643b8df06179'
NATIVE_ADMISSION_SOURCE_SHA256 = '69cfba9e97223407b5e3b53632f28657a9323ae3db46031881343a2f7a54bdae'
NATIVE_COAL_SUPPLY_ASSET_SHA256 = '3ec3b94b03c86cf963328ef9a6f75551ab285968ccfd50d2e2a25c72e89a242e'
V5_PROFILE = 'e759-observation-v2-water-origin-v4-manual-cycle-v5'
V5_OBSERVER_PROFILE = V5_PROFILE + '-connector-observer-v1'
V6_OBSERVER_PROFILE = V5_PROFILE.replace('manual-cycle-v5', 'manual-cycle-v6') + '-connector-observer-v1'
V7_CENSUS_KEY = 'material_census'


def fixed_query() -> str:
    """Compose the v7 read-only query without changing any frozen v5/v6 bytes."""
    return _compose_query()


def fixed_first_spend_query(parameters: dict) -> str:
    """Compose native proof and first payment into one RCON command."""
    if (not isinstance(parameters, dict)
            or set(parameters) != {'target', 'layout', 'part', 'receipt'}
            or any(type(value) is not str or not value for value in parameters.values())
            or parameters['part'] != 'chest'):
        raise ValueError('First coal payment requires an exact chest command')
    source = _compose_query(first_spend=True)
    request = 'local coal_native_request=helpers.json_to_table(' + json.dumps(
        json.dumps(parameters, sort_keys=True, separators=(',', ':'))) + ')\n'
    source = request + source
    state_marker = 'need(q.revision==4 and not q.committed,"unsupported_coal_state")'
    pending_marker = ('need(row and not row.pending and not row.manual_pending and not row.fault\n'
                      '            and next(bounded(row.parts,2))==nil and e and own(e)==row.target.unit_number\n'
                      '            and (e.name=="boiler" or e.name=="stone-furnace"),"unsupported_fuel_target")')
    prepared_state = '''local __pending_count=0
    need(q.revision==4 and q.committed==true,"unsupported_coal_state")
    for __target,__row in pairs(q.rows) do
        if __row.pending then
            __pending_count=__pending_count+1
            need(__target==coal_native_request.target
                and __row.pending.phase=="prepared"
                and __row.pending.part==coal_native_request.part
                and __row.pending.receipt==coal_native_request.receipt
                and __row.parts and next(__row.parts)==nil,"coal_prepared_identity_changed")
        end
    end
    need(__pending_count==1,"coal_first_payment_not_prepared")'''
    prepared_pending = '''need(row and (not row.pending or (target==coal_native_request.target
            and row.pending.phase=="prepared" and row.pending.part==coal_native_request.part
            and row.pending.receipt==coal_native_request.receipt
            and row.layout==coal_native_request.layout))
            and not row.manual_pending and not row.fault
            and next(bounded(row.parts,2))==nil and e and own(e)==row.target.unit_number
            and (e.name=="boiler" or e.name=="stone-furnace"),"unsupported_fuel_target")'''
    if source.count(state_marker) != 1 or source.count(pending_marker) != 1:
        raise RuntimeError('Pinned first-spend query guards changed')
    source = source.replace(state_marker, prepared_state, 1)
    source = source.replace(pending_marker, prepared_pending, 1)
    terminal = '''local encoded=helpers.table_to_json(out)
if #encoded>262144 then
    encoded=helpers.table_to_json{schema=out.schema,query_status="unsupported",reason="response_bound"}
end
rcon.print(encoded)'''
    action_terminal = '''local rt=jev_fle_runtime
local action={status="not_dispatched",reason="native_query_or_admission_unqualified"}
local attempt={phase="not_dispatched"}
if out.query_status=="observed" and out.coal_admission
    and out.coal_admission.qualified==true then
    local journal=rt.coal_native_admission_journal_v1
    if not journal then
        journal={schema="jev.native-coal-admission-journal.v1",protocol=1,
            session_id=out.epoch.session_id,actor_unit=out.epoch.actor_unit,
            native_profile=out.material_census.native_profile,
            guard_asset_sha256=out.coal_admission.source_asset_sha256,
            builder_asset_sha256=__COAL_SUPPLY_ASSET_SHA256__,
            legacy_manual_history=out.manual_cycle,
            order={},rows={}}
        rt.coal_native_admission_journal_v1=journal
    end
    need(journal.schema=="jev.native-coal-admission-journal.v1"
        and journal.protocol==1 and journal.session_id==out.epoch.session_id
        and journal.actor_unit==out.epoch.actor_unit
        and journal.native_profile==out.material_census.native_profile
        and journal.guard_asset_sha256==out.coal_admission.source_asset_sha256
        and journal.builder_asset_sha256==__COAL_SUPPLY_ASSET_SHA256__
        and journal.legacy_manual_history
        and journal.legacy_manual_history.status=="observed"
        and journal.legacy_manual_history.reason=="qualified_journal_rows"
        and type(journal.order)=="table" and type(journal.rows)=="table",
        "coal_admission_journal_identity")
    need(#journal.order<32 and journal.rows[coal_native_request.receipt]==nil,
        "coal_admission_receipt_reused_or_journal_full")
    local entry={schema="jev.native-coal-admission-attempt.v1",
        phase="dispatching",session_id=out.epoch.session_id,
        actor_unit=out.epoch.actor_unit,target=coal_native_request.target,
        layout=coal_native_request.layout,part=coal_native_request.part,
        receipt=coal_native_request.receipt,tick=out.epoch.tick,
        source_asset_sha256=out.coal_admission.source_asset_sha256,
        builder_asset_sha256=__COAL_SUPPLY_ASSET_SHA256__,
        admission=out.coal_admission}
    journal.rows[entry.receipt]=entry
    journal.order[#journal.order+1]=entry.receipt
    attempt=entry
    local ok,result=pcall(function()
        return rt.campaign.build_coal_source(coal_native_request)
    end)
    if not ok then entry.phase="unknown";error(result) end
    local source_row=q.rows[coal_native_request.target]
    local paid=source_row and source_row.parts
        and source_row.parts[coal_native_request.part]
    local expected_role="coal:"..coal_native_request.target..":"
        ..coal_native_request.part
    if result.placed~=true or not paid or paid.receipt~=entry.receipt
        or paid.paid~=1 or not paid.entity or not paid.entity.valid
        or paid.role~=expected_role or rt.campaign.entities[expected_role]~=paid.entity
        or paid.unit_number~=paid.entity.unit_number then
        entry.phase="unknown"
        error("coal_admission_payment_readback_ambiguous")
    end
    entry.phase="paid"
    entry.paid={role=paid.role,unit_number=paid.unit_number,paid=paid.paid,
        tick=game.tick}
    action={status="placed",result={placed=true}}
    attempt={schema=entry.schema,phase=entry.phase,session_id=entry.session_id,
        actor_unit=entry.actor_unit,target=entry.target,layout=entry.layout,
        part=entry.part,receipt=entry.receipt,tick=entry.tick,
        source_asset_sha256=entry.source_asset_sha256,
        builder_asset_sha256=entry.builder_asset_sha256,admission=entry.admission,
        paid=entry.paid}
end
local envelope=helpers.table_to_json({schema="jev.coal-native-first-payment.v1",
    query_status=out.query_status,reason=out.reason,epoch=out.epoch,
    admission=out.coal_admission or {},action=action,attempt=attempt})
assert(#envelope<=65536,"coal_admission_response_bound")
rcon.print(envelope)'''
    if action_terminal.count('__COAL_SUPPLY_ASSET_SHA256__') != 3:
        raise RuntimeError('Pinned builder asset markers changed')
    action_terminal = action_terminal.replace(
        '__COAL_SUPPLY_ASSET_SHA256__', json.dumps(NATIVE_COAL_SUPPLY_ASSET_SHA256))
    if source.count(terminal) != 1:
        raise RuntimeError('Pinned first-spend response boundary changed')
    return source.replace(terminal, action_terminal, 1)


def _compose_query(*, first_spend: bool = False) -> str:
    root = files('jev_factorio').joinpath('lua')
    base = root.joinpath('coal_economics.lua').read_text()
    # This query authorizes only its original installed builder. Completion
    # inspection adds helpers to the current direct-install asset; that must
    # not silently migrate the frozen v7 payment contract or its asset pin.
    coal_supply = root.joinpath('coal_supply_v7.lua').read_bytes()
    census = root.joinpath('coal_material_census_v7.lua').read_text()
    cycle = root.joinpath('coal_manual_cycle_v2.lua').read_bytes()
    admission = root.joinpath('coal_native_admission_v1.lua').read_text()
    if (hashlib.sha256(base.encode('utf-8')).hexdigest() != BASE_SOURCE_SHA256
            or hashlib.sha256(census.encode('utf-8')).hexdigest() != V7_CENSUS_SOURCE_SHA256
            or hashlib.sha256(admission.encode('utf-8')).hexdigest() != NATIVE_ADMISSION_SOURCE_SHA256
            or hashlib.sha256(coal_supply).hexdigest() != NATIVE_COAL_SUPPLY_ASSET_SHA256
            or hashlib.sha256(cycle).hexdigest() != CYCLE_SOURCE_SHA256
            or base.count(MARKER) != 1
            or base.count('jev.coal-native-economics.v5') != 1
            or base.count(V5_PROFILE) != 1
            or census.count('__CYCLE_ASSET_SHA256__') != 1
            or census.count('__ADMISSION_ASSET_SHA256__') != 2
            or census.count('__COAL_SUPPLY_ASSET_SHA256__') != 2
            or admission.count('__COAL_SUPPLY_ASSET_SHA256__') != 1):
        raise RuntimeError('Fixed native economics source changed before v7 composition')
    census = census.replace('__CYCLE_ASSET_SHA256__', CYCLE_SOURCE_SHA256)
    census = census.replace('__ADMISSION_ASSET_SHA256__', NATIVE_ADMISSION_SOURCE_SHA256)
    census = census.replace('__COAL_SUPPLY_ASSET_SHA256__',
                            NATIVE_COAL_SUPPLY_ASSET_SHA256)
    admission = admission.replace('__COAL_SUPPLY_ASSET_SHA256__',
                                  NATIVE_COAL_SUPPLY_ASSET_SHA256)
    call = ('out.coal_admission=coal_native_admission_v1(out,actor,player,c,q,coal_native_request,'
            + json.dumps(NATIVE_ADMISSION_SOURCE_SHA256) + ')'
            if first_spend else
            'out.coal_admission=coal_native_admission_v1(out,actor,player,c,q,nil,'
            + json.dumps(NATIVE_ADMISSION_SOURCE_SHA256) + ')')
    composed_census = census + '\n' + admission + '\n' + MARKER + '\n' + call
    return (base.replace('jev.coal-native-economics.v5', SCHEMA, 1)
            .replace(MARKER, composed_census, 1))


def query_sha256() -> str:
    return hashlib.sha256(fixed_query().encode('utf-8')).hexdigest()


def fixed_admission_journal_readback_query() -> str:
    """Return a read-only verifier for the same-RPC admission/payment journal.

    This query remains usable after the first payment, when the legacy v5
    economics query correctly refuses to reclassify a committed bundle. It
    never re-runs admission or calls a builder; it verifies the exact v7 guard
    hash, retained native profile/history, one-use receipts, and current paid
    ownership from the already installed campaign state.
    """
    from .backends.native_attachment import (
        CLOSED_WORLD_PROFILE, MANUAL_CYCLE_PROFILE, cycle_journal_sha256,
        manual_journal_sha256,
    )

    replacements = {
        '__V5_PROFILE__': (json.dumps(MANUAL_CYCLE_PROFILE), 1),
        '__V6_PROFILE__': (json.dumps(CLOSED_WORLD_PROFILE), 1),
        '__MANUAL_JOURNAL_SHA256__': (json.dumps(manual_journal_sha256()), 2),
        '__CYCLE_JOURNAL_SHA256__': (json.dumps(cycle_journal_sha256()), 1),
        '__GUARD_SHA256__': (json.dumps(NATIVE_ADMISSION_SOURCE_SHA256), 2),
        '__COAL_SUPPLY_ASSET_SHA256__': (json.dumps(NATIVE_COAL_SUPPLY_ASSET_SHA256), 3),
    }
    source = r'''-- Read-only verification only: no admission, builder, or runtime writes.
local function need(ok,code) if not ok then error(code,0) end end
local function integer(x,lo,hi)
    return type(x)=="number" and x==x and x%1==0 and x>=lo and x<=hi
end
local function bounded(t,n)
    need(type(t)=="table","journal_table_unavailable")
    local size=0;for _ in pairs(t) do size=size+1;need(size<=n,"journal_bound") end
    return t
end
local function sequence(t,n)
    bounded(t,n);local count=0
    for key in pairs(t) do need(integer(key,1,n),"journal_array_key");count=count+1 end
    for i=1,count do need(t[i]~=nil,"journal_sparse_array") end
    return t
end
local rt=jev_fle_runtime
need(rt and rt.campaign and rt.coal_supply and rt.native_installation,
    "journal_runtime_unavailable")
local c,q,n=rt.campaign,rt.coal_supply,rt.native_installation
local actor=rt.agent_characters and rt.agent_characters[1]
local index=rt.jev_bound_player_index
local player=integer(index,1,1000000) and game.get_player(index) or nil
need(player and player.connected and actor and actor.valid and player.character==actor
    and #game.connected_players==1 and game.connected_players[1]==player
    and game.speed==1 and not game.tick_paused,
    "journal_actor_binding")
need(script.active_mods.base=="2.0.77","journal_engine_version")
local mod_count=0;for _ in pairs(script.active_mods) do mod_count=mod_count+1 end
need(mod_count==1,"journal_engine_mods")
local v5=n.profile==__V5_PROFILE__
local v6=n.profile==__V6_PROFILE__
need(v5 or v6,"journal_native_profile")
need(n.session_id==rt.jev_session_id and n.actor_unit==actor.unit_number
    and n.assets and n.assets.coal_manual_journal_v1==__MANUAL_JOURNAL_SHA256__,
    "journal_native_binding")
    if v5 then
    need(n.assets.coal_manual_cycle_v2==nil and rt.coal_manual_cycle_v2==nil
        and n.callbacks and n.callbacks.cycle_tick==nil,"journal_v5_profile_changed")
else
    need(n.assets.coal_manual_cycle_v2==__CYCLE_JOURNAL_SHA256__
        and rt.coal_manual_cycle_v2 and n.callbacks
        and n.callbacks.cycle_tick==rt.coal_manual_cycle_v2.combined_tick_handler,
        "journal_v6_profile_changed")
    end
local solid=rt.solid_routes
need(n.assets.coal_supply==__COAL_SUPPLY_ASSET_SHA256__
    and solid and solid.implementation_revision==4 and solid.coal==q
    and solid.coal_api and type(q.prepare)=="function" and type(q.build)=="function"
    and c.prepare_coal_source==q.prepare and c.build_coal_source==q.build,
    "journal_builder_identity")
need(q.revision==4 and q.admission_evidence==true and q.committed==true
    and type(q.rows)=="table" and type(c.entities)=="table",
    "journal_coal_commitment")
local journal=rt.coal_native_admission_journal_v1
need(journal and journal.schema=="jev.native-coal-admission-journal.v1"
    and journal.protocol==1 and journal.session_id==rt.jev_session_id
    and journal.actor_unit==actor.unit_number and journal.native_profile==n.profile
    and journal.guard_asset_sha256==__GUARD_SHA256__
    and journal.builder_asset_sha256==__COAL_SUPPLY_ASSET_SHA256__
    and type(journal.legacy_manual_history)=="table"
    and journal.legacy_manual_history.status=="observed"
    and journal.legacy_manual_history.reason=="qualified_journal_rows"
    and journal.legacy_manual_history.journal_asset_sha256==__MANUAL_JOURNAL_SHA256__,
    "journal_identity")
local history=journal.legacy_manual_history
sequence(history.gathers,32);sequence(history.deliveries,128)
local manual=rt.coal_manual_journal_v1
need(manual and manual.protocol==1 and manual.session_id==rt.jev_session_id
    and manual.actor_unit==actor.unit_number
    and n.callbacks and n.callbacks.journal_tick==manual.tick_handler,
    "journal_legacy_gather_owner")
sequence(manual.order,32);bounded(manual.rows,32)
need(#manual.order>=#history.gathers,"journal_legacy_gather_prefix")
for index,row in ipairs(history.gathers) do
    local receipt=row.receipt
    local current=manual.rows[receipt]
    need(manual.order[index]==receipt and current and current.receipt==receipt
        and current.status=="complete" and current.pending==false
        and current.overflow==false and current.fault==false
        and current.session_id==rt.jev_session_id and current.actor_unit==actor.unit_number
        and current.started_tick==row.started_tick
        and current.finished_tick==row.finished_tick
        and current.coal_before==row.coal_before and current.coal_after==row.coal_after
        and current.walking_ticks==row.walking_ticks
        and current.mining_ticks==row.mining_ticks,
        "journal_legacy_gather_changed")
end
sequence(c.receipt_order,128);bounded(c.receipts,128)
local last_delivery_index=0
for _,row in ipairs(history.deliveries) do
    local current=c.receipts[row.receipt]
    local found
    for index,receipt in ipairs(c.receipt_order) do
        if receipt==row.receipt then found=index;break end
    end
    need(found and found>last_delivery_index and current
        and current.item=="coal" and current.extracting==false
        and current.role==row.role and current.unit_number==row.unit
        and current.quantity==row.coal and current.tick==row.tick,
        "journal_legacy_delivery_changed")
    last_delivery_index=found
end
sequence(journal.order,32);bounded(journal.rows,32)
local attempts={};local seen={}
for _,receipt in ipairs(journal.order) do
    need(type(receipt)=="string" and #receipt==64 and not receipt:find("[^0-9a-f]")
        and not seen[receipt],"journal_receipt_identity")
    seen[receipt]=true
    local row=journal.rows[receipt]
    need(row and row.schema=="jev.native-coal-admission-attempt.v1"
        and row.receipt==receipt and row.session_id==rt.jev_session_id
        and row.actor_unit==actor.unit_number and row.source_asset_sha256==__GUARD_SHA256__
        and row.builder_asset_sha256==__COAL_SUPPLY_ASSET_SHA256__
        and (row.phase=="dispatching" or row.phase=="unknown" or row.phase=="paid")
        and type(row.target)=="string" and type(row.layout)=="string"
        and row.part=="chest" and integer(row.tick,0,game.tick)
        and row.admission and row.admission.schema=="jev.coal-native-admission.v1"
        and row.admission.qualified==true
        and row.admission.source_asset_sha256==row.source_asset_sha256
        and row.admission.session_id==row.session_id
        and row.admission.actor_unit==row.actor_unit
        and row.admission.tick==row.tick,
        "journal_attempt_identity")
    local source_row=q.rows[row.target]
    local part=source_row and source_row.parts and source_row.parts[row.part]
    local current=false
    if part then
        local entity=c.entities[part.role]
        need(part.role=="coal:"..row.target..":"..row.part
            and part.receipt==receipt and part.paid==1 and entity and entity.valid
            and part.entity==entity and part.unit_number==entity.unit_number,
            "journal_current_payment_identity")
        current={role=part.role,unit_number=part.unit_number,paid=part.paid}
    end
    if row.phase=="paid" then
        need(current and row.paid and row.paid.role==current.role
            and row.paid.unit_number==current.unit_number
            and row.paid.paid==current.paid and integer(row.paid.tick,row.tick,game.tick),
            "journal_paid_readback")
    else
        need(row.paid==nil,"journal_ambiguous_paid_field")
    end
    attempts[#attempts+1]={schema=row.schema,phase=row.phase,
        session_id=row.session_id,actor_unit=row.actor_unit,target=row.target,
        layout=row.layout,part=row.part,receipt=row.receipt,tick=row.tick,
        source_asset_sha256=row.source_asset_sha256,admission=row.admission,
        builder_asset_sha256=row.builder_asset_sha256,
        paid=row.paid or false,current_payment=current}
end
local count=0;for receipt in pairs(journal.rows) do
    count=count+1;need(seen[receipt],"journal_orphan_attempt")
end
need(count==#attempts and count==#journal.order,"journal_attempt_count")
local out={schema="jev.native-coal-admission-readback.v1",status="observed",
    legacy_history_preserved=true,
    epoch={session_id=rt.jev_session_id,tick=game.tick,actor_index=index,
        actor_unit=actor.unit_number,surface_index=actor.surface.index,
        force_index=actor.force.index},native_profile=n.profile,
    guard_asset_sha256=journal.guard_asset_sha256,
    builder_asset_sha256=journal.builder_asset_sha256,
    legacy_manual_history=history,rows=attempts}
local encoded=helpers.table_to_json(out)
if #encoded>262144 then error("journal_readback_response_bound",0) end
rcon.print(encoded)'''
    for marker, (value, count) in replacements.items():
        if source.count(marker) != count:
            raise RuntimeError('Admission readback template marker changed')
        source = source.replace(marker, value)
    return source


def decode_admission_journal_readback(raw: dict, *, expected_epoch: dict,
                                      expected_native_profile: str) -> dict:
    """Validate read-only paid/ambiguous admission evidence after commitment."""
    fields = {'schema', 'status', 'legacy_history_preserved', 'epoch', 'native_profile',
              'guard_asset_sha256', 'builder_asset_sha256',
              'legacy_manual_history', 'rows'}
    epoch_fields = {'session_id', 'tick', 'actor_index', 'actor_unit',
                    'surface_index', 'force_index'}
    require(isinstance(raw, dict) and set(raw) == fields
            and raw['schema'] == 'jev.native-coal-admission-readback.v1'
            and raw['status'] == 'observed'
            and raw['legacy_history_preserved'] is True
            and raw['native_profile'] == expected_native_profile
            and raw['native_profile'] in {V5_OBSERVER_PROFILE, V6_OBSERVER_PROFILE}
            and raw['guard_asset_sha256'] == NATIVE_ADMISSION_SOURCE_SHA256
            and raw['builder_asset_sha256'] == NATIVE_COAL_SUPPLY_ASSET_SHA256,
            'coal_native_admission_readback_fields')
    epoch = raw['epoch']
    require(isinstance(epoch, dict) and set(epoch) == epoch_fields
            and type(epoch['session_id']) is str
            and all(type(epoch[key]) is int for key in epoch_fields - {'session_id'})
            and epoch['session_id'] == expected_epoch['session_id']
            and epoch['tick'] >= expected_epoch['tick']
            and all(epoch[key] == expected_epoch[key]
                    for key in ('actor_index', 'actor_unit', 'surface_index', 'force_index')),
            'coal_native_admission_readback_epoch')
    history = _legacy_manual_history(raw['legacy_manual_history'])
    journal = {'status': 'observed',
               'schema': 'jev.native-coal-admission-journal.v1',
               'protocol': 1, 'session_id': epoch['session_id'],
               'actor_unit': epoch['actor_unit'],
               'guard_asset_sha256': raw['guard_asset_sha256'],
               'builder_asset_sha256': raw['builder_asset_sha256'],
               'native_profile': raw['native_profile'],
               'legacy_manual_history': history, 'rows': raw['rows']}
    normalized = _admission_journal(journal, epoch, raw['native_profile'])
    return {**raw, 'legacy_manual_history': history,
            'rows': normalized['rows']}


def _legacy_manual_history(value):
    require(isinstance(value, dict)
            and set(value) == {'status', 'reason', 'journal_asset_sha256',
                               'gathers', 'deliveries'}
            and value['status'] == 'observed'
            and value['reason'] == 'qualified_journal_rows',
            'coal_legacy_manual_history_fields')
    from .backends.native_attachment import manual_journal_sha256
    require(value['journal_asset_sha256'] == manual_journal_sha256(),
            'coal_legacy_manual_history_asset')
    gathers = rows(value['gathers'], 0, 32)
    gather_receipts = set()
    for row in gathers:
        require(isinstance(row, dict)
                and set(row) == {'receipt', 'started_tick', 'finished_tick',
                                 'coal_before', 'coal_after', 'walking_ticks',
                                 'mining_ticks'}, 'coal_legacy_gather_fields')
        receipt = identity(row['receipt'])
        require(receipt not in gather_receipts, 'coal_legacy_gather_alias')
        gather_receipts.add(receipt)
        start = integer(row['started_tick'], 0)
        finish = integer(row['finished_tick'], start + 1)
        before = integer(row['coal_before'], 0, 200)
        integer(row['coal_after'], before + 1, 200)
        integer(row['walking_ticks'], 0, finish - start)
        integer(row['mining_ticks'], 1, finish - start)
    deliveries = rows(value['deliveries'], 0, 128)
    delivery_receipts = set()
    for row in deliveries:
        require(isinstance(row, dict)
                and set(row) == {'receipt', 'role', 'unit', 'coal', 'tick'},
                'coal_legacy_delivery_fields')
        receipt = identity(row['receipt'])
        require(receipt not in delivery_receipts, 'coal_legacy_delivery_alias')
        delivery_receipts.add(receipt)
        identity(row['role'])
        integer(row['unit'], 1)
        integer(row['coal'], 1, 200)
        integer(row['tick'], 0)
    return {**value, 'gathers': gathers, 'deliveries': deliveries}


@dataclass(frozen=True)
class GroundStack:
    unit: int
    name: str
    count: int
    position: tuple[float, float]
    fuel_value: int
    fuel_category: str


@dataclass(frozen=True)
class TreeProduct:
    name: str
    minimum: int
    maximum: int
    expected_amount: float
    fuel_value: int
    fuel_category: str
    probability: float


@dataclass(frozen=True)
class TreeStock:
    unit: int
    name: str
    position: tuple[float, float]
    mining_time: float
    products: tuple[TreeProduct, ...]


@dataclass(frozen=True)
class AlternativeStock:
    actor_position: tuple[float, float]
    manual_mining_speed: float
    ground_items: tuple[GroundStack, ...]
    trees: tuple[TreeStock, ...]
    tree_product_upper_bounds: tuple[tuple[str, int, int, str], ...]
    owned_inventory_counts: tuple[tuple[str, int], ...]
    copper_consumer_position: tuple[float, float]
    radius_tiles: int = 64
    local_scope_only: bool = True
    observed_generated_entities_only: bool = True
    unobserved_chunks_inside_scope_unknown: bool = True
    current_surface_only: bool = True
    current_surface_coverage_complete: bool = False
    unobserved_current_surface_outside_scope: bool = True
    future_generated_chunks_unknown: bool = True


@dataclass(frozen=True)
class V7Observation:
    graph: NativeEconomics
    census: Census
    cycles: tuple[CycleRow, ...]
    alternatives: AlternativeStock
    native_admission: dict
    raw_sha256: str
    profile: str = PROFILE
    cycle_history_coverage: str = 'native_v2'
    native_admission_journal: dict | None = None
    native_payback_proven: bool = False
    mutation_authorized: bool = False


def _item_fuel(row, known):
    fuel_value = integer(row['fuel_value'], 0, 10**12)
    category = row['fuel_category']
    require(type(category) is str and len(category) <= 128
            and all(32 <= ord(char) <= 126 for char in category),
            'material_census_fuel_category')
    previous = known.setdefault(row['name'], (fuel_value, category))
    require(previous == (fuel_value, category), 'material_census_fuel_prototype_mismatch')
    return fuel_value, category


def _alternatives(raw):
    value = raw['material_census']
    require(isinstance(value, dict)
            and set(value) == set(('status reason owned_surface_coverage_complete actor_unit '
                'native_profile cycle_asset_sha256 admission_journal actor_items entities actor_inventories ground_items trees '
                'crafting_queue actor_position manual_mining_speed alternative_stock').split())
            and value['status'] == 'observed'
            and value['reason'] == 'bounded_owned_and_local_alternatives_v7'
            and value['owned_surface_coverage_complete'] is True
            and value['actor_unit'] == raw['epoch']['actor_unit']
            and ((value['native_profile'] == V5_OBSERVER_PROFILE
                  and value['cycle_asset_sha256'] is False)
                 or (value['native_profile'] == V6_OBSERVER_PROFILE
                     and value['cycle_asset_sha256'] == CYCLE_SOURCE_SHA256))
            and type(value['crafting_queue']) is int and value['crafting_queue'] == 0,
            'material_census_v7_unqualified')
    actor_position = point(value['actor_position'])
    manual_mining_speed = number(value['manual_mining_speed'], 0.001, 1000)
    alt = value['alternative_stock']
    require(isinstance(alt, dict)
            and set(alt) == {'status', 'scope', 'radius_tiles',
                             'copper_consumer_position',
                             'observed_generated_entities_only',
                             'unobserved_chunks_inside_scope_unknown',
                             'unobserved_current_surface_outside_scope',
                             'future_generated_chunks_unknown',
                             'ground_item_entities', 'trees', 'tree_product_upper_bounds',
                             'ground_item_count', 'tree_count', 'owned_inventory_counts'}
            and alt['status'] == 'observed'
            and alt['scope'] == 'actor_and_copper_consumer_radius_64'
            and type(alt['radius_tiles']) is int and alt['radius_tiles'] == 64
            and alt['observed_generated_entities_only'] is True
            and alt['unobserved_chunks_inside_scope_unknown'] is True
            and alt['unobserved_current_surface_outside_scope'] is True
            and alt['future_generated_chunks_unknown'] is True,
            'material_census_alternative_scope')
    consumer_position = point(alt['copper_consumer_position'])
    research = raw.get('research_work')
    research_targets = (rows(research.get('targets', []), 0, 3)
                        if isinstance(research, dict)
                        and research.get('status') == 'observed' else [])
    copper_targets = [row for row in research_targets
                      if isinstance(row, dict) and row.get('recipe') == 'copper-plate']
    if copper_targets:
        require(len(copper_targets) == 1, 'material_census_consumer_alias')
        role = identity(copper_targets[0].get('role'))
        registry = rows(raw['registry'], 1, 2048)
        matches = [row for row in registry
                   if isinstance(row, dict) and row.get('role') == role]
        require(len(matches) == 1
                and point(matches[0]['position']) == consumer_position,
                'material_census_consumer_position_mismatch')

    def in_local_scope(position):
        return any(max(abs(position[0] - center[0]),
                       abs(position[1] - center[1])) <= alt['radius_tiles']
                   for center in (actor_position, consumer_position))

    known = {}
    ground = []
    ground_rows = rows(alt['ground_item_entities'], 0, 512)
    require(value['ground_items'] == len(ground_rows)
            and alt['ground_item_count'] == len(ground_rows),
            'material_census_ground_count_mismatch')
    previous_unit = 0
    ground_total = 0
    for row in ground_rows:
        require(isinstance(row, dict)
                and set(row) == {'unit', 'name', 'count', 'position',
                                 'fuel_value', 'fuel_category'},
                'material_census_ground_fields')
        unit = integer(row['unit'], 1)
        require(unit > previous_unit, 'material_census_ground_order')
        previous_unit = unit
        name = identity(row['name'])
        count = integer(row['count'], 1, 200_000)
        position = point(row['position'])
        require(in_local_scope(position), 'material_census_ground_outside_scope')
        fuel_value, category = _item_fuel({**row, 'name': name}, known)
        ground_total += count
        require(ground_total <= 2_000_000, 'material_census_ground_bound')
        ground.append(GroundStack(unit, name, count, position,
                                  fuel_value, category))
    trees = []
    tree_rows = rows(alt['trees'], 0, 512)
    require(value['trees'] == len(tree_rows)
            and alt['tree_count'] == len(tree_rows),
            'material_census_tree_count_mismatch')
    previous_unit = 0
    aggregate = {}
    for row in tree_rows:
        require(isinstance(row, dict)
                and set(row) == {'unit', 'name', 'position', 'mining_time', 'products'},
                'material_census_tree_fields')
        unit = integer(row['unit'], 1)
        require(unit > previous_unit, 'material_census_tree_order')
        previous_unit = unit
        name = identity(row['name'])
        position = point(row['position'])
        require(in_local_scope(position), 'material_census_tree_outside_scope')
        mining_time = number(row['mining_time'], 0, 1_000_000)
        products = []
        product_rows = rows(row['products'], 0, 16)
        product_names = []
        for product in product_rows:
            require(isinstance(product, dict)
                    and set(product) == {'name', 'minimum', 'maximum',
                                         'expected_amount', 'fuel_value',
                                         'fuel_category', 'probability'},
                    'material_census_tree_product_fields')
            item = identity(product['name'])
            minimum = integer(product['minimum'], 1, 200_000)
            maximum = integer(product['maximum'], 1, 200_000)
            require(maximum >= minimum, 'material_census_tree_yield_range')
            probability = number(product['probability'], 0, 1)
            expected_amount = number(product['expected_amount'], 0, 200_000)
            require(expected_amount == ((minimum + maximum) / 2) * probability,
                    'material_census_tree_expected_yield')
            fuel_value, category = _item_fuel({**product, 'name': item}, known)
            product_names.append(item)
            aggregate[item] = aggregate.get(item, 0) + maximum
            require(aggregate[item] <= 2_000_000,
                    'material_census_tree_yield_bound')
            products.append(TreeProduct(item, minimum, maximum, expected_amount,
                                        fuel_value, category, probability))
        require((mining_time > 0) == bool(products),
                'material_census_tree_mining_time')
        require(product_names == sorted(set(product_names)),
                'material_census_tree_product_order')
        trees.append(TreeStock(unit, name, position, mining_time,
                               tuple(products)))
    summary = []
    for row in rows(alt['tree_product_upper_bounds'], 0, 128):
        require(isinstance(row, dict)
                and set(row) == {'name', 'maximum', 'fuel_value', 'fuel_category'},
                'material_census_tree_summary_fields')
        name = identity(row['name'])
        maximum = integer(row['maximum'], 1, 2_000_000)
        fuel_value, category = _item_fuel({**row, 'name': name}, known)
        summary.append((name, maximum, fuel_value, category))
    require(summary == sorted(summary)
            and {name: maximum for name, maximum, _, _ in summary} == aggregate,
            'material_census_tree_summary_mismatch')
    owned_counts = []
    owned_names = []
    for row in rows(alt['owned_inventory_counts'], 0, 128):
        require(isinstance(row, dict) and set(row) == {'name', 'count'},
                'material_census_owned_stock_fields')
        name = identity(row['name'])
        count = integer(row['count'], 1, 2_000_000)
        owned_names.append(name)
        owned_counts.append((name, count))
    require(owned_names == sorted(set(owned_names)),
            'material_census_owned_stock_order')
    return AlternativeStock(actor_position, manual_mining_speed, tuple(ground),
                            tuple(trees), tuple(summary), tuple(owned_counts),
                            consumer_position)


def _admission_journal(value, expected_epoch, expected_profile=None):
    if value is False:
        return None
    require(isinstance(value, dict)
            and set(value) == {'status', 'schema', 'protocol', 'session_id',
                               'actor_unit', 'guard_asset_sha256',
                               'builder_asset_sha256', 'native_profile',
                               'legacy_manual_history', 'rows'}
            and value['status'] == 'observed'
            and value['schema'] == 'jev.native-coal-admission-journal.v1'
            and value['protocol'] == 1
            and value['session_id'] == expected_epoch['session_id']
            and value['actor_unit'] == expected_epoch['actor_unit']
            and value['guard_asset_sha256'] == NATIVE_ADMISSION_SOURCE_SHA256
            and value['builder_asset_sha256'] == NATIVE_COAL_SUPPLY_ASSET_SHA256
            and value['native_profile'] in {V5_OBSERVER_PROFILE, V6_OBSERVER_PROFILE}
            and (expected_profile is None or value['native_profile'] == expected_profile),
            'coal_native_admission_journal_unqualified')
    legacy_history = _legacy_manual_history(value['legacy_manual_history'])
    attempts = rows(value['rows'], 0, 32)
    seen = set()
    normalized = []
    for attempt in attempts:
        require(isinstance(attempt, dict)
                and set(attempt) == {'schema', 'phase', 'session_id', 'actor_unit',
                    'target', 'layout', 'part', 'receipt', 'tick',
                    'source_asset_sha256', 'builder_asset_sha256',
                    'admission', 'paid', 'current_payment'}
                and attempt['schema'] == 'jev.native-coal-admission-attempt.v1'
                and attempt['phase'] in {'dispatching', 'unknown', 'paid'}
                and attempt['session_id'] == expected_epoch['session_id']
                and attempt['actor_unit'] == expected_epoch['actor_unit']
                and attempt['source_asset_sha256'] == NATIVE_ADMISSION_SOURCE_SHA256
                and attempt['builder_asset_sha256'] == NATIVE_COAL_SUPPLY_ASSET_SHA256
                and attempt['part'] == 'chest',
                'coal_native_admission_attempt_fields')
        receipt = identity(attempt['receipt'])
        target = identity(attempt['target'])
        identity(attempt['layout'])
        require(receipt not in seen, 'coal_native_admission_receipt_alias')
        seen.add(receipt)
        tick = integer(attempt['tick'], 0, expected_epoch['tick'])
        proof_epoch = {'session_id': attempt['session_id'],
                       'actor_unit': attempt['actor_unit'], 'tick': tick}
        proof = _native_admission(attempt['admission'], proof_epoch,
                                  NATIVE_ADMISSION_SOURCE_SHA256)
        require(proof.get('qualified') is True,
                'coal_native_admission_attempt_proof')
        paid = attempt['paid']
        current = attempt['current_payment']
        if attempt['phase'] == 'paid':
            require(isinstance(paid, dict)
                    and set(paid) == {'role', 'unit_number', 'paid', 'tick'}
                    and identity(paid['role'])
                    and paid['role'] == f"coal:{target}:{attempt['part']}"
                    and integer(paid['unit_number'], 1)
                    and paid['paid'] == 1
                    and integer(paid['tick'], tick, expected_epoch['tick'])
                    and isinstance(current, dict)
                    and set(current) == {'role', 'unit_number', 'paid'}
                    and current == {key: paid[key] for key in ('role', 'unit_number', 'paid')},
                    'coal_native_admission_paid_receipt')
        else:
            require(paid is False
                    and (current is False or (isinstance(current, dict)
                         and set(current) == {'role', 'unit_number', 'paid'}
                         and identity(current['role'])
                         and current['role'] == f"coal:{target}:{attempt['part']}"
                         and integer(current['unit_number'], 1)
                         and current['paid'] == 1)),
                    'coal_native_admission_unresolved_receipt')
        normalized.append({**attempt, 'admission': proof})
    return {**value, 'legacy_manual_history': legacy_history,
            'rows': normalized}


def decode_v7(raw: dict, *, expected_epoch: dict, expected_bundle: dict,
              unit_qualification: dict, expected_connectors=None,
              expected_routes=None, expected_journal_asset_sha256=None) -> V7Observation:
    """Validate v7 alternatives and reuse the exact immutable v6 graph/cycle checks."""
    from .coal_economic_v6 import SCHEMA as V6_SCHEMA

    if not isinstance(raw, dict) or raw.get('schema') != SCHEMA:
        raise NativeEconomicsUnavailable('v7_native_projection_unsupported')
    alternatives = _alternatives(raw)
    admission = _native_admission(raw.get('coal_admission'), expected_epoch,
                                  NATIVE_ADMISSION_SOURCE_SHA256)
    if admission.get('qualified') is True:
        _validate_manual_service_forecast(alternatives, admission)
    value = raw['material_census']
    journal = _admission_journal(value['admission_journal'], expected_epoch,
                                  value['native_profile'])
    # The frozen v6 decoder is reused only for common graph, inventory and
    # receipt validation. The v7 alternative rows above are parsed independently;
    # normalized zero counts are confined to this internal v6 helper input.
    common = dict(raw)
    common.pop('coal_admission', None)
    common['schema'] = V6_SCHEMA
    legacy_v1_history = value['native_profile'] == V5_OBSERVER_PROFILE
    cycle_state = raw.get('cycle_v2')
    if legacy_v1_history:
        require(isinstance(cycle_state, dict)
                and set(cycle_state) == {'status', 'journal_asset_sha256',
                                         'cycle_complete', 'rows'}
                and cycle_state['status'] == 'not_installed'
                and cycle_state['journal_asset_sha256'] is False
                and cycle_state['cycle_complete'] is False
                and cycle_state['rows'] in ([], {}),
                'material_census_v7_legacy_cycle_state')
    else:
        require(isinstance(cycle_state, dict)
                and cycle_state.get('status') == 'observed'
                and cycle_state.get('journal_asset_sha256') == CYCLE_SOURCE_SHA256,
                'material_census_v7_cycle_state')
    census = dict(value)
    census.pop('actor_position')
    census.pop('manual_mining_speed')
    census.pop('alternative_stock')
    census.pop('native_profile')
    census.pop('admission_journal')
    census.update(reason='bounded_owned_surface', ground_items=0, trees=0)
    common['material_census'] = census
    decoded: V6Observation = decode_v6(
        common, expected_epoch=expected_epoch, expected_bundle=expected_bundle,
        unit_qualification=unit_qualification, expected_connectors=expected_connectors,
        expected_routes=expected_routes,
        expected_journal_asset_sha256=expected_journal_asset_sha256,
        allow_uninstalled_cycle_v2=legacy_v1_history)
    raw_sha256 = digest(raw)
    return V7Observation(decoded.graph,
                         decoded.census,
                         decoded.cycles,
                         alternatives,
                         admission,
                         raw_sha256,
                         cycle_history_coverage=('legacy_v1_preserved'
                                                 if legacy_v1_history else 'native_v2'),
                         native_admission_journal=journal)


def _manual_service_estimate(alternatives, categories, needed_energy,
                             coal_energy_per_item, fastest_mining_time):
    """Mirror the native, explicitly estimated local manual-fuel policy."""
    accepted = set(categories)
    candidates = []
    for row in alternatives.ground_items:
        if row.fuel_value > 0 and row.fuel_category in accepted:
            energy = row.count * row.fuel_value
            candidates.append((60 / energy, 'ground', row.unit, row.position,
                               energy, 60))
    for tree in alternatives.trees:
        if tree.mining_time <= 0:
            continue
        energy = math.floor(sum(product.expected_amount * product.fuel_value
                                for product in tree.products
                                if product.fuel_value > 0
                                and product.fuel_category in accepted))
        if energy > 0:
            service = math.ceil(tree.mining_time * 60
                                / alternatives.manual_mining_speed)
            candidates.append((service / energy, 'tree', tree.unit,
                               tree.position, energy, service))
    candidates.sort(key=lambda row: (row[0], row[1], row[2]))
    remaining = needed_energy
    collection_ticks = collected_energy = source_count = 0
    position = alternatives.actor_position
    for _, _, _, next_position, energy, service in candidates:
        if remaining <= 0:
            break
        distance = abs(position[0] - next_position[0]) + abs(position[1] - next_position[1])
        collection_ticks += math.ceil(distance * 20) + service
        amount = min(remaining, energy)
        collected_energy += amount
        remaining = max(0, remaining - amount)
        position = next_position
        source_count += 1
    coal_units = math.ceil(remaining / coal_energy_per_item)
    coal_ticks = math.ceil(coal_units * fastest_mining_time * 60
                           / alternatives.manual_mining_speed)
    collection_route_ticks = collection_ticks + coal_ticks
    baseline_units = math.ceil(needed_energy / coal_energy_per_item)
    baseline_ticks = math.ceil(baseline_units * fastest_mining_time * 60
                               / alternatives.manual_mining_speed)
    if collection_route_ticks <= baseline_ticks:
        return {'service_ticks': collection_route_ticks,
                'collection_ticks': collection_ticks,
                'collected_energy': collected_energy,
                'source_count': source_count,
                'baseline_ticks': baseline_ticks,
                'baseline_units': baseline_units}
    return {'service_ticks': baseline_ticks, 'collection_ticks': 0,
            'collected_energy': 0, 'source_count': 0,
            'baseline_ticks': baseline_ticks,
            'baseline_units': baseline_units}


def _validate_manual_service_forecast(alternatives, proof):
    estimate = _manual_service_estimate(
        alternatives, proof['copper_fuel_categories'],
        proof['copper_residual_demand_joules_upper'],
        proof['copper_coal_fuel_joules_per_item_estimate'],
        proof['copper_fastest_coal_mining_time_seconds_estimate'])
    require(proof['manual_copper_coal_units_estimate'] == estimate['baseline_units']
            and proof['manual_copper_mining_ticks_estimate_no_walk']
                == estimate['baseline_ticks']
            and proof['manual_fuel_service_ticks_estimate'] == estimate['service_ticks']
            and proof['manual_local_collection_ticks_estimate'] == estimate['collection_ticks']
            and proof['manual_local_collection_fuel_joules_estimate']
                == estimate['collected_energy']
            and proof['manual_local_collection_source_count_estimate']
                == estimate['source_count'],
            'coal_native_admission_manual_service_mismatch')


def _native_admission(value, expected_epoch, expected_asset_sha256):
    reject_fields = {'schema', 'qualified', 'reason', 'source_asset_sha256', 'session_id', 'tick',
                     'actor_unit', 'details'}
    if (not isinstance(value, dict) or value.get('schema') != 'jev.coal-native-admission.v1'
            or type(value.get('qualified')) is not bool
            or type(value.get('reason')) is not str
            or value.get('source_asset_sha256') != expected_asset_sha256
            or value.get('session_id') != expected_epoch.get('session_id')
            or value.get('tick') != expected_epoch.get('tick')
            or value.get('actor_unit') != expected_epoch.get('actor_unit')):
        raise NativeEconomicsUnavailable('coal_native_admission_unbound')
    if value['qualified'] is False:
        require(set(value) == reject_fields and isinstance(value['details'], dict)
                and not value['details'], 'coal_native_admission_rejection_fields')
        require(value['reason'] and all(c in 'abcdefghijklmnopqrstuvwxyz_'
                                         for c in value['reason']),
                'coal_native_admission_rejection_reason')
        return dict(value)
    expected = set(('schema qualified reason session_id tick actor_unit technology '
                    'source_asset_sha256 '
                    'rocket_goal_path direct_target remaining_research_units research_demand '
                    'copper_plate_recipe_batches current_copper_ore_available '
                    'copper_plate_ore_input_required copper_plate_ore_input_available '
                    'productive_copper_recipe_ticks whole_construction_kit '
                    'whole_construction_kit_carried manual_copper_coal_units_estimate '
                    'current_placement_count current_placement_manhattan_distance_tiles_estimate '
                'placement_service_ticks_estimate placement_travel_ticks_estimate '
                'remaining_project_setup_ticks_estimate '
                'manual_copper_mining_ticks_estimate_no_walk '
                'manual_fuel_service_ticks_estimate manual_local_collection_ticks_estimate '
                'manual_local_collection_fuel_joules_estimate '
                'manual_local_collection_source_count_estimate manual_fuel_service_basis '
                'copper_fuel_categories copper_coal_fuel_joules_per_item_estimate '
                'copper_fastest_coal_mining_time_seconds_estimate '
                'existing_load_joules_per_tick planned_load_joules_per_tick '
                    'total_load_joules_per_tick generation_capacity_joules_per_tick '
                    'boiler_conversion_efficiency generator_conversion_efficiency '
                    'boiler_source_coal_per_tick_lower copper_source_coal_per_tick_lower '
                    'boiler_source_finite_ore copper_source_finite_ore '
                    'boiler_source_energy_joules_lower copper_source_energy_joules_lower '
                    'whole_project_electric_joules_upper buffer_energy_joules_upper '
                    'current_boiler_fuel_joules_lower current_copper_fuel_joules_lower '
                    'current_boiler_alternative_fuel_joules_upper '
                    'current_copper_alternative_fuel_joules_upper '
                    'boiler_residual_demand_joules_upper copper_residual_demand_joules_upper '
                    'current_surface_only local_scope_only local_alternative_scope '
                    'observed_generated_entities_only unobserved_chunks_inside_scope_unknown '
                    'unobserved_current_surface_outside_scope future_generated_chunks_unknown '
                    'time_to_completion_claimed mutation_authorized').split())
    require(set(value) == expected
            and value['source_asset_sha256'] == expected_asset_sha256
            and value['reason'] == 'current_research_direct_copper_fuel_and_setup_estimate_margin'
            and value['direct_target'] == 'recipe:copper-plate'
            and value['current_surface_only'] is True
            and value['local_scope_only'] is True
            and value['local_alternative_scope'] == 'actor_and_copper_consumer_radius_64'
            and value['observed_generated_entities_only'] is True
            and value['unobserved_chunks_inside_scope_unknown'] is True
            and value['unobserved_current_surface_outside_scope'] is True
            and value['future_generated_chunks_unknown'] is True
            and type(value['whole_construction_kit_carried']) is bool
            and value['time_to_completion_claimed'] is False
            and value['mutation_authorized'] is False,
            'coal_native_admission_qualified_fields')
    identity(value['technology'])
    path = rows(value['rocket_goal_path'], 1, 65)
    for name in path:
        identity(name)
    for key in ('remaining_research_units', 'copper_plate_recipe_batches',
                'productive_copper_recipe_ticks', 'boiler_source_finite_ore',
                'copper_source_finite_ore','copper_plate_ore_input_required',
                'manual_copper_coal_units_estimate', 'current_placement_count',
                'placement_service_ticks_estimate', 'placement_travel_ticks_estimate',
                'remaining_project_setup_ticks_estimate',
                'manual_copper_mining_ticks_estimate_no_walk',
                'manual_fuel_service_ticks_estimate',
                'manual_local_collection_ticks_estimate',
                'manual_local_collection_fuel_joules_estimate',
                'manual_local_collection_source_count_estimate'):
        low = 0 if key in {'manual_local_collection_ticks_estimate',
                           'manual_local_collection_fuel_joules_estimate',
                           'manual_local_collection_source_count_estimate'} else 1
        integer(value[key], low, 1_000_000_000)
    number(value['copper_coal_fuel_joules_per_item_estimate'], 0.001, 10**12)
    number(value['copper_fastest_coal_mining_time_seconds_estimate'], 0.001, 1_000_000)
    require(value['manual_fuel_service_basis']
            == 'local_expected_tree_yield_60_tick_pickup_20_tick_tiles_direct_coal_no_walk_v1',
            'coal_native_admission_manual_service_basis')
    categories = rows(value['copper_fuel_categories'], 1, 16)
    require(all(type(category) is str and 0 < len(category) <= 128
                and all(32 <= ord(char) <= 126 for char in category)
                for category in categories)
            and categories == sorted(set(categories)),
            'coal_native_admission_fuel_categories')
    number(value['current_placement_manhattan_distance_tiles_estimate'], 0, 2_000_000_000)
    require(value['current_placement_count'] <= 512
            and value['placement_service_ticks_estimate'] == value['current_placement_count'] * 300
            and value['placement_travel_ticks_estimate']
                == math.ceil(value['current_placement_manhattan_distance_tiles_estimate'] * 20)
            and value['remaining_project_setup_ticks_estimate']
                == value['placement_service_ticks_estimate'] + value['placement_travel_ticks_estimate']
            and value['manual_copper_mining_ticks_estimate_no_walk']
                > value['remaining_project_setup_ticks_estimate'],
            'coal_native_admission_setup_forecast')
    require(value['manual_fuel_service_ticks_estimate']
            > value['remaining_project_setup_ticks_estimate']
            and value['manual_local_collection_ticks_estimate']
                <= value['manual_fuel_service_ticks_estimate']
            and value['manual_local_collection_source_count_estimate'] <= 512,
            'coal_native_admission_manual_service_forecast')
    integer(value['current_copper_ore_available'], 0, 2**53 - 1)
    integer(value['copper_plate_ore_input_available'], 0, 2**53 - 1)
    require(value['copper_plate_ore_input_available']
            >= value['copper_plate_ore_input_required'],
            'coal_native_admission_current_target_stock')
    for key in ('existing_load_joules_per_tick', 'planned_load_joules_per_tick',
                'total_load_joules_per_tick', 'generation_capacity_joules_per_tick',
                'boiler_source_energy_joules_lower', 'copper_source_energy_joules_lower',
                'whole_project_electric_joules_upper', 'buffer_energy_joules_upper',
                'current_boiler_fuel_joules_lower', 'current_copper_fuel_joules_lower',
                'current_boiler_alternative_fuel_joules_upper',
                'current_copper_alternative_fuel_joules_upper',
                'boiler_residual_demand_joules_upper',
                'copper_residual_demand_joules_upper'):
        integer(value[key], 0, 2**53 - 1)
    for key in ('boiler_conversion_efficiency', 'generator_conversion_efficiency',
                'boiler_source_coal_per_tick_lower', 'copper_source_coal_per_tick_lower'):
        number(value[key], 0.000001, 1_000_000)
    require(value['total_load_joules_per_tick']
            == value['existing_load_joules_per_tick'] + value['planned_load_joules_per_tick']
            and value['total_load_joules_per_tick'] < value['generation_capacity_joules_per_tick']
            and value['boiler_source_energy_joules_lower']
                > value['boiler_residual_demand_joules_upper'] > 0
            and value['copper_source_energy_joules_lower']
                > value['copper_residual_demand_joules_upper'] > 0,
            'coal_native_admission_strict_margin')
    for key in ('research_demand', 'whole_construction_kit'):
        require(isinstance(value[key], dict) and len(value[key]) <= 128
                and all(identity(name) and integer(count, 1, 2_000_000)
                        for name, count in value[key].items()),
                'coal_native_admission_bill')
    return dict(value)


def alternative_stock_upper_bounds(observed: V7Observation, names: tuple[str, ...]) -> dict:
    """Report owned stock plus bounded local alternatives; never close terrain."""
    if (type(observed) is not V7Observation
            or observed.profile != PROFILE
            or observed.native_payback_proven is not False
            or observed.mutation_authorized is not False
            or observed.alternatives.local_scope_only is not True
            or observed.alternatives.current_surface_only is not True
            or observed.alternatives.current_surface_coverage_complete is not False
            or observed.alternatives.unobserved_current_surface_outside_scope is not True
            or observed.alternatives.future_generated_chunks_unknown is not True
            or type(names) is not tuple or not 1 <= len(names) <= 128
            or any(type(name) is not str for name in names)
            or tuple(sorted(set(names))) != names):
        raise ValueError('Coal v7 alternative stock has unqualified scope')
    base = diagnostic_stock_upper_bounds(observed_v6(observed), names)
    counts = dict(base['upper_counts'])
    alternative_fuel_joules = 0
    tree_costs = {}

    def add(name, amount, fuel_value):
        if name in counts:
            counts[name] += amount
            if counts[name] > 2**53 - 1:
                raise ValueError('Coal v7 alternative count overflow')

    for row in observed.alternatives.ground_items:
        add(row.name, row.count, row.fuel_value)
        alternative_fuel_joules += row.count * row.fuel_value
    for tree in observed.alternatives.trees:
        if tree.mining_time <= 0:
            continue
        mining_ticks = tree.mining_time * 60 / observed.alternatives.manual_mining_speed
        for product in tree.products:
            # Use the prototype-derived expected yield. Walking and drop
            # variance remain outside this diagnostic estimate.
            if product.expected_amount > 0:
                tree_costs.setdefault(product.name, []).append(
                    (mining_ticks, product.expected_amount))
    tree_ticks_per_item_estimate = {}
    for name, amount, fuel_value, _ in observed.alternatives.tree_product_upper_bounds:
        add(name, amount, fuel_value)
        alternative_fuel_joules += amount * fuel_value
        costs = tree_costs.get(name, ())
        if costs:
            tree_ticks_per_item_estimate[name] = min(
                ticks / expected for ticks, expected in costs)
    if alternative_fuel_joules > 2**53 - 1:
        raise ValueError('Coal v7 alternative fuel-energy overflow')
    return {'schema': 'jev.coal-alternative-stock-upper-bounds.v1',
            'native_raw_sha256': observed.raw_sha256,
            'session_id': observed.graph.epoch.session_id,
            'tick': observed.graph.epoch.tick,
            'local_scope_counts_upper': counts,
            'ground_and_tree_fuel_energy_joules_upper': alternative_fuel_joules,
            'tree_ticks_per_item_estimate': tree_ticks_per_item_estimate,
            'basis': 'owned_inventory_plus_actor_consumer_local_ground_plus_expected_tree_yields',
            'scope': 'actor_and_copper_consumer_radius_64',
            'local_scope_only': True,
            'observed_generated_entities_only': True,
            'unobserved_chunks_inside_scope_unknown': True,
            'current_surface_coverage_complete': False,
            'unobserved_current_surface_outside_scope': True,
            'future_generated_chunks_unknown': True,
            'native_payback_proven': False,
            'mutation_authorized': False}


def observed_v6(observed: V7Observation) -> V6Observation:
    """Create the common evidence view for the unchanged overlap-safe helper."""
    if type(observed) is not V7Observation:
        raise ValueError('Expected v7 native economics evidence')
    return V6Observation(observed.graph, observed.census, observed.cycles,
                         observed.raw_sha256, False, False)
