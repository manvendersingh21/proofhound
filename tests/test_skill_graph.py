import json

from services.universal_ui.from_demo import scenario_from_recording
from services.universal_ui.skill_graph import agent_steps, executable, scenario_hash, skill_record, store_skill

SHOP = '''
page.goto("http://127.0.0.1:8765/")
page.get_by_test_id("buy").click()
page.get_by_test_id("buy").click()
expect(page.get_by_text("Created")).to_be_visible()
'''


def _labels(spec, previous=None):
    return {step['name']: step for step in store_skill('shop', spec, '/tmp/scenario.json', previous)}


def test_recorded_shop_steps_replay_without_an_agent():
    spec, _, _ = scenario_from_recording(SHOP, 'shop')
    labels = _labels(spec)
    assert labels['click_buy']['agent_required'] is False
    assert labels['wait_for_Created']['agent_required'] is False
    assert all(not step['agent_required'] for step in labels.values())
    assert agent_steps('shop', scenario_hash(spec)) == []

    record = skill_record('shop')
    assert record['scenario_hash'] == scenario_hash(spec)
    assert record['has_step'] == len(spec['steps'])
    assert record['next_count'] == len(spec['steps']) - 1


def test_a_step_without_a_locator_needs_an_agent():
    spec, _, _ = scenario_from_recording(SHOP, 'shop')
    spec['steps'].insert(2, {'name': 'mystery', 'action': 'click'})
    labels = _labels(spec)
    assert labels['mystery'] == {'name': 'mystery', 'action': 'click', 'agent_required': True,
                                 'reason': 'no locator and no path'}
    assert [step['name'] for step in agent_steps('shop', scenario_hash(spec))] == ['mystery']


def test_needs_agent_marker_and_changed_failing_scenario(tmp_path):
    marked = SHOP.replace('to_be_visible()', 'to_be_visible()  # needs_agent')
    spec, _, _ = scenario_from_recording(marked, 'shop')
    labels = _labels(spec)
    assert labels['wait_for_Created']['reason'] == 'user marked needs_agent'
    assert 'needs_agent' not in json.dumps(executable(spec))

    plain, _, _ = scenario_from_recording(SHOP, 'shop')
    previous = {'verdict': 'FAIL', 'skill_hash': 'old', 'steps': [s['name'] for s in plain['steps']],
                'failed_steps': ['wait_for_Created']}
    retried = _labels(plain, previous)
    assert retried['wait_for_Created']['agent_required'] is True
    assert retried['click_buy']['agent_required'] is False
