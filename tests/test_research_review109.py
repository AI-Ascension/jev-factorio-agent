"""Historical research findings through offline producer, reader and CLI paths."""
import json
import sys
from types import SimpleNamespace

import pytest

from jev_factorio import research_catalog as rc
from jev_factorio.dashboard import EventWriter, Monitor, project_state
from test_research_catalog import DATA_11, DATA_20, export_with, raw_catalog


@pytest.mark.parametrize("invalid", [[1], "not-an-object", 1, False])
def test_bad_pack_table_does_not_stop_monitor_and_valid_reload_recovers(tmp_path, invalid):
    path, events = tmp_path / rc.FILENAME, tmp_path / "events"
    raw = raw_catalog(DATA_20)
    raw['science_packs'] = invalid
    path.write_text(json.dumps(raw))
    with EventWriter(events) as writer:
        writer.emit("observation", 2, state=project_state({"tick": 1, "researched": []}))
        monitor = Monitor(events, research=path)
        monitor.poll()
        assert monitor.snapshot()['view']['research']['status'] == 'invalid'
        rc.write(path, raw_catalog(DATA_20))
        writer.emit("observation", 2, state=project_state({"tick": 2, "researched": []}))
        monitor.poll()
        assert monitor.snapshot()['view']['research']['status'] == 'ok'
        assert monitor.snapshot()['view']['state']['tick'] == 2


@pytest.mark.parametrize("count", [128, 129, rc.MAX_TECHNOLOGIES])
def test_researched_semantics_survive_actual_event_write_and_poll(tmp_path, count):
    names = [f'tech-{n}' for n in range(count)]
    raw = {'schema': rc.SCHEMA, 'version': '2.0.77', 'mods': {'base': '2.0.77'},
           'technologies': {n: {'essential': True} for n in names}, 'science_packs': {}}
    sidecar, events = tmp_path / rc.FILENAME, tmp_path / 'events'
    rc.write(sidecar, raw)
    with EventWriter(events) as writer:
        writer.emit('observation', 2, state=project_state({
            'tick': 50, 'game_version': '2.0.77', 'researched': names}))
    monitor = Monitor(events, research=sidecar)
    monitor.poll()
    view = monitor.snapshot()['view']
    assert view['research']['status'] == 'ok'
    assert view['research']['path_done'] == count
    assert view['state']['researched'] == names


@pytest.mark.parametrize("observed,expected", [('2.0.77', 'ok'), ('1.1.110', 'version mismatch')])
def test_overlapping_names_do_not_hide_game_version_mismatch(tmp_path, observed, expected):
    sidecar, events = tmp_path / rc.FILENAME, tmp_path / 'events'
    rc.write(sidecar, raw_catalog(DATA_20))
    with EventWriter(events) as writer:
        writer.emit('observation', 2, state=project_state({
            'tick': 3, 'game_version': observed, 'researched': ['electronics']}))
    monitor = Monitor(events, research=sidecar)
    monitor.poll()
    assert monitor.snapshot()['view']['research']['status'] == expected


def test_trigger_is_selected_only_if_eligible_and_no_ordinary_topic():
    tree = rc.Tree(rc.parse(raw_catalog(DATA_20)))
    first = tree.summary(set(), None, None, {})['current']
    assert first['name'] == 'electronics' and first['trigger']['type'] == 'craft-item'
    assert first['progress'] is None
    second = tree.summary({'electronics'}, None, None, {})['current']
    assert second['name'] == 'automation-science-pack'
    ordinary = tree.summary({'electronics', 'automation-science-pack'}, 'automation', .25, {})
    assert ordinary['current']['name'] == 'automation' and ordinary['current']['progress'] == .25
    assert tree.summary(set(tree.path), None, None, {})['current'] is None


@pytest.mark.parametrize("maximum,infinite", [(5, False), (2 ** 32 - 1, True)])
def test_formula_cost_does_not_remove_finite_prerequisite(maximum, infinite):
    raw = raw_catalog(DATA_20)
    raw['technologies']['automation'].update(count_formula='1000*L', max_level=maximum)
    parsed = rc.parse(raw)
    assert parsed['technologies']['automation']['infinite'] is infinite
    assert ('automation' in rc.Tree(parsed).path) is not infinite


@pytest.mark.parametrize("watch", ['nan', 'inf', '-inf', '1'])
def test_invalid_watch_rejected_before_client_or_export(tmp_path, monkeypatch, watch):
    monkeypatch.setenv('JEV_RCON_PASSWORD', 'offline-fixture')
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid watch interval reached client construction')
    monkeypatch.setitem(sys.modules, 'factorio_rcon', SimpleNamespace(RCONClient=forbidden))
    with pytest.raises(SystemExit) as error:
        rc.main(['--watch', watch, '--out', str(tmp_path / rc.FILENAME)])
    assert error.value.code == 2
    assert not (tmp_path / rc.FILENAME).exists()


@pytest.mark.parametrize('watch', [None, '2'])
def test_valid_watcher_and_single_export_remain_bounded_offline(tmp_path, monkeypatch, watch):
    monkeypatch.setenv('JEV_RCON_PASSWORD', 'offline-fixture')
    calls, sleeps = [], []
    def send(command):
        calls.append(command)
        return json.dumps(raw_catalog(DATA_20))
    monkeypatch.setitem(sys.modules, 'factorio_rcon', SimpleNamespace(
        RCONClient=lambda *args: SimpleNamespace(send_command=send)))
    def stop(seconds):
        sleeps.append(seconds)
        raise RuntimeError('bounded offline watcher stop')
    monkeypatch.setattr(rc.time, 'sleep', stop)
    path = tmp_path / rc.FILENAME
    args = ['--out', str(path)] + ([] if watch is None else ['--watch', watch])
    if watch is None:
        assert rc.main(args) == 0 and sleeps == []
    else:
        with pytest.raises(RuntimeError, match='bounded offline watcher stop'):
            rc.main(args)
        assert sleeps == [2.0]
    assert len(calls) == 1 and rc.read(path)['version'] == '2.0.77'


def test_research_list_keeps_redaction_and_incomplete_state_barriers(tmp_path, monkeypatch):
    monkeypatch.setenv('TYPESAFE_API_KEY', 'private-technology-name')
    events, sidecar = tmp_path / 'events', tmp_path / rc.FILENAME
    rc.write(sidecar, raw_catalog(DATA_20))
    with EventWriter(events) as writer:
        writer.emit('observation', 2, state=project_state({
            'tick': 3, 'researched': ['private-technology-name'],
            'factory': {'research': None, 'research_progress': None}}))
    assert 'private-technology-name' not in events.read_text()
    monitor = Monitor(events, research=sidecar)
    monitor.poll()
    view = monitor.snapshot()['view']
    assert view['research']['status'] == 'observation incomplete'
    assert view['research']['current'] is None


@pytest.mark.parametrize('events_enabled', [False, True])
def test_cli_catalog_is_written_beside_selected_output(tmp_path, monkeypatch, events_enabled):
    from jev_factorio import main
    from jev_factorio.backends.mock import MockBackend
    events, log = tmp_path / 'display/events.jsonl', tmp_path / 'logs/gameplay.jsonl'
    log.parent.mkdir()
    backend = MockBackend()
    commands = []
    def send(command):
        commands.append(command)
        return json.dumps(raw_catalog(DATA_20))
    backend._instance = SimpleNamespace(rcon_client=SimpleNamespace(send_command=send))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main, 'make_backend', lambda *args, **kwargs: backend)
    monkeypatch.setattr(sys, 'argv', ['jev-factorio', '--backend', 'fle', '--controller', 'hierarchical',
        '--policy', 'deterministic', '--target', 'bootstrap_mining', '--steps', '1', '--tick-seconds', '.01',
        '--checkpoint', str(tmp_path / 'checkpoint.json'), '--log-file', str(log)]
        + (['--dashboard-events', str(events)] if events_enabled else []))
    main.cli()
    assert len(commands) == 1 and commands[0].startswith('/sc ')
    directory = events.parent if events_enabled else log.parent
    assert (directory / rc.FILENAME).exists()
    monitor = Monitor(events if events_enabled else log, research=directory / rc.FILENAME)
    monitor.poll()
    assert monitor.research_tree is not None


@pytest.mark.parametrize('version', [None, '2.0.77'])
@pytest.mark.parametrize('binding', ['valid', 'changed_tick', 'changed_session'])
def test_trigger_fallback_requires_current_version_and_mission_identity(tmp_path, version, binding):
    events, sidecar = tmp_path / 'events', tmp_path / rc.FILENAME
    rc.write(sidecar, raw_catalog(DATA_20))
    state = {'tick': 100, 'session_id': 'trigger-session', 'researched': [],
             'factory': {'research': None, 'research_progress': None}}
    if version is not None:
        state['game_version'] = version
    projected = project_state(state)
    if binding == 'changed_tick':
        projected['tick'] += 1
    elif binding == 'changed_session':
        projected['session_id'] = 'other-session'
    with EventWriter(events) as writer:
        writer.emit('observation', 2, state=projected)
    monitor = Monitor(events, research=sidecar)
    monitor.poll()
    research = monitor.snapshot()['view']['research']
    assert research['version_identity'] == ('unknown' if version is None else 'matched')
    if version is not None and binding == 'valid':
        assert research['current']['name'] == 'electronics'
        assert research['current']['trigger']['type'] == 'craft-item'
        assert research['current']['progress'] is None
    else:
        assert research['current'] is None


@pytest.mark.parametrize('table', ['absent', None, {}])
def test_legacy_science_pack_tables_remain_readable(table):
    raw = raw_catalog(DATA_20)
    if table == 'absent':
        raw.pop('science_packs')
    else:
        raw['science_packs'] = table
    assert rc.parse(raw)['science_packs'] == {}


def test_catalog_own_state_can_display_trigger_without_controller(tmp_path):
    events, sidecar = tmp_path / 'events', tmp_path / rc.FILENAME
    raw = raw_catalog(DATA_20)
    raw['state'] = {'tick': 50, 'researched': [], 'current': None, 'progress': None}
    rc.write(sidecar, raw)
    with EventWriter(events):
        pass
    monitor = Monitor(events, research=sidecar)
    monitor.poll()
    research = monitor.snapshot()['view']['research']
    assert research['source'] == 'catalog'
    assert research['version_identity'] == 'catalog-only'
    assert research['current']['name'] == 'electronics'
    assert research['current']['progress'] is None


@pytest.mark.parametrize('api,version,data', [('1.1', '1.1.110', DATA_11), ('2.0', '2.0.77', DATA_20)])
@pytest.mark.parametrize('maximum,infinite', [(5, False), (2 ** 32 - 1, True)])
def test_actual_lua_export_keeps_formula_and_max_level_independent(api, version, data, maximum, infinite):
    data = json.loads(json.dumps(data))
    technology = data['technology']['automation']
    technology['max_level'] = maximum
    technology['unit']['count_formula'] = '1000*L'
    parsed = export_with(data, api, {'base': version})
    assert parsed['technologies']['automation']['infinite'] is infinite
    assert ('automation' in rc.Tree(parsed).path) is not infinite
