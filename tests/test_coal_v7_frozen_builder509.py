"""Preserve the v7 builder contract while completion adds a current helper."""
from hashlib import sha256
from importlib.resources import files
from pathlib import Path

import pytest
from lupa.lua52 import LuaRuntime

from jev_factorio import coal_economic_v7 as v7
from test_coal_native_admission_v1 import _qualified_lua, _run_first_spend

ROOT = Path(__file__).resolve().parents[1]
LUA = ROOT / 'src/jev_factorio/lua'
FROZEN_BUILDER_SHA256 = '3ec3b94b03c86cf963328ef9a6f75551ab285968ccfd50d2e2a25c72e89a242e'


def test_v7_queries_remain_byte_identical_to_main_before_completion_helpers():
    # Independently captured from b1a8dd180d95968b95278e7c2d2ff5b930d6eae5.
    cases = (
        (v7.fixed_query(), 99109,
         'c7134f44c5b3de86bef21c33c959df8a724b08177b81a2e64093f3454a7f84c2'),
        (v7.fixed_first_spend_query({'target': 'recipe:copper-plate',
            'layout': 'layout:fixture', 'part': 'chest', 'receipt': 'a' * 64}),
         104044, '9825aebfe7297bab2ab5564e883470e28c774ba869670014ac3105a831108ee2'),
        (v7.fixed_admission_journal_readback_query(), 9049,
         'c2ebc469957ad95fedbfb4004ad74bc39036c96255f9509c23194008c74166ed'),
    )
    for query, expected_bytes, expected_sha256 in cases:
        raw = query.encode('utf-8')
        assert len(raw) == expected_bytes
        assert sha256(raw).hexdigest() == expected_sha256


def test_frozen_builder_is_packaged_with_its_original_hash_and_real_paid_behavior():
    raw = files('jev_factorio').joinpath('lua/coal_supply_v7.lua').read_bytes()
    assert sha256(raw).hexdigest() == FROZEN_BUILDER_SHA256
    assert v7.NATIVE_COAL_SUPPLY_ASSET_SHA256 == FROZEN_BUILDER_SHA256
    assert sha256((LUA / 'coal_supply.lua').read_bytes()).hexdigest() != FROZEN_BUILDER_SHA256
    lua = LuaRuntime(unpack_returned_tuples=True)
    for name in ('tests/fixtures/solid_routes_runtime.lua',
                 'tests/fixtures/coal_supply_runtime.lua',
                 'src/jev_factorio/lua/solid_routes.lua'):
        lua.execute((ROOT / name).read_text())
    lua.execute(raw.decode('utf-8'))
    lua.execute('configure_coal(); p=coal_build("alpha","chest")')
    assert lua.eval('storage.coal_supply.completion_sample') is None
    assert lua.eval('paid_calls') == 1
    assert lua.eval('storage.coal_supply.rows.alpha.parts.chest.receipt') == 'coal:alpha:chest'
    # Re-reading the same already-paid receipt must not charge a second time.
    lua.execute('campaign.prepare_coal_source(p); campaign.build_coal_source(p)')
    assert lua.eval('paid_calls') == 1


@pytest.mark.parametrize('replacement', ['tampered', 'current', 'missing'])
def test_v7_compiler_rejects_missing_or_replaced_frozen_builder(tmp_path, monkeypatch, replacement):
    target = tmp_path / 'lua'
    target.mkdir()
    for name in ('coal_economics.lua', 'coal_material_census_v7.lua',
                 'coal_manual_cycle_v2.lua', 'coal_native_admission_v1.lua'):
        (target / name).write_bytes((LUA / name).read_bytes())
    if replacement != 'missing':
        raw = ((LUA / 'coal_supply.lua').read_bytes() if replacement == 'current'
               else (LUA / 'coal_supply_v7.lua').read_bytes() + b'\n-- changed\n')
        (target / 'coal_supply_v7.lua').write_bytes(raw)
    monkeypatch.setattr(v7, 'files', lambda _: tmp_path)
    with pytest.raises((RuntimeError, FileNotFoundError)):
        v7.fixed_query()


def test_current_builder_manifest_does_not_inherit_v7_payment_authority():
    lua, bundle = _qualified_lua()
    digest = sha256((LUA / 'coal_supply.lua').read_bytes()).hexdigest()
    lua.globals().jev_fle_runtime.native_installation.assets.coal_supply = digest
    envelope, _ = _run_first_spend(lua, bundle)
    assert envelope['action']['status'] == 'not_dispatched'
    assert envelope['admission']['qualified'] is False
    assert lua.eval('build_calls') == 0
    assert lua.eval('jev_fle_runtime.coal_native_admission_journal_v1') is None
