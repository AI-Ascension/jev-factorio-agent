"""Additional malformed native actor controls; all engine state is simulated."""
import pytest

from jev_factorio.owner_completion import preflight_completed_owner
from test_independent_owner509_native_actions import _preflight, begin_path, native_state


@pytest.mark.parametrize("mutation", [
    'storage.fair.quarantined=true',
    'storage.fair.mining_target={token=1}',
    'storage.fair.job.unit=999999',
    'storage.fair.job.cleanup_result="failed"',
    'storage.fair.job.kind="unknown"',
    'player.walking_state=nil',
    'player.mining_state={}',
])
def test_unknown_or_unsettled_native_state_preserves_obligation(tmp_path, mutation):
    lua, _client, checkpoint, kwargs = _preflight(tmp_path)
    begin_path(lua)
    lua.execute('storage.fair.stop()')
    lua.execute(mutation)
    before, raw = native_state(lua), checkpoint.read_bytes()
    with pytest.raises(Exception, match="Native actor"):
        preflight_completed_owner(checkpoint, **kwargs)
    assert native_state(lua) == before
    assert checkpoint.read_bytes() == raw


def test_terminal_failed_native_job_is_preserved_when_controls_were_cleared(tmp_path):
    lua, _client, checkpoint, kwargs = _preflight(tmp_path)
    begin_path(lua)
    lua.execute('storage.fair.stop("Retained historical failure")')
    assert lua.eval('storage.fair.job.status') == 'failed'
    before, raw = native_state(lua), checkpoint.read_bytes()
    assert preflight_completed_owner(checkpoint, **kwargs).observation_sha256
    assert native_state(lua) == before
    assert checkpoint.read_bytes() == raw
