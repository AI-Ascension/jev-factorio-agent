"""Independent public completion controls; engine callbacks are offline doubles."""
import json
import hashlib
from pathlib import Path
import pytest
from test_native_optional_profiles import _case
from test_native_owner_completion_attachment509 import _make_native_json_faithful, _CompletionProfileClient
from jev_factorio.controller import HierarchicalLoop
from jev_factorio.provenance import source_revision
from jev_factorio.owner_completion import preflight_completed_owner


def _preflight(tmp_path):
    lua, output = _case()
    _make_native_json_faithful(lua)
    lua.execute("player=game.get_player(1);surface=player.surface")
    memory = HierarchicalLoop.memory_type(
        session_id="offline-optional-profile-session", target="rocket_launch",
        last_tick=1234, status="completed", completed_goals={"rocket_launch": 1234},
        history=[{"kind":"goal_completed", "goal":"rocket_launch", "tick":1234}])
    checkpoint = tmp_path / "completed.json"
    memory.save(checkpoint)
    owner = tmp_path / "owner.py"
    owner.write_text("# offline owner fixture\n")
    digest = hashlib.sha256(owner.read_bytes()).hexdigest()
    root = Path(__file__).resolve().parents[1]
    pin = source_revision(root)
    kwargs = dict(target="rocket_launch",source_root=root,expected_source_revision=pin,
                  startup_source_revision=pin,owner_path=owner,expected_owner_sha256=digest,
                  startup_owner_sha256=digest,native_identity={"tick":1234,"speed":1,
                  "marked":True,"runtime":True,"session_id":memory.session_id,"connected":1},
                  client=_CompletionProfileClient(lua,output))
    return lua, kwargs["client"], checkpoint, kwargs


def native_state(lua):
    return json.loads(lua.eval('helpers.table_to_json({job=storage.fair.job, '
                              'walking=player.walking_state, mining=player.mining_state})'))


def begin_path(lua):
    # The real source producer constructs the pending job and calls the
    # fixture's Factorio request_path API. No query/preflight code is patched.
    lua.execute('player.position={x=0,y=0}; '
                'player.character.prototype={collision_box={},collision_mask={}}; '
                'surface.request_path=function(spec) return 177 end; '
                'storage.fair.begin_move({x=10,y=0})')
    assert lua.eval('storage.fair.job.status') == 'path_pending'
    assert lua.eval('storage.fair.job.request') == 177


@pytest.mark.parametrize('case', ['path_pending','walking_control','mining_control','malformed_job'])
def test_public_completed_preflight_rejects_unresolved_native_action(tmp_path, case):
    lua, client, checkpoint, kwargs = _preflight(tmp_path)
    if case == 'path_pending':
        begin_path(lua)
    elif case == 'walking_control':
        lua.execute('player.walking_state={walking=true,direction=0}')
    elif case == 'mining_control':
        lua.execute('player.mining_state={mining=true,position={x=2,y=0}}')
    else:
        lua.execute('storage.fair.job={status="unknown"}')
    before, raw = native_state(lua), checkpoint.read_bytes()
    with pytest.raises(Exception):
        preflight_completed_owner(checkpoint, **kwargs)
    assert native_state(lua) == before
    assert checkpoint.read_bytes() == raw


def test_public_completed_preflight_retains_settled_native_action(tmp_path):
    lua, client, checkpoint, kwargs = _preflight(tmp_path)
    begin_path(lua)
    lua.execute('storage.fair.stop()')
    before, raw = native_state(lua), checkpoint.read_bytes()
    assert preflight_completed_owner(checkpoint, **kwargs).observation_sha256
    assert native_state(lua) == before
    assert checkpoint.read_bytes() == raw
