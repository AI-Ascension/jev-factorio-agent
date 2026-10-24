-- First base-2.0.77 rocket only. No grants, instant mining, settings changes or retries.
local campaign = assert(storage.campaign)
storage.launch_readiness = storage.launch_readiness or {schema=1,serial=0,attempts={},receipts={}}
local r = storage.launch_readiness
assert(r.schema==1, "Unsupported launch readiness runtime")
local silo_role,pad_role="recipe:rocket-part","utility:landing-pad"
local function point(p) return {x=p.x,y=p.y} end
local function counts(inv)
    local result={}
    if inv then for _,s in pairs(inv.get_contents()) do
        assert(s.quality==nil or s.quality=="normal", "Unsupported cargo quality")
        result[s.name]=(result[s.name] or 0)+s.count
    end end
    return result
end
local function supported()
    if script.active_mods.base~="2.0.77" then return false end
    for name in pairs(script.active_mods) do if name~="base" and name~="core" then return false end end
    return true
end
local function actor()
    assert(supported(), "Launch readiness requires unmodified base 2.0.77")
    local player=storage.fair.actor()
    assert(type(storage.jev_session_id)=="string" and storage.jev_session_id~="", "Missing launch session")
    if r.session_id then
        assert(r.session_id==storage.jev_session_id and r.actor_unit==player.character.unit_number
            and r.surface_index==player.surface.index and r.force_index==player.force.index,
            "Launch campaign identity changed; reconcile rather than reset")
    else
        r.session_id,r.actor_unit=storage.jev_session_id,player.character.unit_number
        r.surface_index,r.force_index=player.surface.index,player.force.index
    end
    return player
end
local function landing_pad(player)
    local rows=player.surface.find_entities_filtered{name="cargo-landing-pad",force=player.force,limit=2}
    assert(#rows<=1, "Ambiguous landing pad ownership")
    local entity=rows[1]
    if entity then
        assert(entity.valid and entity.force==player.force and entity.unit_number, "Invalid landing pad")
        if r.pad_unit then assert(entity.unit_number==r.pad_unit, "Paid landing pad changed") end
        return entity
    end
    assert(not r.pad_unit, "Paid landing pad disappeared")
end
local function destination_ready(pad,item)
    if not pad then return false end
    if item=="raw-fish" then return true end -- The pinned fish has no launch products.
    if item~="satellite" then return false end
    -- Base 2.0.77 satellites yield exactly 1000 space science. Check the
    -- actual pad's room; do not empty or enlarge an existing destination.
    local inventory=pad.get_inventory(defines.inventory.cargo_landing_pad_main)
    -- can_insert only proves that *some* of a stack fits. These normal,
    -- non-durable items use the basic inventory's full insertable count.
    return inventory and inventory.get_insertable_count("space-science-pack")>=1000 or false
end
local function silo_for(player)
    local silo=campaign.entities[silo_role]
    if not silo then return nil end
    assert(silo.valid and silo.name=="rocket-silo" and silo.unit_number
        and silo.surface==player.surface and silo.force==player.force, "Rocket silo identity invalid")
    if r.silo_unit then assert(silo.unit_number==r.silo_unit, "Rocket silo changed") end
    return silo
end
local function cargo_for(silo)
    if not silo then return nil,nil end
    local rocket=silo.rocket
    if not rocket or not rocket.valid then return nil,nil end
    assert(rocket.unit_number, "Rocket identity unavailable")
    return silo.get_inventory(defines.inventory.rocket_silo_rocket),rocket
end
local function cargo_item(inv)
    local cargo=counts(inv)
    local item
    for name,count in pairs(cargo) do
        assert((name=="raw-fish" or name=="satellite") and count==1 and item==nil,
            "Expected one payload unit; do not discard unexpected rocket cargo")
        item=name
    end
    return item
end
local function exact_fields(value, fields)
    if type(value)~="table" then return false end
    local expected,count={},0
    for _,field in ipairs(fields) do expected[field]=true end
    for field in pairs(value) do
        if type(field)~="string" or not expected[field] then return false end
        count=count+1
    end
    return count==#fields
end
local function integer(value)
    return type(value)=="number" and value>=0 and value==math.floor(value)
end
local function load_reconciled(silo,rocket,item)
    if type(r.attempts)~="table" or type(r.receipts)~="table" then return false end
    local attempt=r.attempts.load
    local matching,load_count=nil,0
    for receipt_id,receipt in pairs(r.receipts) do
        if type(receipt)=="table" and receipt.kind=="load" then
            load_count=load_count+1
            if type(attempt)=="table" and receipt_id==attempt.receipt then matching=receipt end
        end
    end
    if attempt==nil then return load_count==0 end
    if not exact_fields(attempt,{"receipt","silo_unit","rocket_unit","item","tick"})
        or type(attempt.receipt)~="string" or #attempt.receipt==0 or #attempt.receipt>128
        or not integer(attempt.silo_unit) or attempt.silo_unit<1
        or not integer(attempt.rocket_unit) or attempt.rocket_unit<1
        or type(attempt.item)~="string" or (attempt.item~="raw-fish" and attempt.item~="satellite")
        or not integer(attempt.tick) then return false end
    if load_count~=1 or not matching
        or not exact_fields(matching,{"kind","session_id","actor_unit","tick","item",
            "quantity","silo_unit","rocket_unit"}) then return false end
    return matching.kind=="load" and matching.session_id==r.session_id
        and matching.actor_unit==r.actor_unit and integer(matching.tick)
        and matching.tick==attempt.tick and matching.tick<=game.tick
        and matching.item==attempt.item and matching.item==item and matching.quantity==1
        and integer(matching.silo_unit) and matching.silo_unit==attempt.silo_unit
        and integer(matching.rocket_unit) and matching.rocket_unit==attempt.rocket_unit
        and silo and silo.unit_number==attempt.silo_unit
        and rocket and rocket.unit_number==attempt.rocket_unit
end
local function base_receipt(kind)
    return {kind=kind,session_id=r.session_id,actor_unit=r.actor_unit,tick=game.tick}
end
local function no_replay(kind,receipt)
    assert(type(receipt)=="string" and #receipt>0 and #receipt<=128, "Invalid launch receipt")
    assert(not r.attempts[kind] and not r.receipts[receipt], "Launch operation already attempted; reconcile")
end
local function reserve(player,item,count)
    local selected=player.get_item_count("raw-fish")>0 and "raw-fish"
        or (player.get_item_count("satellite")>0 and "satellite" or nil)
    if item==selected then
        local silo=silo_for(player)
        local inv=cargo_for(silo)
        if not cargo_item(inv) and not r.receipts.launch then
            assert(player.get_item_count(item)-count>=1, "Final launch payload is reserved")
        end
    end
end
campaign.launch_assert_spend=function(costs)
    if not supported() then return end
    local player=actor()
    for item,count in pairs(costs) do reserve(player,item,count) end
end
local function clear_site(player,p)
    return player.surface.is_chunk_generated{x=math.floor(p.x/32),y=math.floor(p.y/32)}
        and (not campaign.production_reserved or not campaign.production_reserved("cargo-landing-pad",p,0))
        and (not campaign.mining_outpost_reserved or not campaign.mining_outpost_reserved("cargo-landing-pad",p,0))
        and player.surface.can_place_entity{name="cargo-landing-pad",position=p,direction=0,
            force=player.force,build_check_type=defines.build_check_type.manual}
end
local function pad_offer(player)
    if r.attempts.pad then return nil end
    local old=r.pad_offer
    if old and clear_site(player,old.position) then return old end
    if player.get_item_count("cargo-landing-pad")<1 then return nil end
    -- The pinned pad uses build_grid_size=2. An odd player coordinate must
    -- not make every proposal off-grid (even though the pad is eight tiles wide).
    local cx,cy=math.floor(player.position.x/2)*2,math.floor(player.position.y/2)*2
    local best,distance
    -- 17x17 candidates in already generated local terrain. No map generation.
    for dx=-16,16,2 do for dy=-16,16,2 do
        local p={x=cx+dx,y=cy+dy}
        local cost=dx*dx+dy*dy
        if (not distance or cost<distance) and clear_site(player,p) then best,distance=p,cost end
    end end
    if not best then return nil end
    r.serial=r.serial+1;r.pad_offer={id="landing:"..r.serial,position=best}
    return r.pad_offer
end
local function fish_offer(player)
    if r.attempts.fish or player.get_item_count("raw-fish")>0 or player.get_item_count("satellite")>0 then return nil end
    local old=r.fish_offer
    if old and old.entity.valid and player.can_reach_entity(old.entity) then return old end
    local best,distance
    for _,fish in ipairs(player.surface.find_entities_filtered{type="fish",name="fish",
            position=player.position,radius=16,limit=16}) do
        if fish.valid and fish.minable and player.can_reach_entity(fish) then
            local d=(fish.position.x-player.position.x)^2+(fish.position.y-player.position.y)^2
            if not distance or d<distance then best,distance=fish,d end
        end
    end
    if not best then return nil end
    r.serial=r.serial+1;r.fish_offer={id="fish:"..r.serial,entity=best}
    return r.fish_offer
end
campaign.prepare_launch_pad=function(p)
    local player=actor();no_replay("pad",p.receipt)
    assert(not landing_pad(player), "Use the existing landing pad")
    assert(r.pad_offer and r.pad_offer.id==p.site and clear_site(player,r.pad_offer.position), "Landing site changed")
    assert(player.get_item_count("cargo-landing-pad")>=1, "Landing pad item missing")
    rcon.print(helpers.table_to_json({name="cargo-landing-pad",position=r.pad_offer.position}))
end
campaign.build_launch_pad=function(p)
    local player=actor();no_replay("pad",p.receipt)
    assert(not landing_pad(player) and r.pad_offer and r.pad_offer.id==p.site
        and clear_site(player,r.pad_offer.position), "Landing site no longer available")
    assert(not campaign.entities[pad_role], "Landing-pad role occupied")
    local position=r.pad_offer.position
    assert(player.get_item_count("cargo-landing-pad")>=1, "Missing paid landing pad")
    -- Intent precedes the engine mutation. A partial/unknown result cannot replay.
    r.attempts.pad={receipt=p.receipt,site=p.site,position=point(position),tick=game.tick}
    local built=storage.fair.place("cargo-landing-pad",position,0)
    local entity=player.surface.find_entity("cargo-landing-pad",position)
    assert(entity and entity.valid and entity.unit_number==built.unit_number, "Landing-pad placement ambiguous")
    campaign.entities[pad_role]=entity;r.pad_unit=entity.unit_number
    local receipt=base_receipt("pad");receipt.site=p.site;receipt.paid=1;receipt.unit_number=entity.unit_number
    r.receipts[p.receipt]=receipt
end
campaign.begin_launch_fish=function(p)
    local player=actor();no_replay("fish",p.receipt)
    local offer=r.fish_offer;local fish=offer and offer.entity
    assert(offer and offer.id==p.target and fish.valid and fish.minable and player.can_reach_entity(fish), "Fish left normal reach")
    assert(player.crafting_queue_size==0 and player.get_item_count("raw-fish")==0
        and player.get_item_count("satellite")==0, "Payload already available or craft active")
    assert(player.get_main_inventory().get_insertable_count("raw-fish")>=5, "No room for native fish yield")
    player.update_selected_entity(fish.position)
    assert(player.selected==fish, "Fish obscured")
    storage.fair.stop()
    r.attempts.fish={receipt=p.receipt,target=p.target,tick=game.tick}
    r.pending_fish={receipt=p.receipt,target=p.target,entity=fish,player_index=player.index}
    storage.fair.job={kind="mine",status="mining",lease=game.tick+180,
        unit=player.character.unit_number,entity=fish,item="raw-fish",baseline=0,quantity=5,
        last_progress=game.tick,last_count=0}
    player.mining_state={mining=true,position=fish.position}
end
local previous_mined=script.get_event_handler(defines.events.on_player_mined_entity)
if previous_mined==r.mined_handler then previous_mined=r.previous_mined end
r.previous_mined=previous_mined
r.mined_handler=function(event)
    local pending=r.pending_fish
    if pending and event.entity==pending.entity and event.player_index==pending.player_index then
        local ok,player=pcall(actor)
        if ok and event.entity.name=="fish" then
            local result=counts(event.buffer)
            if result["raw-fish"]==5 and next(result)=="raw-fish" and next(result,"raw-fish")==nil then
                local receipt=base_receipt("fish");receipt.target=pending.target;receipt.quantity=5
                r.receipts[pending.receipt]=receipt;r.pending_fish=nil
            end
        end
    end
    if previous_mined then previous_mined(event) end
end
script.on_event(defines.events.on_player_mined_entity,r.mined_handler)
campaign.load_launch_payload=function(p)
    local player=actor();no_replay("load",p.receipt)
    assert(p.role==silo_role and (p.item=="raw-fish" or p.item=="satellite"), "Invalid payload command")
    local silo=silo_for(player);local cargo,rocket=cargo_for(silo)
    assert(destination_ready(landing_pad(player),p.item), "Landing destination cannot accept payload results")
    assert(silo and silo.unit_number==p.silo_unit and rocket and rocket.unit_number==p.rocket_unit,
        "Rocket payload identity changed")
    assert(silo.rocket_silo_status==defines.rocket_silo_status.rocket_ready
        and silo.send_to_orbit_automatically==false and player.can_reach_entity(silo), "Rocket cargo is not safely loadable")
    assert(cargo and cargo.is_empty() and cargo.can_insert{name=p.item,count=1}, "Rocket cargo not empty or unavailable")
    local inventory=player.get_main_inventory()
    assert(inventory.get_item_count(p.item)>=1, "Missing paid payload")
    r.attempts.load={receipt=p.receipt,silo_unit=p.silo_unit,rocket_unit=p.rocket_unit,item=p.item,tick=game.tick}
    r.silo_unit=p.silo_unit
    local removed=inventory.remove{name=p.item,count=1}
    local inserted=cargo.insert{name=p.item,count=removed}
    if inserted<removed then
        assert(inventory.insert{name=p.item,count=removed-inserted}==removed-inserted, "Payload refund ambiguous")
    end
    assert(removed==1 and inserted==1, "Payload transfer requires reconciliation")
    local receipt=base_receipt("load");receipt.item=p.item;receipt.quantity=1
    receipt.silo_unit=p.silo_unit;receipt.rocket_unit=p.rocket_unit;r.receipts[p.receipt]=receipt
end
if campaign.launch~=r.launch then r.old_launch=campaign.launch end
local old_launch=r.old_launch
r.launch=function(role)
    local player=actor();assert(role==silo_role, "Unsupported silo role")
    assert(not r.attempts.launch, "Launch already requested; observe, never replay")
    local silo=silo_for(player);local cargo,rocket=cargo_for(silo)
    local item=cargo_item(cargo)
    assert(silo and rocket and item and destination_ready(landing_pad(player),item), "Launch prerequisites missing")
    assert(silo.send_to_orbit_automatically==false and silo.rocket_silo_status==defines.rocket_silo_status.rocket_ready,
        "Rocket launch not ready for manual request")
    assert(player.can_reach_entity(silo), "Rocket outside native reach")
    assert(load_reconciled(silo,rocket,item), "Paid payload load is unresolved; retain its original receipt")
    r.attempts.launch={silo_unit=silo.unit_number,rocket_unit=rocket.unit_number,tick=game.tick}
    r.silo_unit=silo.unit_number
    old_launch(role) -- Engine return remains final authority; no invented can-launch API.
    local receipt=base_receipt("launch");receipt.silo_unit=silo.unit_number
    receipt.rocket_unit=rocket.unit_number;r.receipts.launch=receipt
end
campaign.launch=r.launch
if campaign.transfer~=r.transfer then r.old_transfer=campaign.transfer end
local old_transfer=r.old_transfer
r.transfer=function(role,item,quantity,receipt,extracting)
    if not extracting and supported() then reserve(actor(),item,quantity) end
    return old_transfer(role,item,quantity,receipt,extracting)
end
campaign.transfer=r.transfer
if campaign.craft~=r.craft then r.old_craft=campaign.craft end
local old_craft=r.old_craft
r.craft=function(name,batches)
    if supported() then
        local player=actor();local recipe=assert(player.force.recipes[name])
        for _,i in pairs(recipe.ingredients) do if i.type=="item" then reserve(player,i.name,i.amount*batches) end end
    end
    return old_craft(name,batches)
end
campaign.craft=r.craft
if campaign.observe~=r.observer then r.old_observe=campaign.observe end
local old_observe=r.old_observe
local function observe_launch()
    local row={schema=1,version=script.active_mods.base,supported=supported(),tick=game.tick,
        session_id=storage.jev_session_id,actor_unit=0,surface_index=0,force_index=0,fault=false,
        pad={},pad_site={},fish={},silo={},attempts=r.attempts,receipts=r.receipts}
    if not row.supported then row.reason="unsupported_version_or_mods";return row end
    local ok,err=pcall(function()
        local player=actor();row.actor_unit=r.actor_unit;row.surface_index=r.surface_index;row.force_index=r.force_index
        local pad=landing_pad(player)
        if pad then row.pad={name=pad.name,unit_number=pad.unit_number,position=point(pad.position),
            accepts={['raw-fish']=true,satellite=destination_ready(pad,"satellite")}} end
        local silo=silo_for(player)
        if silo then
            local cargo,rocket=cargo_for(silo)
            row.silo={unit_number=silo.unit_number,rocket_unit=rocket and rocket.unit_number or 0,
                ready=silo.rocket_silo_status==defines.rocket_silo_status.rocket_ready,
                automatic=silo.send_to_orbit_automatically,cargo_available=cargo~=nil,cargo=counts(cargo)}
        end
        if not pad and player.force.technologies["rocket-silo"].researched then row.pad_site=pad_offer(player) or {} end
        if not cargo_item(cargo_for(silo)) then
            local offer=fish_offer(player)
            if offer then row.fish={id=offer.id,position=point(offer.entity.position),reachable=true,yield=5} end
        end
    end)
    if not ok then row.fault=true;row.reason="launch_evidence_invalid" end
    return row
end
r.observer=function()
    local result=old_observe();result.launch_readiness=observe_launch();return result
end
campaign.observe=r.observer
