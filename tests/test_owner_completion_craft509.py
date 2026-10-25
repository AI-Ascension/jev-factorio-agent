"""Public completion must retain native craft obligations (offline Lua controls)."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from jev_factorio.owner_completion import preflight_completed_owner
from jev_factorio.provenance import source_revision
from test_native_owner_completion_attachment509 import _source_generated_successor_checkpoint


def _preflight(tmp_path):
    lua, client, snapshot, _loaded, checkpoint = _source_generated_successor_checkpoint(tmp_path)
    owner = tmp_path / "owner.py"
    owner.write_text("# offline owner fixture\n", encoding="utf-8")
    digest = hashlib.sha256(owner.read_bytes()).hexdigest()
    root = Path(__file__).resolve().parents[1]
    pin = source_revision(root)
    kwargs = dict(
        target="rocket_launch", source_root=root,
        expected_source_revision=pin, startup_source_revision=pin,
        owner_path=owner, expected_owner_sha256=digest, startup_owner_sha256=digest,
        native_identity={"tick": snapshot.tick, "speed": 1, "marked": True,
                         "runtime": True, "session_id": snapshot.session_id, "connected": 1},
        client=client)
    return lua, client, checkpoint, kwargs


def _craft_state(lua):
    return lua.eval('helpers.table_to_json({job=storage.campaign.craft_jobs.job, '
                    'queue=player.crafting_queue, size=player.crafting_queue_size, '
                    'submitting=storage.campaign.craft_jobs.submitting or false, '
                    'begin_calls=begin_calls, stock=stock})')


def test_settled_native_craft_remains_valid_completion_evidence(tmp_path):
    lua, client, checkpoint, kwargs = _preflight(tmp_path)
    assert lua.eval('storage.campaign.craft_jobs.job.status') == 'completed'
    before, checkpoint_before = _craft_state(lua), checkpoint.read_bytes()
    assert preflight_completed_owner(checkpoint, **kwargs).observation_sha256
    assert _craft_state(lua) == before
    assert checkpoint.read_bytes() == checkpoint_before


@pytest.mark.parametrize("mutation", [
    # The existing public producer creates a paid running job and native queue.
    'stock["iron-plate"]=2; c.begin_craft_job("independent-active:1","iron-gear-wheel",1)',
    'player.crafting_queue_size=1; player.crafting_queue={{recipe="iron-gear-wheel",count=1}}',
    'storage.campaign.craft_jobs.submitting=true',
    'storage.campaign.craft_jobs.job.status="invalid"',
    'storage.campaign.craft_jobs.job.finished=0',
    'storage.campaign.craft_jobs.job.unit_number=999999',
    'storage.campaign.craft_jobs.job.completed_tick=game.tick+1',
])
def test_public_completion_rejects_native_craft_obligations_without_mutation(tmp_path, mutation):
    lua, client, checkpoint, kwargs = _preflight(tmp_path)
    lua.execute(mutation)
    before, checkpoint_before = _craft_state(lua), checkpoint.read_bytes()
    with pytest.raises(Exception, match="craft|Craft"):
        preflight_completed_owner(checkpoint, **kwargs)
    assert _craft_state(lua) == before
    assert checkpoint.read_bytes() == checkpoint_before
