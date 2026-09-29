from services.universal_ui.from_demo import scenario_from_recording

RECORDING = '''
from playwright.sync_api import expect, sync_playwright
page.goto("http://127.0.0.1:8765/")
page.get_by_test_id("buy").click()
page.get_by_test_id("buy").click()
page.get_by_test_id("buy").click()
expect(page.get_by_text("Created")).to_be_visible()
'''


def test_consecutive_duplicate_clicks_are_removed():
    spec, origin, removed = scenario_from_recording(RECORDING, 'checkout')
    assert origin == 'http://127.0.0.1:8765'
    assert removed == 2
    actions = [(step['action'], step.get('locator', {}).get('value')) for step in spec['steps']]
    assert actions[:3] == [
        ('goto', None),
        ('click', 'buy'),
        ('wait_for', 'Created'),
    ]
    assert spec['steps'][-3:] == [
        {'name': 'js_errors', 'action': 'assert_no_console_errors'},
        {'name': 'http_errors', 'action': 'assert_no_http_errors'},
        {'name': 'page_errors', 'action': 'assert_no_page_errors'},
    ]
    assert spec['allow_mutations'] is True


def test_a_repeat_after_a_different_step_is_kept():
    text = '''
    page.goto("http://127.0.0.1:9/")
    page.get_by_label("Order ID").fill("one")
    page.get_by_test_id("buy").click()
    page.get_by_label("Order ID").fill("two")
    page.get_by_test_id("buy").click()
    '''
    spec, _, removed = scenario_from_recording(text, 'two-orders')
    assert removed == 0
    clicks = [step for step in spec['steps'] if step['action'] == 'click']
    assert len(clicks) == 2
    assert clicks[0]['name'] != clicks[1]['name']
