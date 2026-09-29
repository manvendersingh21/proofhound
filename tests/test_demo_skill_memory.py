import json

from scripts import demo_skill
from services.universal_ui.from_demo import scenario_from_recording

RECORDING = '''
page.goto("http://localhost:3000/login")
page.get_by_placeholder("name@company.com").fill("admin@example.com")
page.get_by_placeholder("name@company.com").fill("admin@example.com")
page.get_by_role("button", name="Login").click()
page.get_by_role("button", name="Login").click()
expect(page.get_by_text("New chat")).to_be_visible(timeout=15000)
'''


def _spec():
    spec, _, removed = scenario_from_recording(RECORDING, 'ai-agent-template')
    assert removed == 2
    return spec


def test_memory_records_and_skips_an_unchanged_pass(tmp_path, monkeypatch):
    monkeypatch.setenv('QA_MEMORY_PATH', str(tmp_path / 'memory.json'))
    spec = _spec()
    assert demo_skill.load_memory() == []
    assert demo_skill.last_pass([], spec) is None

    entry = demo_skill.remember(spec, {'verdict': 'PASS', 'trace_id': 'a' * 32, 'run_id': 'r1', 'findings': []})
    stored = json.loads((tmp_path / 'memory.json').read_text())
    assert stored == [entry]
    assert entry['project'] == 'ai-agent-template'
    assert entry['origin'] == 'http://localhost:3000'
    assert entry['steps'][0] == 'goto_login'
    assert demo_skill.last_pass(demo_skill.load_memory(), spec)['trace_id'] == 'a' * 32


def test_a_changed_scenario_or_a_later_failure_runs_again(tmp_path, monkeypatch):
    monkeypatch.setenv('QA_MEMORY_PATH', str(tmp_path / 'memory.json'))
    spec = _spec()
    demo_skill.remember(spec, {'verdict': 'PASS', 'trace_id': 'a' * 32, 'run_id': 'r1', 'findings': []})

    changed = json.loads(json.dumps(spec))
    changed['steps'][1]['value'] = 'someone@example.com'
    assert demo_skill.last_pass(demo_skill.load_memory(), changed) is None

    demo_skill.remember(spec, {'verdict': 'FAIL', 'trace_id': 'b' * 32, 'run_id': 'r2',
                               'findings': [{'step': 'wait_for_New_chat'}]})
    entries = demo_skill.load_memory()
    assert entries[-1]['failed_steps'] == ['wait_for_New_chat']
    assert demo_skill.last_pass(entries, spec) is None


def test_replay_prints_last_entry_and_skips_without_a_browser(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('QA_MEMORY_PATH', str(tmp_path / 'memory.json'))
    monkeypatch.setattr(demo_skill.Path, 'home', lambda: tmp_path)
    spec = _spec()
    demo_skill.write_skill('ai-agent-template', spec, 2, RECORDING)
    demo_skill.remember(spec, {'verdict': 'PASS', 'trace_id': 'c' * 32, 'run_id': 'r3', 'findings': []})

    def no_browser(*_args, **_kwargs):
        raise AssertionError('an unchanged PASS must not start the receiver')

    monkeypatch.setattr(demo_skill, 'start_receiver', no_browser)
    code = demo_skill.main(['replay', '--skill', 'ai-agent-template', '--no-agents'])
    captured = capsys.readouterr()
    assert code == 0
    assert 'last memory entry:' in captured.err and 'c' * 32 in captured.err
    assert json.loads(captured.out)['skipped'] is True
