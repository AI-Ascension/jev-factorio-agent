-- Explicit, paid electric coal branches. No spawned entities, grants or tick handler.
-- Requires the exact straight-corridor extension; does not enable a supervisor treatment.
local c,fair,r=storage.campaign,storage.fair,storage.solid_routes
assert(c and fair and r and r.implementation_revision==4 and r.coal_api and c.observe==r.observer,
    "Coal supply requires the qualified solid runtime")
local function set_admission_evidence(enabled)
    assert(type(enabled)=="boolean","Coal admission evidence opt-in must be boolean")
    storage.coal_supply.admission_evidence=enabled
end
if storage.coal_supply then
    assert(r.coal==storage.coal_supply and r.coal.revision==4 and c.prepare_coal_source==r.coal.prepare
        and c.build_coal_source==r.coal.build,"Coal runtime requires reconciliation")
    c.set_coal_admission_evidence=set_admission_evidence
    return
end
local q={revision=4,targets={},rows={},committed=false,serial=0,reason="no_supported_bundle",admission_evidence=false}
storage.coal_supply,r.coal=q,q
local a=r.coal_api
local vec={{x=0,y=-1},{x=1,y=0},{x=0,y=1},{x=-1,y=0}}
local function text(s,n) return type(s)=="string" and #s>0 and #s<=(n or 128) and not s:find("[^ -~]") end
local function int(n) return type(n)=="number" and n>=0 and n<=9007199254740991 and n%1==0 end
local function count(t) local n=0;for _ in pairs(t) do n=n+1 end;return n end
local function cp(t) if type(t)~="table" then return t end;local v={};for k,x in pairs(t) do v[k]=cp(x) end;return v end
local function point(p) return {x=p.x or p[1],y=p.y or p[2]} end
local function same(p,v) return p and v and p.x==v.x and p.y==v.y end
local function add(p,v,n) return {x=p.x+v.x*n,y=p.y+v.y*n} end
local function box(p,h) return {left_top={x=p.x-h,y=p.y-h},right_bottom={x=p.x+h,y=p.y+h}} end
local function overlaps(x,y) return x.left_top.x<y.right_bottom.x and y.left_top.x<x.right_bottom.x and x.left_top.y<y.right_bottom.y and y.left_top.y<x.right_bottom.y end
local function role(target,part) return "coal:"..target..":"..part end
local function actor()
    local p,ch=a.epoch()
    -- Exclusive source-balance accounting cannot qualify a multiplayer surface.
    assert(game.connected_players and #game.connected_players==1 and game.connected_players[1]==p,
        "Coal accounting requires one connected actor")
    return p,ch
end
local function normal_proto(name)
    local p=prototypes.entity[name];assert(p and p.collision_box,"Coal prototype unavailable")
    return p
end
local function footprint(name,p)
    local b=normal_proto(name).collision_box
    local l,h=point(b.left_top),point(b.right_bottom)
    assert(l.x<0 and l.y<0 and h.x>0 and h.y>0 and h.x-l.x<=3 and h.y-l.y<=3,"Unsupported coal collision box")
    return {left_top={x=p.x+l.x,y=p.y+l.y},right_bottom={x=p.x+h.x,y=p.y+h.y}}
end
local function power(p)
    local _,ch=actor();local poles=ch.surface.find_entities_filtered{position=p,radius=10,type="electric-pole",force=ch.force,limit=65}
    assert(#poles<=64,"Coal power bound")
    table.sort(poles,function(x,y) return x.unit_number<y.unit_number end)
    local roles={};for name in pairs(c.entities) do roles[#roles+1]=name end;table.sort(roles)
    assert(#roles<=2048,"Coal ownership bound")
    for _,pole in ipairs(poles) do
        local radius=pole.prototype.get_supply_area_distance(pole.quality)
        assert(type(radius)=="number" and radius>=0 and radius<math.huge,"Invalid coal power radius")
        if radius and pole.electric_network_id and math.abs(pole.position.x-p.x)<=radius and math.abs(pole.position.y-p.y)<=radius then
            local pr=nil;for _,name in ipairs(roles) do if c.entities[name]==pole then assert(not pr,"Aliased power pole");pr=name end end
            if pr then for _,name in ipairs(roles) do
                local e=c.entities[name]
                if e and e.valid and e~=pole and (e.type=="assembling-machine" or e.name=="electric-mining-drill")
                    and e.surface==ch.surface and e.force==ch.force and e.electric_network_id==pole.electric_network_id
                    and e.energy>0 then
                    a.owner(pr);a.owner(name)
                    return {pole_role=pr,pole_unit=pole.unit_number,network_id=pole.electric_network_id,
                        witness_role=name,witness_unit=e.unit_number,energized=true}
                end
            end end
        end
    end
    error("Coal source lacks observed owned power")
end
local function area_for(p)
    local proto=normal_proto("electric-mining-drill")
    local radius=proto.get_mining_drill_radius and proto.get_mining_drill_radius("normal") or proto.mining_drill_radius
    assert(type(radius)=="number" and radius>0 and radius<=3 and proto.resource_drain_rate_percent==100
        and proto.drops_full_belt_stacks==false,"Unsupported mining radius or drain/stack semantics")
    return box(p,radius)
end
local function resource_set(row,retain)
    local _,ch=actor();local b=row.mining_area
    local found=ch.surface.find_entities_filtered{area={{b.left_top.x,b.left_top.y},{b.right_bottom.x,b.right_bottom.y}},type="resource",limit=65}
    assert(#found<=64,"Coal resource bound")
    local refs,total={},0
    for _,e in ipairs(found) do
        local proto=e.prototype;local m=proto.mineable_properties
        assert(e.valid and e.name=="coal" and e.type=="resource" and e.amount>=0 and int(e.amount)
            and not proto.infinite_resource and proto.resource_category=="basic-solid"
            and m and m.minable and not m.required_fluid and #m.products==1,
            "Coal source is not finite exclusive coal")
        local product=m.products[1]
        assert(product.type=="item" and product.name=="coal" and product.amount==1
            and (product.probability or 1)==1 and not product.amount_min and not product.amount_max,
            "Variable coal yield unsupported")
        refs[#refs+1]=e;total=total+e.amount
    end
    if row.resources then
        local expected={};for _,e in ipairs(row.resources) do if e.valid then expected[e]=true end end
        for _,e in ipairs(refs) do assert(expected[e],"Coal resource replaced or added");expected[e]=nil end
        assert(next(expected)==nil,"Coal resource moved outside reserved area")
    elseif retain then row.resources=refs end
    -- Neighbour drill positions alone are insufficient: inspect their actual mining areas.
    local near=ch.surface.find_entities_filtered{area={{b.left_top.x-10,b.left_top.y-10},{b.right_bottom.x+10,b.right_bottom.y+10}},type="mining-drill",limit=65}
    assert(#near<=64,"Coal drill bound")
    for _,e in ipairs(near) do
        if not row.parts.drill or e~=row.parts.drill.entity then
            assert(e.mining_area and not overlaps({left_top=point(e.mining_area.left_top),right_bottom=point(e.mining_area.right_bottom)},b),
                "Foreign drill intersects reserved coal")
        end
    end
    assert(int(total),"Invalid resource total");return total
end
local function target_ok(row) return a.endpoint_ok(row.target,"coal",false) end
local function paid_ok(row)
    target_ok(row)
    for _,spec in ipairs(row.steps) do
        local part=row.parts[spec.part]
        if part then
            local e=a.owner(part.role)
            assert(e==part.entity and e.unit_number==part.unit_number and e.name==spec.name
                and same(e.position,spec.position) and e.direction==spec.direction and part.paid==1,"Coal paid source changed")
            if spec.part=="chest" then
                assert(same(point(e.bounding_box.left_top),row.chest_bounds.left_top) and same(point(e.bounding_box.right_bottom),row.chest_bounds.right_bottom),"Coal chest bounds changed")
            else
                local modules=e.get_module_inventory()
                assert((not modules or modules.is_empty()) and e.productivity_bonus==0 and e.drop_target==row.parts.chest.entity,
                    "Coal drill output or yield changed")
                assert(e.mining_area and same(point(e.mining_area.left_top),row.mining_area.left_top)
                    and same(point(e.mining_area.right_bottom),row.mining_area.right_bottom),"Coal mining area changed")
            end
        end
    end
end
local function corridor(row)
    for _,cell in pairs(r.cells) do if cell.source.role==role(row.target.role,"chest") then
        assert(cell.target.unit_number==row.target.unit_number and #cell.steps==#row.corridor,"Coal corridor changed")
        for i,s in ipairs(row.corridor) do local t=cell.steps[i];assert(t.part==s.part and t.direction==s.direction and same(t.position,s.position),"Coal corridor geometry changed") end
        return cell
    end end
end
-- Preserve the full future geometry of both project families before payment.
local function corridor_reservations_clear(cell,row)
    if cell.source.role==role(row.target.role,"chest") then return end
    for _,step in ipairs(cell.steps) do
        for _,reserved in ipairs(row.corridor) do
            assert(math.abs(step.position.x-reserved.position.x)+math.abs(step.position.y-reserved.position.y)>1.01,
                "Coal/solid corridor reservation conflict")
        end
        local footprint=box(step.position,.49)
        assert(not overlaps(footprint,row.chest_bounds) and not overlaps(footprint,row.drill_bounds),
            "Coal source footprint reservation conflict")
    end
end
q.corridor_reservations_clear=function(cell)
    if q.committed then for _,row in pairs(q.rows) do corridor_reservations_clear(cell,row) end end
end
local function clear(row,observing)
    for _,cell in pairs(r.cells) do corridor_reservations_clear(cell,row) end
    paid_ok(row)
    local p=actor();local remaining=resource_set(row,false)
    assert(remaining>=100 or row.parts.drill,"Coal source too small or depleted")
    for _,spec in ipairs(row.steps) do if not row.parts[spec.part] then
        assert(p.surface.can_place_entity{name=spec.name,position=spec.position,direction=spec.direction,force=p.force,
            build_check_type=defines.build_check_type.manual},"Coal construction obstructed")
    end end
    local ok,evidence=pcall(function()
        local proof=power(row.steps[2].position)
        for _,spec in ipairs(row.corridor) do if spec.name=="inserter" then power(spec.position) end end
        return proof
    end)
    if ok then row.power=evidence
    elseif observing and row.power then row.power.energized=false;row.reason="no_power"
    else error("Coal source lacks observed owned power") end
    row.remaining=remaining
end
local function network_source(name)
    for target in pairs(q.rows) do if name==role(target,"chest") then return true end end
    return false
end
local function kit(extra)
    local bill={}
    for _,row in pairs(q.rows) do
        for _,s in ipairs(row.steps) do if not row.parts[s.part] then bill[s.name]=(bill[s.name] or 0)+1 end end
        local cell=corridor(row)
        for _,s in ipairs(row.corridor) do if not cell or not cell.parts[s.part] then bill[s.name]=(bill[s.name] or 0)+1 end end
    end
    local function add_other(cell)
        if network_source(cell.source.role) then return end -- Already in the coal bundle.
        for _,s in ipairs(cell.steps) do
            if not cell.parts[s.part] then bill[s.name]=(bill[s.name] or 0)+1 end
        end
    end
    for _,cell in pairs(r.cells) do add_other(cell) end
    if extra and not r.cells[extra.route] then add_other(extra) end
    return bill
end
local function affordable(extra)
    local p=actor();assert(p.crafting_queue_size==0,"Coal kit is locked while crafting")
    for name,n in pairs(kit(extra)) do assert(a.stock(p.get_main_inventory(),name,false)>=n,"Incomplete coal network construction kit") end
    return p
end
local function full_clear()
    assert(count(q.rows)==#q.targets and #q.targets>=2,"Coal bundle incomplete")
    local seen={}
    for _,row in pairs(q.rows) do
        assert(not row.fault and not row.manual_pending,"Coal source requires reconciliation")
        for _,other in ipairs(seen) do
            corridor_reservations_clear({source={role=role(row.target.role,"chest")},steps=row.corridor},other)
        end
        clear(row)
        local cell=corridor(row)
        if cell then
            assert(not cell.fault,"Coal receiving corridor requires reconciliation")
            -- Validate every paid receiving component as well as future cells.
            -- clear accepts healthy partial prefixes; full topology is not
            -- required before the bundle has finished commissioning.
            a.clear(cell)
        end
        seen[#seen+1]=row
    end
end
local function source_for(s)
    for _,row in pairs(q.rows) do if s==role(row.target.role,"chest") then return row end end
end
local function qualify(target,used)
    local endpoint,e=a.endpoint(target,"coal","fuel",false);local p=actor()
    local tries=0
    for direction,v in ipairs(vec) do
        local b=endpoint.bounds
        for x=math.floor(b.left_top.x)-1,math.ceil(b.right_bottom.x)+1 do
            for y=math.floor(b.left_top.y)-1,math.ceil(b.right_bottom.y)+1 do
                local receive={x=x+.5,y=y+.5}
                if not a.inside(receive,b) and a.inside(add(receive,v,1),b) then for n=1,24 do
                    tries=tries+1;assert(tries<=512,"Coal layout search bound")
                    local send=add(receive,v,-n-1);local chest=add(send,v,-1)
                    local steps={{part="receive",name="inserter",position=receive,direction=((direction-1)*4+8)%16}}
                    for i=n,1,-1 do steps[#steps+1]={part="belt:"..i,name="transport-belt",position=add(send,v,i),direction=(direction-1)*4} end
                    steps[#steps+1]={part="send",name="inserter",position=send,direction=((direction-1)*4+8)%16}
                    local proto=normal_proto("electric-mining-drill");local drop=point(proto.vector_to_place_result)
                    for d=0,3 do
                        local delta={x=drop.x,y=drop.y};for _=1,d do delta={x=-delta.y,y=delta.x} end
                        local drill={x=chest.x-math.floor(delta.x+.5),y=chest.y-math.floor(delta.y+.5)}
                        local row={target=endpoint,steps={{part="chest",name="wooden-chest",position=chest,direction=0},
                            {part="drill",name="electric-mining-drill",position=drill,direction=d*4}},corridor=steps,
                            chest_bounds=footprint("wooden-chest",chest),drill_bounds=footprint("electric-mining-drill",drill),
                            mining_area=area_for(drill),parts={},manual_total=0,manual_receipts={}}
                        local ok=pcall(function()
                            assert(a.inside(add(drill,delta,1),row.chest_bounds),"Coal drop misses chest")
                            assert(not overlaps(row.drill_bounds,row.chest_bounds) and not overlaps(row.drill_bounds,endpoint.bounds),"Coal source collision")
                            for _,old in pairs(used) do
                                assert(not overlaps(row.mining_area,old.mining_area),"Shared coal patch")
                                corridor_reservations_clear({source={role=role(target,"chest")},steps=steps},old)
                            end
                            local footprints={row.chest_bounds,row.drill_bounds}
                            for _,s in ipairs(steps) do footprints[#footprints+1]=box(s.position,.4) end
                            for i,f in ipairs(footprints) do
                                for j=1,i-1 do assert(not overlaps(f,footprints[j]),"Coal self collision") end
                                for _,old in pairs(used) do for _,g in ipairs(old.footprints) do assert(not overlaps(f,g),"Coal branch collision") end end
                            end
                            for _,s in ipairs(steps) do assert(p.surface.can_place_entity{name=s.name,position=s.position,direction=s.direction,force=p.force,
                                build_check_type=defines.build_check_type.manual},"Coal corridor obstruction") end
                            clear(row);row.remaining=resource_set(row,true);row.footprints=footprints
                        end)
                        if ok then return row end
                    end
                end end
            end
        end
    end
    error("No qualified bounded coal source")
end
q.corridor_offer=function(intent)
    local row=source_for(intent.source)
    if not row then
        for _,t in ipairs(q.targets) do if intent.source==role(t,"chest") then return true,nil end end
        return false,nil
    end
    assert(intent.target==row.target.role and intent.item=="coal" and intent.destination=="fuel","Coal intent changed")
    if not row.parts.chest or row.fault then return true,nil end
    paid_ok(row)
    local src=a.endpoint(intent.source,"coal","chest",true)
    return true,{route="solid:"..src.unit_number..":"..row.target.unit_number..":coal:fuel",item="coal",
        source=src,target=row.target,steps=row.corridor,parts={}}
end
q.allow_source=function(cell,allowed)
    local row=source_for(cell.source.role)
    if row then
        paid_ok(row)
        if row.parts.drill then allowed[row.parts.drill.unit_number]=true end
    end
end
q.construction_gate=function(cell,receipt)
    local row=source_for(cell.source.role)
    if not row and not q.committed then return end -- Unpaid coal proposals own no stock.
    -- Any paid construction consumes capacity protected by the committed bundle.
    -- Recheck native ownership/resources/power even for an unrelated corridor;
    -- the last observation cannot authorize payment after approach/world changes.
    full_clear()
    for _,other in pairs(r.cells) do
        assert(not other.fault,"Mixed receiving corridor requires reconciliation")
        assert(not other.pending or (other==cell and other.pending.phase=="prepared"
            and other.pending.receipt==receipt),"Another mixed corridor action requires reconciliation")
    end
    affordable(cell) -- Include the selected downstream kit once, plus other paid prefixes.
    for _,x in pairs(q.rows) do
        for _,part in pairs(x.parts) do assert(part.receipt~=receipt,"Coal receipt reused as corridor payment") end
        assert(not x.pending and not x.manual_pending and not x.fault,"Coal source action requires reconciliation")
    end
end
local function next_spec(row) for _,s in ipairs(row.steps) do if not row.parts[s.part] then return s end end end
local function params(args)
    assert(type(args)=="table" and count(args)==4 and text(args.target,48) and text(args.layout)
        and (args.part=="chest" or args.part=="drill") and text(args.receipt),"Invalid coal command")
    local row=q.rows[args.target];assert(row and row.layout==args.layout and not row.fault,"Coal offer changed")
    for _,other in pairs(q.rows) do
        for part,paid in pairs(other.parts) do if paid.receipt==args.receipt then assert(other==row and part==args.part,"Coal receipt reused") end end
        if other.pending then assert(other==row and other.pending.part==args.part and other.pending.receipt==args.receipt,"Another coal action pending") end
    end
    for _,cell in pairs(r.cells) do
        assert(not cell.pending,"Corridor action still pending")
        for _,paid in pairs(cell.parts) do assert(paid.receipt~=args.receipt,"Corridor receipt reused for source") end
    end
    if row.parts[args.part] then assert(row.parts[args.part].receipt==args.receipt,"Coal paid receipt changed");paid_ok(row);return row,nil end
    local s=next_spec(row);assert(s and s.part==args.part,"Coal prefix order changed");return row,s
end
local function fuel_energy(row)
    local e=target_ok(row);local b=e.burner;local value=prototypes.item.coal.fuel_value
    assert(b and type(value)=="number" and value>0,"Coal fuel energy unavailable")
    local n=a.stock(e.get_fuel_inventory(),"coal",true)
    local burning=b.currently_burning
    assert(not burning or (type(burning.name)=="string" and burning.name or burning.name.name)=="coal"
        and (not burning.quality or burning.quality.name=="normal"),"Mixed active fuel")
    assert(type(b.remaining_burning_fuel)=="number" and b.remaining_burning_fuel>=0
        and type(b.heat)=="number" and b.heat>=0,"Coal burner energy unavailable")
    -- Include burner heat: converting chemical fuel into stored heat is NOT consumption.
    return n*value+b.remaining_burning_fuel+b.heat,value
end
local function finish(row)
    local pending=row.pending;if not pending or pending.phase~="placed" then return end
    local e=pending.entity;local p=actor();local s=pending.spec
    assert(e and e.valid and e.surface==p.surface and e.force==p.force and e.name==s.name
        and e.unit_number==pending.unit_number and e.quality and e.quality.name=="normal"
        and same(e.position,s.position) and e.direction==s.direction and pending.after==pending.before-1,"Coal payment mismatch")
    local name=role(row.target.role,s.part)
    assert(not c.entities[name] or c.entities[name]==e,"Coal role already occupied")
    for other,owned in pairs(c.entities) do assert(other==name or owned~=e,"Coal entity already owned") end
    c.entities[name]=e
    row.parts[s.part]={entity=e,role=name,unit_number=e.unit_number,receipt=pending.receipt,paid=1}
    paid_ok(row)
    row.pending=nil
end
q.prepare=function(args,quiet)
    local row,s=params(args)
    if not s then local e=row.parts[args.part].entity;return a.reply{name=e.name,position=e.position,already_paid=true} end
    assert(not row.pending or row.pending.phase=="prepared","Ambiguous source dispatch")
    full_clear();affordable()
    if s.part=="drill" then
        local cell=corridor(row);assert(cell and not cell.fault and not cell.pending and a.topology(cell),"Coal receiver must precede mining")
        assert(a.stock(row.parts.chest.entity.get_inventory(defines.inventory.chest),"coal",true)==0,"Coal source not empty at bootstrap")
        for _,part in pairs(cell.parts) do
            local e=part.entity
            if e.type=="inserter" then assert(not e.held_stack.valid_for_read,"Coal arm not empty") end
            if e.type=="transport-belt" then for lane=1,2 do assert(e.get_transport_line(lane).get_item_count()==0,"Coal belt not empty") end end
        end
        fuel_energy(row)
    end
    q.committed=true
    row.pending=row.pending or {part=s.part,receipt=args.receipt,phase="prepared",spec=s}
    local result={name=s.name,position=s.position}
    return quiet and result or a.reply(result)
end
q.build=function(args)
    local row,s=params(args);if not s then return {already_paid=true} end
    local journal=row.pending;assert(journal and journal.phase=="prepared","Coal source not prepared")
    q.prepare(args,true) -- Recheck actual identities, kit and empty receiving path after movement.
    local p=actor()
    if s.part=="drill" then
        row.base_ore=resource_set(row,false);row.base_energy,row.fuel_value=fuel_energy(row)
        row.base_manual=row.manual_total;row.first_tick=game.tick;row.positive=0;row.delivered=0;row.last_positive=game.tick
    end
    journal.before=p.get_item_count(s.name);journal.phase="dispatching"
    local receipt=fair.place(s.name,s.position,s.direction)
    local e=p.surface.find_entity(s.name,s.position);local after=p.get_item_count(s.name)
    assert(e and e.valid and receipt.unit_number==e.unit_number and receipt.name==s.name and same(receipt.position,s.position)
        and after==journal.before-1,"Coal native placement payment mismatch")
    journal.entity,journal.unit_number,journal.after,journal.phase=e,e.unit_number,after,"placed"
    finish(row);return {placed=true}
end
c.prepare_coal_source,c.build_coal_source=q.prepare,q.build
q.handles=function(name,item)
    for t,row in pairs(q.rows) do if q.committed and (name==role(t,"chest") or name==role(t,"drill") or name==t and item=="coal") then return true end end
    return false
end
q.transfer=function(delegate,name,item,n,receipt,extracting)
    local row=q.rows[name]
    assert(row and item=="coal" and not extracting and int(n) and n>=1 and n<=200 and text(receipt),"Coal source is exclusively owned")
    paid_ok(row);assert(not row.fault and not row.pending,"Coal source requires reconciliation")
    local cell=corridor(row);assert(not cell or not cell.pending,"Coal corridor is pending")
    assert(not row.manual_pending and not row.manual_receipts[receipt] and not (c.receipts and c.receipts[receipt])
        and count(row.manual_receipts)<512,"Manual coal receipt requires reconciliation")
    local j={receipt=receipt,quantity=n,phase="dispatching",unit_number=row.target.unit_number};row.manual_pending=j
    local started_tick=game.tick
    local ok,value=pcall(delegate,name,item,n,receipt,extracting)
    local evidence=c.receipts and c.receipts[receipt]
    if evidence and evidence.role==name and evidence.item==item and evidence.quantity==n
        and evidence.unit_number==j.unit_number and evidence.extracting==false
        and int(evidence.tick) and evidence.tick>=started_tick and evidence.tick<=game.tick then
        j.phase="applied";row.manual_receipts[receipt]=n;row.manual_total=row.manual_total+n;row.manual_pending=nil
    else row.fault="manual_transfer_ambiguous" end
    if not ok then error(value) end
    assert(not row.manual_pending,"Manual coal receipt missing");return value
end
q.sample=function(cell,pipe)
    local row=source_for(cell.source.role);if not row then return false end
    paid_ok(row);assert(not row.fault and not row.manual_pending,"Coal flow requires reconciliation")
    row.route=cell.route
    if not row.parts.drill then cell.reason="source_unbuilt";cell.flow=nil;return true end
    -- This call is inside the same atomic observe that refreshed every reserved resource.
    local remaining=row.remaining;assert(remaining<=row.base_ore,"Coal resource counter increased")
    local mined=row.base_ore-remaining
    local contained=a.stock(row.parts.chest.entity.get_inventory(defines.inventory.chest),"coal",true)+pipe
    assert(contained<=mined,"Coal appears without source depletion")
    local lower=math.max(0,mined-contained-1);local upper=mined-contained -- one native internal drill item is unobserved
    local energy,value=fuel_energy(row);local manual=row.manual_total-row.base_manual
    assert(energy<=row.base_energy+(upper+manual)*value+1,"Coal target energy appears without provenance")
    lower=math.max(row.delivered,lower) -- cumulative delivery lower bounds cannot be lost in a drill buffer transition
    local consumed=math.max(0,row.base_energy+(lower+manual)*value-energy)
    local previous=row.last_sample
    if previous then
        assert(game.tick>=previous.tick and mined>=previous.mined,"Coal observation counters regressed")
        if game.tick==previous.tick then assert(mined==previous.mined and contained==previous.contained
            and energy==previous.energy+(manual-previous.manual)*value and manual>=previous.manual,"Coal same-tick state changed") end
    end
    if lower>row.delivered then row.positive=row.positive+1;row.last_positive=game.tick end
    row.delivered=math.max(row.delivered,lower)
    row.remaining=remaining
    row.last_sample={tick=game.tick,mined=mined,contained=contained,energy=energy,manual=manual}
    row.flow={layout=row.layout,route_layout=cell.layout,method="exclusive_mined_coal_lower_bound",first_tick=row.first_tick,
        last_tick=game.tick,last_positive_tick=row.last_positive,positive_samples=row.positive,mined=mined,delivered_lower=lower,
        manual_inserted=manual,drill_unit=row.parts.drill.unit_number,chest_unit=row.parts.chest.unit_number,target_unit=row.target.unit_number,
        burned_lower_joules=consumed,initial_fuel_joules=row.base_energy,target_fuel_joules=energy,fuel_value_joules=value,
        in_transit_uncertainty=1,self_fuel=0}
    cell.flow={layout=cell.layout,source_unit=cell.source.unit_number,target_unit=cell.target.unit_number,
        method="exclusive_mined_coal_lower_bound",first_tick=row.first_tick,last_tick=game.tick,last_positive_tick=row.last_positive,
        positive_samples=row.positive,sent=mined,received=lower,unattributed_loss=0}
    if row.parts.drill.entity.energy<=0 or cell.parts.send.entity.energy<=0 or cell.parts.receive.entity.energy<=0 then row.reason="no_power"
    elseif remaining==0 then row.reason="depleted"
    elseif target_ok(row).get_fuel_inventory().get_insertable_count("coal")==0 then row.reason="backpressure"
    else row.reason="observing_flow" end
    cell.reason=row.reason;return true
end
q.before_observe=function()
    actor()
    if not q.committed then
        local retained=count(q.rows)==#q.targets and #q.targets>=2
        if retained then retained=pcall(full_clear) end
        if not retained then
            -- Negative survey caching suppresses repeated bounded searches. It is
            -- never positive placement permission: prepare rechecks everything.
            q.rows={}
            if q.last_survey_tick and game.tick-q.last_survey_tick<600 then return end
            q.last_survey_tick=game.tick
            local proposed={};local ok=pcall(function()
                for _,t in ipairs(q.targets) do proposed[t]=qualify(t,proposed) end
            end)
            q.rows={}
            if ok and #q.targets>=2 then
                q.serial=q.serial+1;for _,row in pairs(proposed) do row.layout="coal-network:"..q.serial end
                q.rows=proposed;q.reason="proposal"
            else q.reason="no_supported_bundle" end
        end
    else
        for _,row in pairs(q.rows) do
            if row.pending and row.pending.phase=="placed" then
                if not pcall(finish,row) then row.fault="receipt_reconciliation_failed" end
            elseif row.pending and row.pending.phase=="dispatching" then row.fault="ambiguous_dispatch" end
            if row.manual_pending then row.fault="manual_transfer_ambiguous" end
            if not row.fault and not pcall(clear,row,true) then row.fault="identity_topology_or_balance_mismatch" end
        end
    end
end
q.snapshot=function()
    local p,ch=actor();local rows={}
    for target,row in pairs(q.rows) do
        local parts={};for key,v in pairs(row.parts) do parts[key]={role=v.role,unit_number=v.unit_number,receipt=v.receipt,paid=1} end
        local cell=corridor(row)
        if cell and cell.fault then row.fault="identity_topology_or_balance_mismatch" end
        local state=not q.committed and "proposed" or (row.parts.drill and row.flow and (row.remaining==0 and "depleted" or "ready") or "building")
        rows[target]={target=cp(row.target),layout=row.layout,steps=cp(row.steps),corridor=cp(row.corridor),
            chest_bounds=cp(row.chest_bounds),drill_bounds=cp(row.drill_bounds),mining_area=cp(row.mining_area),parts=parts,
            state=row.fault and "fault" or state,remaining=row.remaining,power=cp(row.power),route=cell and cell.route or "",
            flow=not row.fault and row.flow or {},reason=row.fault or row.reason or (q.committed and "building" or "proposal"),
            pending=row.pending and {part=row.pending.part,receipt=row.pending.receipt,phase=row.pending.phase} or {},
            manual_pending=row.manual_pending or {}}
    end
    local result={protocol=1,session_id=storage.jev_session_id,tick=game.tick,actor_index=p.index,surface_index=ch.surface.index,
        force_index=ch.force.index,targets=cp(q.targets),committed=q.committed,sources=rows,reason=q.reason}
    if q.admission_evidence then
        -- Electric network membership alone cannot attribute generation fuel or
        -- bound competing loads. Expose this absence explicitly and fail closed.
        result.protocol=2
        result.admission={protocol=1,session_id=result.session_id,tick=result.tick,
            actor_index=result.actor_index,surface_index=result.surface_index,force_index=result.force_index,
            qualified=false,reason="electric_conversion_and_construction_cost_unknown"}
    end
    return result
end
c.set_coal_admission_evidence=set_admission_evidence
c.set_coal_targets=function(targets)
    assert(type(targets)=="table" and #targets>=2 and #targets<=4 and count(targets)==#targets,"Coal needs two to four targets")
    local seen,binding={},{}
    for i,t in ipairs(targets) do
        assert(text(t,48) and not t:find("^coal:") and not seen[t],"Invalid coal consumer role");seen[t]=true;binding[i]=#t..":"..t
        local matched=false
        for _,intent in ipairs(r.intents) do
            if intent.source==role(t,"chest") then
                assert(not matched and intent.target==t and intent.item=="coal" and intent.destination=="fuel","Coal/solid intent mismatch")
                matched=true
            end
        end
        assert(matched,"Coal/solid intent mismatch")
    end
    for _,intent in ipairs(r.intents) do
        local dedicated=false
        for _,t in ipairs(targets) do if intent.source==role(t,"chest") then dedicated=true end end
        assert(dedicated or (intent.destination=="input" and intent.item~="coal" and not intent.source:find("^coal:") and not intent.target:find("^coal:")),
            "Extra transport must be independent downstream input work")
    end
    binding=table.concat(binding,"\n");assert(not q.binding or q.binding==binding,"Coal treatment immutable")
    q.binding,q.targets=binding,cp(targets);return {configured=#targets}
end
-- Used by the ordinary fair actor, not a new mining command or provider.
q.resource_reserved=function(e)
    if not q.committed or not e or not e.valid then return false end
    for _,row in pairs(q.rows) do if a.inside(point(e.position),row.mining_area) then return true end end
    return false
end
q.placement_reserved=function(name,p,direction)
    if not q.committed then return false end
    for _,row in pairs(q.rows) do
        local exact=false
        if row.pending and row.pending.phase=="dispatching" then local s=row.pending.spec;exact=s.name==name and s.direction==direction and same(s.position,p) end
        local cell=corridor(row)
        if cell and cell.pending and cell.pending.phase=="dispatching" then local s=cell.pending.spec;exact=exact or s.name==name and s.direction==direction and same(s.position,p) end
        local prototype=prototypes.entity[name];assert(prototype and prototype.collision_box,"Unknown reserved construction prototype")
        local lt,rb=point(prototype.collision_box.left_top),point(prototype.collision_box.right_bottom)
        local b={left_top={x=p.x+lt.x,y=p.y+lt.y},right_bottom={x=p.x+rb.x,y=p.y+rb.y}}
        if direction==4 or direction==12 then b={left_top={x=p.x+lt.y,y=p.y+lt.x},right_bottom={x=p.x+rb.y,y=p.y+rb.x}} end
        for _,reserved in ipairs(row.footprints) do if overlaps(b,reserved) and not exact then return true end end
    end
    return false
end

q.external_insert_allowed=function(e,item)
    if not q.committed then return true end
    for target,row in pairs(q.rows) do
        if item=="coal" and e==c.entities[target] then return false end
        for _,part in pairs(row.parts) do if e==part.entity then return false end end
    end
    return true
end
