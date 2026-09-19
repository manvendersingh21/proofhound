"""The whole pipeline driven through the web app's UI with a real browser (no direct API calls)."""
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest


def _free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@pytest.fixture
def server():
    repo = Path(__file__).resolve().parents[1]
    port = _free_port()
    with tempfile.TemporaryDirectory() as temp:
        env = {k: v for k, v in os.environ.items() if not k.startswith(('QA_', 'PYTEST_'))}
        proc = subprocess.Popen([sys.executable, '-m', 'services.platform.serve', '--port', str(port), '--data', temp],
                                cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        base = f'http://127.0.0.1:{port}'
        try:
            for _ in range(100):
                try:
                    if httpx.get(base + '/health', timeout=1).status_code == 200: break
                except httpx.HTTPError:
                    time.sleep(0.1)
            yield base
        finally:
            proc.terminate(); proc.wait(timeout=10)


def _wait_text(page, text, timeout=60000):
    page.get_by_text(text, exact=False).first.wait_for(timeout=timeout)


def test_every_step_through_the_ui(server):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 1360, 'height': 900})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('console', lambda m: m.type == 'error' and errors.append(m.text))

        page.goto(server + '/')
        _wait_text(page, 'Get started')

        # Start the sample app (v2 with the defect), then onboard it from the prefilled form.
        page.get_by_role('link', name='Sample app', exact=True).click()
        page.get_by_role('button', name='Start v2 (with defect)').click()
        page.get_by_role('link', name='Onboard this app →').click()
        page.get_by_role('button', name='Discover').click()
        _wait_text(page, 'Discovered 4 pages')
        _wait_text(page, 'Not followed (looks destructive): /account/delete')

        # Approve the last-copy reservation workflow with an independent state check.
        page.get_by_role('link', name='Review & approve workflows →').click()
        row = page.locator('tr', has_text='form-catalog-reserve-form-b2')
        row.get_by_role('button', name='Approve…').click()
        page.get_by_label('JSON pointer').fill('/reservations')
        page.get_by_label('Expected (JSON)').fill('1')
        page.get_by_label('Element id').fill('status')
        page.get_by_label('Expected text').fill('Reserved')
        page.get_by_label('I am authorized').check()
        page.get_by_role('button', name='Approve workflow').click()
        _wait_text(page, 'changes data · approved')

        # Run it; the runs page follows the job to the run detail.
        page.locator('tr', has_text='form-catalog-reserve-form-b2').get_by_role('button', name='Run').click()
        _wait_text(page, 'What went wrong')
        _wait_text(page, 'The UI said it worked, the real state says it did not.')
        _wait_text(page, 'no database-write span followed')
        assert page.locator('svg[aria-label="Evidence timeline by step"] rect').count() > 0
        _wait_text(page, 'Reproducible bug report')

        # Deploy the fix (v3), onboard it and run the selected regressions.
        page.get_by_role('link', name='Sample app', exact=True).click()
        page.get_by_role('button', name='Start v3 (fixed, /loans changed)').click()
        _wait_text(page, 'v3 · fixed')
        page.get_by_role('link', name='Onboard this app →').click()
        page.get_by_label('Version').fill('v3')
        page.get_by_role('button', name='Discover').click()
        _wait_text(page, 'What changed since v2')
        page.get_by_role('link', name='Review & approve workflows →').click()
        _wait_text(page, 'failed in a previous run')
        _wait_text(page, 'page changed since v2')
        # The v3 app has a new origin; point the saved approval at the new oracle.
        page.locator('tr', has_text='form-catalog-reserve-form-b2').get_by_role('button', name='Edit approval').click()
        oracle = httpx.get(server + '/api/sample-app').json()['oracle']
        page.get_by_label('Independent state service').fill(oracle)
        page.get_by_label('Server log file').fill(httpx.get(server + '/api/sample-app').json()['log_file'])
        page.get_by_label('I am authorized').check()
        page.get_by_role('button', name='Approve workflow').click()
        _wait_text(page, 'changes data · approved')
        page.get_by_role('button', name='selected regression').click()
        page.wait_for_url('**/#/runs')
        deadline = time.time() + 90
        while time.time() < deadline:
            jobs = httpx.get(server + '/api/jobs').json()
            if jobs and all(j['status'] in ('done', 'error') for j in jobs): break
            time.sleep(0.5)
        latest = {}
        for job in reversed(jobs):
            latest[job['scenario_id']] = job
        assert latest['form-catalog-reserve-form-b2']['verdict'] == 'PASS'
        assert latest['nav-loans']['verdict'] == 'PASS'

        # Overview and logs show the history graphically.
        page.get_by_role('link', name='Overview', exact=True).click()
        _wait_text(page, 'Verdicts per day')
        assert page.locator('svg[aria-label="Run verdicts per day"] rect').count() >= 2
        page.get_by_role('link', name='4 · Logs').click()
        _wait_text(page, 'Errors by source')
        assert not errors, errors
        browser.close()
        httpx.delete(server + '/api/sample-app')
