from importlib.resources import files

import pytest


def _fair_runtime(player_index=1):
    runtime = pytest.importorskip("lupa.lua54").LuaRuntime()
    runtime.globals().configured_player_index = player_index
    runtime.execute("""
        handlers = {}
        defines = {
            controllers = {character = 1},
            events = {on_tick = 1, on_script_path_request_finished = 2},
            build_check_type = {manual = 1},
            direction = {north = 0, northeast = 2, east = 4, southeast = 6,
                         south = 8, southwest = 10, west = 12, northwest = 14}
        }
        prototypes = {entity = {pipe = {}, ["offshore-pump"] = {}}}
        script = {
            get_event_handler = function(event) return handlers[event] end,
            on_event = function(event, callback) handlers[event] = callback end,
            on_nth_tick = function(interval, callback)
                handlers["nth" .. interval] = callback
            end
        }
        function make_character(unit)
            return {valid = true, unit_number = unit,
                prototype = {collision_box = {}, collision_mask = {}}}
        end
        function make_player(index)
            local character = make_character(index + 8)
            local player = {
                index = index, connected = true, character = character,
                cheat_mode = false, position = {x = 0, y = 0},
                walking_state = {walking = false},
                mining_state = {mining = false}, surface = nil, force = {},
                get_item_count = function() return 0 end,
                can_reach_entity = function() return true end,
                update_selected_entity = function(position)
                    player.selected = resource
                end
            }
            return player, character
        end
        resource = {
            valid = true, minable = true, name = "coal", surface = {index = 1},
            position = {x = 2, y = 0}
        }
        local primary, primary_character = make_player(1)
        local secondary, secondary_character = make_player(2)
        surface = {
            index = 1,
            request_path = function(parameters)
                requested_path = parameters
                return 17
            end,
            find_entities_filtered = function() return {resource} end
        }
        primary.surface, secondary.surface = surface, surface
        player = configured_player_index == 1 and primary or secondary
        other_player = configured_player_index == 1 and secondary or primary
        character = configured_player_index == 1 and primary_character or secondary_character
        players = {[1] = primary, [2] = secondary}
        game = {
            tick = 0, speed = 1,
            get_player = function(index) return players[index] end
        }
        storage = {
            jev_player_index = configured_player_index,
            agent_characters = {character}
        }
        script.on_event(defines.events.on_tick, function()
            prior_tick_calls = (prior_tick_calls or 0) + 1
        end)
        prior_tick_calls = 0
    """)
    runtime.execute(files("jev_factorio").joinpath("lua/fair_actions.lua").read_text())
    runtime.execute("storage.fair.bind()")
    return runtime


@pytest.fixture
def fair_runtime():
    return _fair_runtime()


def _start_walking(runtime):
    runtime.execute("storage.fair.begin_move{x = 2, y = 0}")
    runtime.execute("""handlers[2]{id = 17, path = {
        {position = {x = 0, y = 0}}, {position = {x = 2, y = 0}}
    }}""")
    runtime.execute("handlers[1]{}")


@pytest.mark.parametrize("kind", ["walk", "mine"])
def test_replaced_character_failure_clears_owned_player_inputs_without_rebinding(
    fair_runtime, kind
):
    runtime = fair_runtime
    if kind == "walk":
        _start_walking(runtime)
        assert runtime.eval("player.walking_state.walking") is True
        assert runtime.eval("storage.fair.job.status") == "walking"
    else:
        runtime.execute('storage.fair.begin_mine({x = 2, y = 0}, "coal", 2)')
        assert runtime.eval("player.mining_state.mining") is True

    runtime.execute("""
        saved_job_unit = storage.fair.job.unit
        saved_job_lease = storage.fair.job.lease
        saved_job_request = storage.fair.job.request
        saved_job_path_requests = storage.fair.job.path_requests
        saved_job_entity = storage.fair.job.entity
        saved_job_quantity = storage.fair.job.quantity
        other_player.walking_state = {walking = true, direction = 4}
        other_player.mining_state = {mining = true}
        replacement = make_character(99)
        replacement.marker = "must-not-be-mutated"
        player.character = replacement
        handlers[1]{}
    """)

    assert runtime.eval("storage.fair.job.status") == "failed"
    assert runtime.eval("storage.fair.job.error") == "Fair player/session invariant failed"
    assert runtime.eval("storage.fair.job.unit") == 9
    assert runtime.eval("storage.fair.job.unit == saved_job_unit") is True
    assert runtime.eval("storage.fair.job.lease == saved_job_lease") is True
    if kind == "walk":
        assert runtime.eval("storage.fair.job.request == saved_job_request") is True
        assert runtime.eval("storage.fair.job.path_requests == saved_job_path_requests") is True
    else:
        assert runtime.eval("storage.fair.job.entity == saved_job_entity") is True
        assert runtime.eval("storage.fair.job.quantity == saved_job_quantity") is True
    assert runtime.eval("player.character == replacement") is True
    assert runtime.eval("storage.agent_characters[1] == character") is True
    assert runtime.eval("replacement.marker") == "must-not-be-mutated"
    assert runtime.eval("player.walking_state.walking") is False
    assert runtime.eval("player.mining_state.mining") is False
    assert runtime.eval("other_player.walking_state.walking") is True
    assert runtime.eval("other_player.mining_state.mining") is True
    assert runtime.eval("prior_tick_calls") == 0


@pytest.mark.parametrize("broken_binding", [
    "player.connected = false",
    "storage.agent_characters[1] = nil",
    "player.character = nil",
    "character.valid = false",
])
def test_actor_failure_clears_owned_inputs_for_disconnected_or_invalid_character(
    fair_runtime, broken_binding
):
    runtime = fair_runtime
    _start_walking(runtime)
    runtime.execute("other_player.walking_state = {walking = true, direction = 4}")
    runtime.execute(broken_binding)
    runtime.execute("handlers[1]{}")

    assert runtime.eval("storage.fair.job.status") == "failed"
    assert runtime.eval("player.walking_state.walking") is False
    assert runtime.eval("player.mining_state.mining") is False
    assert runtime.eval("other_player.walking_state.walking") is True
    assert runtime.eval("storage.agent_characters[1] == nil") is (
        broken_binding == "storage.agent_characters[1] = nil"
    )


def test_nondefault_player_selection_drift_cleans_only_the_bound_player():
    runtime = _fair_runtime(player_index=2)
    runtime.execute('storage.fair.begin_mine({x = 2, y = 0}, "coal", 2)')
    runtime.execute("""
        other_player.walking_state = {walking = true, direction = 4}
        other_player.mining_state = {mining = true}
        storage.jev_player_index = 1
        handlers[1]{}
    """)

    assert runtime.eval("storage.fair.job.status") == "failed"
    assert runtime.eval("storage.fair.job.unit") == 10
    assert runtime.eval("player.walking_state.walking") is False
    assert runtime.eval("player.mining_state.mining") is False
    assert runtime.eval("other_player.walking_state.walking") is True
    assert runtime.eval("other_player.mining_state.mining") is True
    assert runtime.eval("player.character == character") is True


def test_expired_walking_lease_clears_inputs_and_ignores_late_path_for_job():
    runtime = _fair_runtime()
    _start_walking(runtime)
    runtime.execute("""
        other_player.mining_state = {mining = true}
        game.tick = 181
        handlers[1]{}
        late_path_status = storage.fair.job.status
        handlers[2]{id = 17, path = {
            {position = {x = 0, y = 0}}, {position = {x = 2, y = 0}}
        }}
    """)

    assert runtime.eval("late_path_status") == "failed"
    assert runtime.eval("storage.fair.job.status") == "failed"
    assert runtime.eval("storage.fair.job.error") == "Control lease expired or character changed"
    assert runtime.eval("player.walking_state.walking") is False
    assert runtime.eval("player.mining_state.mining") is False
    assert runtime.eval("other_player.mining_state.mining") is True


def test_path_pending_lease_expiry_clears_inputs_before_delayed_result():
    runtime = _fair_runtime()
    runtime.execute("""
        storage.fair.begin_move{x = 2, y = 0}
        assert(storage.fair.job.status == "path_pending")
        local request_id = storage.fair.job.request
        local lease = storage.fair.job.lease
        player.walking_state = {walking = true, direction = 4}
        player.mining_state = {mining = true}
        other_player.walking_state = {walking = true, direction = 4}
        game.tick = lease + 1
        handlers[1]{}
        assert(storage.fair.job.status == "failed")
        assert(storage.fair.job.error == "Control lease expired or character changed")
        assert(storage.fair.job.request == request_id)
        handlers[2]{id = request_id, path = {
            {position = {x = 0, y = 0}}, {position = {x = 2, y = 0}}
        }}
    """)

    assert runtime.eval("storage.fair.job.status") == "failed"
    assert runtime.eval("player.walking_state.walking") is False
    assert runtime.eval("player.mining_state.mining") is False
    assert runtime.eval("other_player.walking_state.walking") is True


@pytest.mark.parametrize("kind", ["walk", "mine"])
def test_same_actor_jobs_still_complete_and_clear_controls(fair_runtime, kind):
    runtime = fair_runtime
    if kind == "walk":
        _start_walking(runtime)
        runtime.execute("player.position = {x = 2, y = 0}; handlers[1]{}")
    else:
        runtime.execute('storage.fair.begin_mine({x = 2, y = 0}, "coal", 1)')
        runtime.execute("handlers[1]{}")
        assert runtime.eval("player.mining_state.mining") is True
        runtime.execute("player.get_item_count = function() return 1 end; handlers[1]{}")

    assert runtime.eval("storage.fair.job.status") == "completed"
    assert runtime.eval("player.walking_state.walking") is False
    assert runtime.eval("player.mining_state.mining") is False
