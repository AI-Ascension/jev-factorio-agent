-- Opt-in, paid straight solid corridors. No entity creation, item grants, or tick handler.
local c, fair = storage.campaign, storage.fair
assert(c and fair, "Solid routes require the existing campaign and fair actor")
local r = storage.solid_routes

-- Read current native counters without calling the campaign observer.  This
-- helper is installed on both first attachment and reload of the same retained
-- runtime so a pre-existing save does not need its owner tables migrated.
local function completion_sample(owner)
    local api=assert(owner.coal_api,"Solid read-only completion API unavailable")
    local p,a=api.epoch()
    local tick=game.tick
    local result={schema=1,producer="solid_routes_readonly_completion_v1",
        session_id=storage.jev_session_id,tick=tick,actor_index=p.index,
        actor_unit=a.unit_number,surface_index=a.surface.index,force_index=a.force.index,
        routes={},diagnostics={}}
    assert(type(result.session_id)=="string" and #result.session_id>0
        and type(owner.cells)=="table" and type(owner.offers)=="table"
        and type(owner.intents)=="table" and type(owner.binding)=="string" and #owner.binding>0,
        "Solid completion owner binding is unavailable")
    local function integer(value)
        return type(value)=="number" and value>=0 and value<=9007199254740991 and value%1==0
    end
    local function held(entity,item)
        local stack=entity.held_stack
        assert(stack and (not stack.valid_for_read or stack.name==item and stack.quality
            and stack.quality.name=="normal"),"Foreign solid held item")
        return stack.valid_for_read and stack.count or 0
    end
    local function pipe_count(cell)
        local total=held(cell.parts.send.entity,cell.item)+held(cell.parts.receive.entity,cell.item)
        local belts=0
        for _,step in ipairs(cell.steps) do if step.part:find("^belt:") then belts=belts+1 end end
        assert(belts>=1 and belts<=24,"Solid completion belt count is invalid")
        for _,step in ipairs(cell.steps) do if step.part:find("^belt:") then
            local entity=assert(cell.parts[step.part] and cell.parts[step.part].entity,
                "Solid completion belt receipt is missing")
            for lane=1,2 do
                local line=entity.get_transport_line(lane)
                for _,entry in pairs(line.get_contents()) do
                    assert(entry.name==cell.item and entry.quality=="normal",
                        "Foreign solid transport item")
                end
                total=total+line.get_item_count(cell.item)
            end
        end end
        return total
    end
    local function per_craft(entity,item,kind)
        local recipe=entity.get_recipe()
        assert(recipe and recipe.products and #recipe.products==1,
            "Unsupported solid completion recipe")
        local product=recipe.products[1]
        assert(product.type=="item" and product.name==item and product.amount==1
            and (product.probability or 1)==1 and not product.amount_min and not product.amount_max
            and entity.productivity_bonus==0,"Variable solid completion recipe")
        local modules=entity.get_module_inventory()
        assert(not modules or modules.is_empty(),"Solid completion modules are unsupported")
        assert(integer(entity.products_finished),"Solid completion counter is invalid")
        if kind=="output" then return 1 end
        assert(kind=="input" and recipe.ingredients and #recipe.ingredients<=32,
            "Unsupported solid completion input")
        local required=nil
        for _,ingredient in pairs(recipe.ingredients) do
            assert(ingredient.type=="item" and integer(ingredient.amount) and ingredient.amount>0,
                "Unsupported solid completion ingredient")
            if ingredient.name==item then required=ingredient.amount end
        end
        assert(required,"Solid completion recipe does not consume routed item")
        return required
    end
    local occupied={}
    local intent_keys={}
    for _,intent in ipairs(owner.intents) do
        assert(#intent_keys<4,"Solid completion intent bound exceeded")
        assert(type(intent)=="table" and type(intent.source)=="string"
            and type(intent.target)=="string" and type(intent.item)=="string"
            and type(intent.destination)=="string","Malformed solid intent")
        local fields={intent.source,intent.target,intent.item,intent.destination};local encoded={}
        for _,value in ipairs(fields) do encoded[#encoded+1]=#value..":"..value end
        intent_keys[#intent_keys+1]=table.concat(encoded,"")
    end
    assert(table.concat(intent_keys,"\n")==owner.binding,
        "Solid completion intents changed after source binding")
    local function add_cell(id,cell,proposed)
        assert(type(id)=="string" and #id>0 and type(cell)=="table" and cell.route==id,
            "Malformed solid completion route")
        local source,target=api.paid_geometry(cell)
        local parts={}
        for part,value in pairs(cell.parts or {}) do
            assert(type(value)=="table")
            parts[part]={role=value.role,unit_number=value.unit_number,
                receipt=value.receipt,paid=value.paid}
        end
        local ready=false
        local counters={ready=false}
        if not proposed and not cell.fault and not cell.pending then
            local ok,linked=pcall(api.topology,cell)
            ready=ok and linked==true
        end
        if ready then
            local source_kind=cell.source.inventory
            local target_kind=cell.target.inventory
            local available=api.stock(api.inventory(source,source_kind),cell.item,true)
            local produced=0
            if source_kind=="output" then
                per_craft(source,cell.item,"output")
                produced=source.products_finished
            end
            local target_stock=api.stock(api.inventory(target,target_kind),cell.item,false)
            local consumed=0
            if target_kind=="input" then
                local per=per_craft(target,cell.item,"input")
                local crafting=target.is_crafting()
                assert(type(crafting)=="boolean","Solid crafting state is invalid")
                consumed=(target.products_finished+(crafting and 1 or 0))*per
            end
            assert(integer(available) and integer(produced) and integer(target_stock)
                and integer(consumed) and (cell.target.inventory=="fuel" or integer(cell.initial_pipe)),
                "Solid completion counter is invalid")
            counters={ready=true,tick=tick,available=available,produced=produced,
                pipe=pipe_count(cell),target_stock=target_stock,consumed=consumed}
            if cell.target.inventory~="fuel" then counters.initial_pipe=cell.initial_pipe end
        end
        result.routes[id]={route=id,layout=cell.layout,item=cell.item,
            source_role=cell.source.role,source_unit=source.unit_number,
            target_role=cell.target.role,target_unit=target.unit_number,
            target_inventory=cell.target.inventory,parts=parts,proposed=proposed==true,
            source_first_tick=cell.first_tick or false,counters=counters,
            baseline_flow=cell.flow and {layout=cell.flow.layout,source_unit=cell.flow.source_unit,
                target_unit=cell.flow.target_unit,method=cell.flow.method,first_tick=cell.flow.first_tick,
                last_tick=cell.flow.last_tick,last_positive_tick=cell.flow.last_positive_tick,
                positive_samples=cell.flow.positive_samples,sent=cell.flow.sent,
                received=cell.flow.received,unattributed_loss=cell.flow.unattributed_loss} or {}}
        if not proposed then
            occupied[cell.source.role]=true;occupied[cell.target.role]=true
        end
    end
    local count=0
    for id,cell in pairs(owner.cells) do count=count+1;assert(count<=4);add_cell(id,cell,false) end
    for id,cell in pairs(owner.offers) do
        if not owner.cells[id] then count=count+1;assert(count<=4);add_cell(id,cell,true) end
    end
    local cached=owner.completion_diagnostics
    local cached_valid=type(cached)=="table" and cached.schema==1
        and cached.producer=="solid_routes_observer_v1"
        and cached.session_id==result.session_id and integer(cached.tick) and cached.tick<=tick
        and tick-cached.tick<=600 and cached.actor_index==p.index
        and cached.actor_unit==a.unit_number and cached.surface_index==a.surface.index
        and cached.force_index==a.force.index and cached.intent_binding==owner.binding
        and type(cached.intents)=="table" and #cached.intents==#owner.intents
        and type(cached.diagnostics)=="table" and #cached.diagnostics==#owner.intents
    local used_cached=false
    for index,intent in ipairs(owner.intents) do
        local state,reason=nil,nil
        if occupied[intent.source] or occupied[intent.target] then
            state,reason="committed","paid_or_pending_route"
        else
            for id,offer in pairs(owner.offers) do
                if not owner.cells[id] and offer.source.role==intent.source
                    and offer.target.role==intent.target and offer.item==intent.item
                    and offer.target.inventory==intent.destination and not offer.fault
                    and not offer.pending then
                    state,reason="proposed","ready_layout";break
                end
            end
        end
        if not state then
            assert(cached_valid,"Unresolved solid intent lacks current or fresh diagnostic evidence")
            local old_intent,old_diagnostic=cached.intents[index],cached.diagnostics[index]
            local source=c.entities[intent.source];local target=c.entities[intent.target]
            assert(type(old_intent)=="table" and old_intent.source==intent.source
                and old_intent.target==intent.target and old_intent.item==intent.item
                and old_intent.destination==intent.destination
                and old_intent.source_unit==(source and source.valid and source.unit_number or false)
                and old_intent.target_unit==(target and target.valid and target.unit_number or false)
                and type(old_diagnostic)=="table" and old_diagnostic.intent_index==index,
                "Solid diagnostic endpoint identity changed")
            state,reason=old_diagnostic.state,old_diagnostic.reason;used_cached=true
        end
        result.diagnostics[index]={intent_index=index,state=state,reason=reason}
    end
    result.diagnostics_tick=used_cached and cached.tick or tick
    result.diagnostics_source=used_cached and "retained_owner_completion_mixed_v1"
        or "retained_owner_completion_v1"
    return result
end
if r then
    assert(r.protocol == 1 and r.contract_family == "straight-solid-corridor-v1" and r.implementation_revision == 4
        and r.reservation_contract == "full-corridor-manhattan-v1" and c.observe == r.observer and c.transfer == r.transfer
        and c.configure == r.configure, "Solid route runtime requires reconciliation")
    r.completion_sample=completion_sample
    return
end
r = {protocol=1, contract_family="straight-solid-corridor-v1", implementation_revision=4, reservation_contract="full-corridor-manhattan-v1", cells={}, offers={}, intents={}, serial=0,
    completion_diagnostics=nil}
storage.solid_routes = r
local vectors = {{x=0,y=-1},{x=1,y=0},{x=0,y=1},{x=-1,y=0}}
local source_names = {["wooden-chest"]=true,["iron-chest"]=true,["steel-chest"]=true,
    ["assembling-machine-1"]=true,["assembling-machine-2"]=true}
local target_names = {["assembling-machine-1"]=true,["assembling-machine-2"]=true,
    ["stone-furnace"]=true,["steel-furnace"]=true,["burner-mining-drill"]=true,
    ["burner-inserter"]=true,["boiler"]=true}
local function count(t) local n=0;for _ in pairs(t) do n=n+1 end;return n end
local function text(s,n) return type(s)=="string" and #s>0 and #s<=(n or 128) and not s:find("[^ -~]") end
local function int(v) return type(v)=="number" and v>=0 and v<=9007199254740991 and v%1==0 end
local function pt(v) return {x=v.x or v[1], y=v.y or v[2]} end
local function add(a,b,k) return {x=a.x+b.x*(k or 1),y=a.y+b.y*(k or 1)} end
local function same(a,b) return a and b and a.x==b.x and a.y==b.y end
local function bounds(e)
    local a,b=pt(e.bounding_box.left_top),pt(e.bounding_box.right_bottom)
    for _,p in ipairs({a,b}) do assert(type(p.x)=="number" and type(p.y)=="number"
        and math.abs(p.x)<=1000000 and math.abs(p.y)<=1000000,"Invalid solid coordinate bound") end
    assert(b.x>a.x and b.y>a.y and b.x-a.x<=4 and b.y-a.y<=4,"Unsupported solid footprint")
    return {left_top=a,right_bottom=b}
end
local function inside(p,b) return p.x>b.left_top.x and p.x<b.right_bottom.x and p.y>b.left_top.y and p.y<b.right_bottom.y end
local function epoch()
    local p=fair.actor() -- Existing binding, normal-speed and non-cheat guard, including default index.
    local a=(storage.agent_characters or {})[1]
    assert(p and a and a.valid and p.character==a and int(p.index) and p.index>0
        and text(storage.jev_session_id), "Solid route actor unavailable")
    return p,a
end
local function owner(role)
    local _,a=epoch();local e=c.entities[role]
    assert(e and e.valid and e.unit_number and e.quality and e.quality.name=="normal" and e.surface==a.surface and e.force==a.force,
        "Solid endpoint is not owned on actor surface/force")
    -- Aliased roles cannot masquerade as independent endpoints.
    local checked=0
    for other,entity in pairs(c.entities) do
        checked=checked+1;assert(checked<=2048,"Owned entity registry exceeds solid audit bound")
        assert(other==role or entity~=e,"Solid endpoint role is aliased")
    end
    return e
end
local function inventory(e,kind)
    if kind=="chest" then return e.get_inventory(defines.inventory.chest) end
    if kind=="output" then return e.get_output_inventory() end
    if kind=="input" then return e.get_inventory(defines.inventory.assembling_machine_input) end
    if kind=="fuel" then return e.get_fuel_inventory() end
    error("Unsupported solid endpoint inventory")
end
local function stock(inv,item,exclusive)
    assert(inv and inv.valid and #inv<=200,"Solid inventory unavailable or too large")
    local amount,total=0,0
    for i=1,#inv do
        local stack=inv[i]
        if stack.valid_for_read then
            assert(stack.quality and stack.quality.name=="normal", "Unsupported item quality")
            assert(int(stack.count), "Invalid native inventory count")
            total=total+stack.count
            if stack.name==item then amount=amount+stack.count end
        end
    end
    assert(not exclusive or total==amount,"Mixed source inventory")
    return amount
end
local function deterministic(e,item,kind)
    local recipe=e.get_recipe()
    assert(recipe and recipe.products and #recipe.products==1,"Unsupported solid recipe")
    local product=recipe.products[1]
    -- Unit-yield only: do not assume products_finished means crafts for multi-output recipes.
    assert(product.type=="item" and product.amount==1 and (product.probability or 1)==1
        and not product.amount_min and not product.amount_max and e.productivity_bonus==0,
        "Unsupported variable/productive recipe")
    local modules=e.get_module_inventory()
    assert(not modules or modules.is_empty(),"Module accounting unsupported")
    local required=nil
    assert(#recipe.ingredients<=32,"Solid ingredient count exceeds bound")
    for _,entry in pairs(recipe.ingredients) do
        assert(entry.type=="item" and int(entry.amount) and entry.amount>0,"Unsupported recipe ingredient")
        if entry.name==item then required=entry.amount end
    end
    assert(kind~="output" or product.name==item,"Wrong source product")
    assert(kind~="input" or required,"Wrong destination ingredient")
    assert(int(e.products_finished),"Invalid production counter")
    return recipe.name, (kind=="input" and required or 1)
end
local function endpoint(role,item,kind,source)
    local e=owner(role)
    assert((source and source_names or target_names)[e.name],"Unsupported solid endpoint prototype")
    assert(source and (kind=="chest" or kind=="output") or not source and (kind=="input" or kind=="fuel"),"Wrong inventory kind")
    assert((kind=="chest")== (e.type=="container"),"Wrong chest inventory")
    if not source then assert((kind=="input")== (e.type=="assembling-machine"),"Wrong consumer inventory") end
    local recipe=""
    if kind=="input" or kind=="output" then recipe=deterministic(e,item,kind) end
    if kind=="fuel" then assert(item=="coal" and e.burner,"Only coal burners supported") end
    stock(inventory(e,kind),item,source)
    return {role=role,unit_number=e.unit_number,name=e.name,inventory=kind,position=pt(e.position),bounds=bounds(e),recipe=recipe},e
end
local function endpoint_ok(saved,item,source)
    local now,e=endpoint(saved.role,item,saved.inventory,source)
    assert(now.unit_number==saved.unit_number and now.name==saved.name and now.recipe==saved.recipe
        and same(now.position,saved.position) and same(now.bounds.left_top,saved.bounds.left_top)
        and same(now.bounds.right_bottom,saved.bounds.right_bottom),"Solid endpoint changed")
    return e
end
local function next_step(cell)
    for _,s in ipairs(cell.steps) do if not cell.parts[s.part] then return s end end
end
local function remaining(cell)
    local bill={};for _,s in ipairs(cell.steps) do if not cell.parts[s.part] then bill[s.name]=(bill[s.name] or 0)+1 end end
    return bill
end
local function paid_geometry(cell)
    local source=endpoint_ok(cell.source,cell.item,true);local target=endpoint_ok(cell.target,cell.item,false)
    assert(source~=target,"Identical route endpoints")
    for _,s in ipairs(cell.steps) do
        local p=cell.parts[s.part]
        if p then
            local e=p.entity
            assert(e and e.valid and e==c.entities[p.role] and e.unit_number==p.unit_number and p.paid==1
                and e.name==s.name and e.quality and e.quality.name=="normal" and same(e.position,s.position) and e.direction==s.direction
                and e.surface==source.surface and e.force==source.force,"Paid solid component changed")
        end
    end
    return source,target
end
local function foreign_connections(cell)
    local source,target=paid_geometry(cell)
    local allowed={[source.unit_number]=true,[target.unit_number]=true}
    for _,part in pairs(cell.parts) do allowed[part.unit_number]=true end
    if r.coal then r.coal.allow_source(cell,allowed) end
    -- Include long-handed arms and mining drops around both endpoint footprints.
    for _,endpoint in ipairs({source,target}) do
        local box=bounds(endpoint)
        local near=endpoint.surface.find_entities_filtered{area={{box.left_top.x-3,box.left_top.y-3},
            {box.right_bottom.x+3,box.right_bottom.y+3}},type={"inserter","mining-drill","loader","loader-1x1"},limit=129}
        assert(#near<=128,"Solid endpoint neighbourhood exceeds bound")
        for _,e in pairs(near) do if not allowed[e.unit_number] then
            if e.type=="loader" or e.type=="loader-1x1" then error("Foreign endpoint loader") end
            assert(e.drop_target~=endpoint and (e.type~="inserter" or e.pickup_target~=endpoint),"Foreign endpoint transport")
        end end
    end
    for _,s in ipairs(cell.steps) do
        local near=source.surface.find_entities_filtered{position=s.position,radius=3,
            type={"transport-belt","underground-belt","splitter","linked-belt","loader","loader-1x1","inserter","mining-drill"},limit=65}
        assert(#near<=64,"Solid route neighbourhood exceeds bound")
        for _,e in pairs(near) do if not allowed[e.unit_number] then
            local function enters(p)
                p=pt(p);return math.abs(p.x-s.position.x)<0.5 and math.abs(p.y-s.position.y)<0.5
            end
            if e.type=="inserter" then
                assert(not enters(e.pickup_position) and not enters(e.drop_position),"Foreign route inserter")
            elseif e.type=="mining-drill" then
                assert(not enters(e.drop_position),"Foreign route mining drop")
            else
                local p=pt(e.position)
                assert(math.abs(p.x-s.position.x)+math.abs(p.y-s.position.y)>1.01,"Foreign belt join")
            end
        end end
    end
end
local function can_place(source,s)
    return source.surface.can_place_entity{name=s.name,position=s.position,direction=s.direction,
        force=source.force,build_check_type=defines.build_check_type.manual}
end
local function powered_position(source,position)
    local poles=source.surface.find_entities_filtered{position=position,radius=10,type="electric-pole",force=source.force,limit=65}
    assert(#poles<=64,"Power survey exceeds bound")
    for _,pole in pairs(poles) do
        local radius=pole.prototype.get_supply_area_distance(pole.quality)
        assert(type(radius)=="number" and radius>=0 and radius<math.huge,"Invalid solid power radius")
        local owned=false
        for _,registered in pairs(c.entities) do if registered==pole then owned=true;break end end
        if owned and pole.electric_network_id and radius and math.abs(pole.position.x-position.x)<=radius
            and math.abs(pole.position.y-position.y)<=radius then return true end
    end
    return false
end
-- Preparing a route reserves its whole future footprint, not only paid cells.
-- Retain faulted/ambiguous reservations until an authorized reconciliation.
-- Uncommitted offers are alternatives and never reserve against each other.
local function reservations_clear(cell)
    if r.coal then r.coal.corridor_reservations_clear(cell) end
    for id,other in pairs(r.cells) do
        if id~=cell.route then
            for _,a in ipairs(cell.steps) do
                for _,b in ipairs(other.steps) do
                    local distance=math.abs(a.position.x-b.position.x)+math.abs(a.position.y-b.position.y)
                    assert(distance>1.01,"Solid corridor reservation conflict")
                end
            end
        end
    end
end
local function clear(cell)
    reservations_clear(cell)
    local source=paid_geometry(cell);foreign_connections(cell)
    for _,s in ipairs(cell.steps) do
        if not cell.parts[s.part] then
            assert(can_place(source,s),"Solid corridor is obstructed")
            if s.name=="inserter" then assert(powered_position(source,s.position),"Solid arm lacks owned power coverage") end
        end
    end
end
local function failure_code(value)
    local message=tostring(value)
    local codes={
        {"is not owned","endpoint_unavailable"},{"role is aliased","aliased_identity"},
        {"Mixed source inventory","mixed_source_items"},{"lacks owned power","missing_owned_power"},
        {"reservation conflict","reserved_corridor"},
        {"obstructed","obstructed_corridor"},{"Foreign","foreign_transport"},
        {"Wrong","incompatible_item_or_inventory"},{"Unsupported","unsupported_endpoint_or_recipe"},
        {"exceeds bound","survey_bound"},{"survey limit","survey_bound"}}
    for _,pair in ipairs(codes) do if message:find(pair[1],1,true) then return pair[2] end end
    return "qualification_failed" -- Never publish arbitrary native error text.
end
local function survey(intent)
    if r.coal then
        local found,cell=r.coal.corridor_offer(intent)
        if found then
            if cell then clear(cell);r.serial=r.serial+1;cell.layout="solid-layout:"..r.serial end
            return cell,"endpoint_unavailable"
        end
    end
    local src=owner(intent.source)
    local source,entity=endpoint(intent.source,intent.item,src.type=="container" and "chest" or "output",true)
    local target=endpoint(intent.target,intent.item,intent.destination,false)
    assert(source.unit_number~=target.unit_number,"Solid self route")
    local id="solid:"..source.unit_number..":"..target.unit_number..":"..intent.item..":"..intent.destination
    assert(#id<=100,"Solid route identity too long")
    local box=source.bounds
    local probes,last_reason=0,"no_supported_corridor"
    -- Deterministic N/E/S/W corridors from each source edge tile. No arbitrary graph search.
    for direction,v in ipairs(vectors) do
        for x=math.floor(box.left_top.x)-1,math.ceil(box.right_bottom.x)+1 do
            for y=math.floor(box.left_top.y)-1,math.ceil(box.right_bottom.y)+1 do
                local send={x=x+0.5,y=y+0.5}
                if not inside(send,box) and inside(add(send,v,-1),box) then
                    for n=1,24 do
                        local receive=add(send,v,n+1)
                        if inside(add(receive,v),target.bounds) and not inside(receive,target.bounds) then
                            local steps={{part="receive",name="inserter",position=receive,direction=((direction-1)*4+8)%16}}
                            for i=n,1,-1 do steps[#steps+1]={part="belt:"..i,name="transport-belt",position=add(send,v,i),direction=(direction-1)*4} end
                            steps[#steps+1]={part="send",name="inserter",position=send,direction=((direction-1)*4+8)%16}
                            local cell={route=id,item=intent.item,source=source,target=target,steps=steps,parts={}}
                            probes=probes+#steps;assert(probes<=512,"Solid layout survey limit")
                            local ok,reason=pcall(clear,cell)
                            if ok then r.serial=r.serial+1;cell.layout="solid-layout:"..r.serial;return cell end
                            last_reason=failure_code(reason)
                        end
                    end
                end
            end
        end
    end
    return nil,last_reason
end
c.set_solid_intents=function(intents)
    assert(type(intents)=="table" and #intents>=1 and #intents<=4 and count(intents)==#intents,"Invalid solid route intents")
    local keys,roles,detached={},{},{}
    for _,intent in ipairs(intents) do
        assert(type(intent)=="table" and count(intent)==4 and text(intent.source,64) and text(intent.target,64)
            and text(intent.item,64) and (intent.destination=="input" or intent.destination=="fuel")
            and (intent.destination~="fuel" or intent.item=="coal")
            and intent.source~=intent.target and not roles[intent.source] and not roles[intent.target],"Invalid or shared solid intent")
        roles[intent.source],roles[intent.target]=true,true
        local fields={}
        for _,key in ipairs({"source","target","item","destination"}) do
            fields[#fields+1]=#intent[key]..":"..intent[key]
        end
        keys[#keys+1]=table.concat(fields,"")
        detached[#detached+1]={source=intent.source,target=intent.target,item=intent.item,destination=intent.destination}
    end
    local binding=table.concat(keys,"\n")
    assert(not r.binding or r.binding==binding,"Solid route treatment is immutable")
    -- A completion query may consume only a diagnostic projection produced
    -- after this exact intent list was observed. Reconfiguration invalidates
    -- even an identical cache so a caller cannot reuse a prior observation as
    -- a fresh completion read.
    r.completion_diagnostics=nil
    r.binding,r.intents=binding,detached
    return {configured=#intents}
end
local function params(args)
    assert(type(args)=="table" and count(args)==4 and text(args.route) and text(args.layout)
        and text(args.part) and text(args.receipt),"Invalid solid command fields")
    for id,other in pairs(r.cells) do
        for part,paid in pairs(other.parts) do
            if paid.receipt==args.receipt then
                assert(id==args.route and part==args.part,"Solid receipt already belongs to another component")
            end
        end
        if other.pending and other.pending.receipt==args.receipt then
            assert(id==args.route and other.pending.part==args.part,"Solid receipt already reserved")
        end
    end
    local cell=r.cells[args.route] or r.offers[args.route]
    assert(cell and cell.layout==args.layout and not cell.fault,"Solid offer missing, changed or faulted")
    local existing=cell.parts[args.part]
    if existing then assert(existing.receipt==args.receipt,"Conflicting solid receipt");paid_geometry(cell);return cell,nil end
    local next=next_step(cell)
    assert(next and next.part==args.part,"Non-prefix solid construction")
    return cell,next
end
local function affordable(cell)
    local p=fair.actor();assert(p.crafting_queue_size==0,"Background crafting must settle before construction")
    local inventory=p.get_main_inventory()
    assert(inventory and inventory.valid and #inventory<=200,"Unsupported actor inventory bound")
    for name,n in pairs(remaining(cell)) do
        local available=0
        for i=1,#inventory do
            local stack=inventory[i]
            if stack.valid_for_read and stack.name==name then
                assert(stack.quality and stack.quality.name=="normal" and int(stack.count),
                    "Unsupported solid construction quality")
                available=available+stack.count
            end
        end
        assert(available>=n,"Incomplete solid construction kit")
    end
    return p
end
local function finish_pending(cell)
    local pending=cell.pending
    if not pending or pending.phase~="placed" then return end
    local e=pending.entity
    local source=endpoint_ok(cell.source,cell.item,true)
    endpoint_ok(cell.target,cell.item,false)
    assert(e and e.valid and e.force==source.force and e.surface==source.surface
        and e.quality and e.quality.name=="normal" and e.unit_number==pending.unit_number and pending.after==pending.before-1
        and pending.spec.name==e.name and same(e.position,pending.spec.position) and e.direction==pending.spec.direction,
        "Ambiguous paid solid placement")
    local role=cell.route..":"..pending.spec.part
    assert(not c.entities[role] or c.entities[role]==e,"Solid role already owned")
    for other,owned in pairs(c.entities) do assert(other==role or owned~=e,"Solid unit already owned elsewhere") end
    c.entities[role]=e
    cell.parts[pending.spec.part]={entity=e,role=role,receipt=pending.receipt,unit_number=e.unit_number,paid=1}
    cell.pending=nil
end
local function reply(value)
    rcon.print(helpers.table_to_json(value))
    return value
end
c.prepare_solid_route=function(args)
    local cell,s=params(args)
    if not s then return reply{position=cell.parts[args.part].entity.position,name=cell.parts[args.part].entity.name,already_paid=true} end
    assert(not cell.pending or cell.pending.part==args.part and cell.pending.receipt==args.receipt,"Different solid action pending")
    assert(not cell.pending or cell.pending.phase=="prepared","Ambiguous solid dispatch requires reconciliation")
    clear(cell);affordable(cell)
    if r.coal then r.coal.construction_gate(cell,args.receipt) end
    r.cells[cell.route],r.offers[cell.route]=cell,nil
    cell.pending=cell.pending or {part=args.part,receipt=args.receipt,phase="prepared",spec=s}
    return reply{position=s.position,name=s.name}
end
c.build_solid_route=function(args)
    local cell,s=params(args)
    if not s then return {already_paid=true} end
    local pending=cell.pending
    assert(pending and pending.part==args.part and pending.receipt==args.receipt and pending.phase=="prepared","Solid action not prepared")
    clear(cell);local player=affordable(cell)
    if r.coal then r.coal.construction_gate(cell,args.receipt) end
    pending.before=player.get_item_count(s.name);pending.phase="dispatching"
    local receipt=fair.place(s.name,s.position,s.direction)
    local e=player.surface.find_entity(s.name,s.position)
    local after=player.get_item_count(s.name)
    assert(e and e.valid and receipt.unit_number==e.unit_number and receipt.name==s.name
        and same(pt(receipt.position),s.position) and after==pending.before-1,"Solid native payment mismatch")
    pending.entity,pending.unit_number,pending.after,pending.phase=e,e.unit_number,after,"placed"
    finish_pending(cell)
    return {placed=true}
end
local function topology(cell)
    local source,target=paid_geometry(cell);foreign_connections(cell)
    if next_step(cell) then return false end
    local n=#cell.steps-2
    local send,receive=cell.parts.send.entity,cell.parts.receive.entity
    assert(send.pickup_target==source and send.drop_target==cell.parts["belt:1"].entity
        and receive.pickup_target==cell.parts["belt:"..n].entity and receive.drop_target==target,"Solid endpoint topology changed")
    for i=1,n do
        local e=cell.parts["belt:"..i].entity;local neighbours=e.belt_neighbours
        assert(#neighbours.inputs==(i>1 and 1 or 0) and #neighbours.outputs==(i<n and 1 or 0),"Solid belt join changed")
        if i>1 then assert(neighbours.inputs[1]==cell.parts["belt:"..(i-1)].entity,"Reverse solid input") end
        if i<n then assert(neighbours.outputs[1]==cell.parts["belt:"..(i+1)].entity,"Reverse solid output") end
    end
    return true
end
local function held(e,item)
    local s=e.held_stack
    assert(not s.valid_for_read or s.name==item and s.quality and s.quality.name=="normal","Foreign solid arm item")
    return s.valid_for_read and s.count or 0
end
local function sample(cell)
    if not topology(cell) then cell.reason="building";return false end
    local source,target=paid_geometry(cell);local pipe=held(cell.parts.send.entity,cell.item)+held(cell.parts.receive.entity,cell.item)
    for i=1,#cell.steps-2 do
        local belt=cell.parts["belt:"..i].entity
        for lane=1,2 do
            local line=belt.get_transport_line(lane)
            -- Empty/normal qualities only; get_contents carries quality in 2.0.
            for _,entry in pairs(line.get_contents()) do
                assert(entry.name==cell.item and entry.quality=="normal","Foreign solid belt item or quality")
            end
            pipe=pipe+line.get_item_count(cell.item)
        end
    end
    if r.coal and r.coal.sample(cell,pipe) then return true end
    local available=stock(inventory(source,cell.source.inventory),cell.item,true)
    local produced=cell.source.inventory=="output" and source.products_finished or 0
    local target_stock=stock(inventory(target,cell.target.inventory),cell.item,false)
    local consumed=0
    if cell.target.inventory=="input" then
        local _,per_craft=deterministic(target,cell.item,"input")
        consumed=(target.products_finished+(target.is_crafting() and 1 or 0))*per_craft
    end
    local now={tick=game.tick,available=available,produced=produced,pipe=pipe,target_stock=target_stock,consumed=consumed}
    local old=cell.previous
    if old then
        assert(now.tick>=old.tick and produced>=old.produced and consumed>=old.consumed,"Solid counters reset")
        if now.tick==old.tick then
            assert(available==old.available and produced==old.produced and pipe==old.pipe
                and target_stock==old.target_stock and consumed==old.consumed,"Solid same-tick mutation")
        else
            local sent=old.available-available+produced-old.produced
            local arrived=sent-(pipe-old.pipe)
            local received=target_stock-old.target_stock+consumed-old.consumed
            assert(sent>=0 and arrived>=0,"Unattributed solid source/pipeline mutation")
            if cell.target.inventory=="input" then assert(arrived==received,"Solid conservation mismatch")
            else
                assert(arrived>=received,"Unattributed fuel insertion")
                cell.loss=cell.loss+arrived-received
                -- Only observed positive inventory growth is a delivery lower bound.
                received=math.max(0,received)
            end
            cell.sent=cell.sent+sent;cell.received=cell.received+received
            if received>0 and cell.received>cell.initial_pipe then
                cell.positive=cell.positive+1
                cell.last_positive_tick=game.tick
            end
        end
    else
        cell.first_tick,cell.sent,cell.received,cell.positive,cell.loss,cell.initial_pipe=game.tick,0,0,0,0,pipe
        cell.last_positive_tick=game.tick
    end
    cell.previous=now
    cell.flow={layout=cell.layout,source_unit=cell.source.unit_number,target_unit=cell.target.unit_number,
        method=cell.target.inventory=="fuel" and "exclusive_fuel_lower_bound" or "stoichiometric_balance",
        first_tick=cell.first_tick,last_tick=game.tick,last_positive_tick=cell.last_positive_tick,
        positive_samples=cell.positive,sent=cell.sent,
        received=math.max(0,cell.received-cell.initial_pipe),unattributed_loss=cell.loss}
    if cell.parts.send.entity.energy<=0 or cell.parts.receive.entity.energy<=0 then cell.reason="no_power"
    elseif available==0 then cell.reason="source_depleted"
    elseif inventory(target,cell.target.inventory).get_insertable_count(cell.item)==0 then cell.reason="backpressure"
    else cell.reason="observing_flow" end
    return true
end
local old_observe=c.observe
r.observer=function()
    if r.coal then r.coal.before_observe() end
    for _,cell in pairs(r.cells) do
        if cell.pending and cell.pending.phase=="placed" then
            if not pcall(finish_pending,cell) then cell.fault="receipt_reconciliation_failed" end
        elseif cell.pending and cell.pending.phase=="dispatching" then cell.fault="ambiguous_dispatch" end
    end
    local result=old_observe();local p,a=epoch();local rows,diagnostics={},{}
    local occupied={}
    for _,cell in pairs(r.cells) do occupied[cell.source.role]=true;occupied[cell.target.role]=true end
    for index,intent in ipairs(r.intents) do
        local diagnostic={intent_index=index,state="committed",reason="paid_or_pending_route"}
        diagnostics[#diagnostics+1]=diagnostic
        if not occupied[intent.source] and not occupied[intent.target] then
            local retained=nil
            for id,offer in pairs(r.offers) do if offer.source.role==intent.source and offer.target.role==intent.target then
                if pcall(clear,offer) then retained=offer else r.offers[id]=nil end
            end end
            if not retained then
                local ok,offer,reason=pcall(survey,intent)
                if ok and offer then r.offers[offer.route]=offer;retained=offer
                else diagnostic.state="unavailable";diagnostic.reason=ok and reason or failure_code(offer) end
            end
            if retained then diagnostic.state="proposed";diagnostic.reason="ready_layout" end
        end
    end
    for _,collection in ipairs({r.offers,r.cells}) do for id,cell in pairs(collection) do
        local linked=false
        if not cell.fault then
            local ok,value=pcall(function()
                reservations_clear(cell)
                paid_geometry(cell)
                if collection==r.cells then return sample(cell) end
                clear(cell);return false
            end)
            if ok then linked=value else cell.fault="identity_topology_or_balance_mismatch" end
        end
        local parts={};for key,v in pairs(cell.parts) do parts[key]={role=v.role,unit_number=v.unit_number,receipt=v.receipt,paid=1} end
        rows[id]={route=id,layout=cell.layout,item=cell.item,source=cell.source,target=cell.target,steps=cell.steps,parts=parts,
            state=cell.fault and "fault" or (collection==r.offers and "proposed" or (linked and "ready" or "building")),
            topology=linked,flow=cell.fault and {} or cell.flow or {},reason=cell.fault or cell.reason or "proposal",
            pending=cell.pending and {part=cell.pending.part,receipt=cell.pending.receipt,phase=cell.pending.phase} or {}}
    end end
    assert(count(rows)<=4,"Solid route cardinality exceeded")
    -- Keep a private copy of the actual ordered producer diagnostics. The
    -- completion reader validates this observation's identity and freshness;
    -- it must never survey endpoints or synthesize a missing reason itself.
    local diagnostic_intents={}
    for index,intent in ipairs(r.intents) do
        local source,target=c.entities[intent.source],c.entities[intent.target]
        diagnostic_intents[index]={source=intent.source,target=intent.target,item=intent.item,
            destination=intent.destination,
            source_unit=source and source.valid and source.unit_number or false,
            target_unit=target and target.valid and target.unit_number or false}
    end
    local retained_diagnostics={}
    for index,diagnostic in ipairs(diagnostics) do
        retained_diagnostics[index]={intent_index=diagnostic.intent_index,
            state=diagnostic.state,reason=diagnostic.reason}
    end
    r.completion_diagnostics={schema=1,producer="solid_routes_observer_v1",
        session_id=storage.jev_session_id,tick=game.tick,actor_index=p.index,
        actor_unit=a.unit_number,surface_index=a.surface.index,force_index=a.force.index,
        intent_binding=r.binding,intents=diagnostic_intents,diagnostics=retained_diagnostics}
    result.solid_routes={protocol=1,session_id=storage.jev_session_id,tick=game.tick,
        actor_index=p.index,surface_index=a.surface.index,force_index=a.force.index,routes=rows,diagnostics=diagnostics}
    if r.coal then result.coal_supply=r.coal.snapshot() end
    return result
end
local old_transfer,old_configure=c.transfer,c.configure
local function guard(role,item,configure)
    for _,cell in pairs(r.cells) do
        if role==cell.source.role or role==cell.target.role then
            assert(not configure,"Solid route owns endpoint recipe")
            if item==cell.item then
                -- Construction is downstream first, sender last. No transport
                -- baseline exists yet, so useful sequential service remains safe
                -- until sender takeover. Never interleave a pending placement.
                assert(not cell.fault and not cell.pending and not cell.parts.send,
                    "Solid route owns connected endpoint item")
                paid_geometry(cell)
            end
        end
        for _,part in pairs(cell.parts) do assert(role~=part.role,"Solid component is exclusively owned") end
    end
end
r.transfer=function(role,item,quantity,receipt,extracting)
    if r.coal and r.coal.handles(role,item) then
        return r.coal.transfer(old_transfer,role,item,quantity,receipt,extracting)
    end
    guard(role,item,false);return old_transfer(role,item,quantity,receipt,extracting)
end
r.configure=function(role,recipe) guard(role,nil,true);return old_configure(role,recipe) end
c.observe,c.transfer,c.configure=r.observer,r.transfer,r.configure

-- Narrow same-version extension surface. Not an external observation provider.
r.coal_api={endpoint=endpoint,endpoint_ok=endpoint_ok,owner=owner,epoch=epoch,stock=stock,
    inventory=inventory,clear=clear,topology=topology,paid_geometry=paid_geometry,remaining=remaining,
    powered_position=powered_position,reply=reply,inside=inside}
r.completion_sample=completion_sample
