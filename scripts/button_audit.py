"""Click each visible button on the template app and record the session.

Destructive labels are skipped. A button fails only when the click throws or
the page raises an exception. Pre-existing HTTP 401s are not failures.

Each run keeps the browser console and the backend container log together and
writes a Proofhound dashboard. The report is not named Allure.
"""
import html
import json
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = 'http://localhost:3000'
PAGES = [
    '/dashboard', '/chat', '/settings', '/settings/profile', '/settings/account',
    '/settings/appearance', '/settings/notifications', '/settings/slash-commands',
    '/admin', '/admin/users', '/admin/conversations', '/admin/system', '/pricing',
]
SKIP = ('log out', 'logout', 'sign out', 'delete', 'remove', 'destroy', 'archive',
        'impersonate', 'revoke', 'wipe', 'disable account', 'drop ')


def _allure(results, name, status, suite, message=''):
    results.mkdir(parents=True, exist_ok=True)
    now = int(time.time() * 1000)
    uid = str(uuid.uuid4())
    body = {
        'uuid': uid,
        'historyId': suite + ':' + name,
        'name': name,
        'fullName': suite + '.' + name,
        'status': status,
        'stage': 'finished',
        'start': now,
        'stop': now + 1,
        'labels': [
            {'name': 'parentSuite', 'value': 'Proofhound'},
            {'name': 'suite', 'value': suite},
            {'name': 'feature', 'value': suite},
        ],
    }
    if message:
        body['statusDetails'] = {'message': message}
    (results / (uid + '-result.json')).write_text(json.dumps(body))


def _buttons(page):
    return page.evaluate('''() => {
      const visible = el => {
        const r = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return r.width > 1 && r.height > 1 && style.visibility !== 'hidden' && style.display !== 'none';
      };
      return [...document.querySelectorAll('button,[role="button"]')].filter(visible).map((el, index) => {
        el.setAttribute('data-qa-btn', String(index));
        const name = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || el.id || 'button').trim();
        return {index, name: name.replace(/\\s+/g, ' ').slice(0, 80)};
      });
    }''')


def _login(page):
    page.goto(ORIGIN + '/login', wait_until='domcontentloaded')
    try:
        page.get_by_text('Reject optional', exact=True).first.click(timeout=2500)
    except Exception:
        pass
    page.get_by_placeholder('name@company.com').fill('admin@example.com')
    page.locator('#password').fill('admin123')
    page.get_by_text('Login', exact=True).first.click()
    page.get_by_text('New chat', exact=True).first.wait_for(timeout=15000)


def _redact(line):
    lowered = line.lower()
    if any(word in lowered for word in ('password', 'authorization', 'api_key', 'api-key', 'bearer ', 'cookie')):
        return '[redacted]'
    return line[:400]


def _backend_log(since):
    completed = subprocess.run(
        ['docker', 'logs', 'demo_ai_app_backend', '--since', since],
        capture_output=True, text=True)
    text = (completed.stdout or '') + (completed.stderr or '')
    if completed.returncode != 0 and not text.strip():
        text = 'backend log unavailable: ' + (completed.stderr or 'docker logs failed')
    return [_redact(line) for line in text.splitlines() if line.strip()][-400:]


def _write_dashboard(output):
    """One Proofhound page: button results plus frontend and backend logs."""
    phases = []
    for path in sorted(output.glob('*.json')):
        if path.name == 'logs.json':
            continue
        try:
            phases.append(json.loads(path.read_text()))
        except json.JSONDecodeError:
            continue
    logs = {}
    log_path = output / 'logs.json'
    if log_path.exists():
        logs = json.loads(log_path.read_text())
    def rows(items):
        body = []
        for item in items:
            if 'status' not in item:
                continue
            body.append('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
                html.escape(item.get('page', '')), html.escape(item.get('button', '')),
                html.escape(item.get('status', '')),
                html.escape((item.get('error') or [''])[0][:180])))
        return ''.join(body)
    sections = []
    for phase in phases:
        sections.append('<h2>%s</h2><p>%s passed, %s failed, %s skipped</p><table><thead><tr>'
                        '<th>Page</th><th>Button</th><th>Status</th><th>Error</th></tr></thead><tbody>%s</tbody></table>' % (
                            html.escape(phase.get('phase', '')), phase.get('passed', 0),
                            phase.get('failed', 0), phase.get('skipped', 0), rows(phase.get('rows', []))))
    def pane(title, lines):
        text = '\n'.join(lines) if lines else 'No lines captured for this run.'
        return '<section><h2>%s</h2><pre>%s</pre></section>' % (html.escape(title), html.escape(text))
    page = '''<!doctype html><html lang="en"><head><meta charset="utf-8"/>
    <title>Proofhound</title><style>
    body{font:15px system-ui,sans-serif;margin:0;color:#17202a;background:#f4f7fb}
    header{padding:22px 28px;background:#13293d;color:#fff} h1{margin:0;font-size:28px}
    main{padding:8px 28px 40px} table{border-collapse:collapse;width:100%%;background:#fff}
    th,td{text-align:left;padding:8px;border-bottom:1px solid #d7e0eb;vertical-align:top;font-size:13px}
    th{background:#e7eef6} .logs{display:grid;grid-template-columns:1fr 1fr;gap:16px}
    pre{white-space:pre-wrap;background:#111;color:#d6f5d6;padding:12px;max-height:480px;overflow:auto;font-size:12px}
    @media(max-width:900px){.logs{grid-template-columns:1fr}}
    </style></head><body><header><h1>Proofhound</h1>
    <p>Frontend and backend logs from the same test run.</p></header><main>
    <div class="logs">%s%s</div>
    %s</main></body></html>''' % (
        pane('Frontend log', logs.get('frontend', [])),
        pane('Backend log', logs.get('backend', [])),
        ''.join(sections))
    target = output / 'proofhound-dashboard' / 'index.html'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page)
    return target


def audit(phase, output):
    output = Path(output)
    video_dir = output / 'video' / phase
    video_dir.mkdir(parents=True, exist_ok=True)
    results = output / 'allure-results'
    rows = []
    errors = []
    frontend = []
    started = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    def on_error(error):
        errors.append(str(error))
        frontend.append(_redact('pageerror: ' + str(error)))

    def on_console(message):
        if message.type in ('error', 'warning'):
            frontend.append(_redact(message.type + ': ' + message.text))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, slow_mo=40)
        context = browser.new_context(
            viewport={'width': 1280, 'height': 800},
            record_video_dir=str(video_dir),
            record_video_size={'width': 1280, 'height': 800},
        )
        page = context.new_page()
        page.on('pageerror', on_error)
        page.on('console', on_console)
        page.on('response', lambda response: frontend.append(
            _redact('HTTP %s %s' % (response.status, response.url.split('?')[0])))
            if response.status >= 400 else None)
        page.set_default_timeout(8000)
        _login(page)
        for path in PAGES:
            page.goto(ORIGIN + path, wait_until='domcontentloaded')
            page.wait_for_timeout(400)
            found = _buttons(page)
            for button in found:
                label = button['name'] or 'button'
                title = path + ' · ' + label
                if any(word in label.lower() for word in SKIP):
                    rows.append({'page': path, 'button': label, 'status': 'skipped'})
                    _allure(results, title, 'skipped', phase, 'destructive label, not clicked')
                    continue
                before = len(errors)
                try:
                    page.locator('[data-qa-btn="%s"]' % button['index']).click(force=True, timeout=2000)
                    page.wait_for_timeout(150)
                    page.keyboard.press('Escape')
                except Exception as exc:
                    text = type(exc).__name__ + ': ' + str(exc)[:180]
                    if 'Timeout' in text:
                        rows.append({'page': path, 'button': label, 'status': 'skipped', 'error': [text]})
                        _allure(results, title, 'skipped', phase, 'not clickable in 2s')
                        continue
                    errors.append(text)
                failed = errors[before:]
                status = 'failed' if failed else 'passed'
                rows.append({'page': path, 'button': label, 'status': status, 'error': failed[:1]})
                _allure(results, title, status, phase, failed[0] if failed else '')
                if page.url.split('?')[0].rstrip('/') != (ORIGIN + path).rstrip('/'):
                    page.goto(ORIGIN + path, wait_until='domcontentloaded')
                    page.wait_for_timeout(200)
                    _buttons(page)
        video = page.video
        context.close()
        browser.close()
        if video:
            target = output / (phase.replace(' ', '-') + '.webm')
            Path(video.path()).replace(target)
            rows.append({'recording': str(target)})
    summary = {
        'phase': phase,
        'passed': sum(1 for row in rows if row.get('status') == 'passed'),
        'failed': sum(1 for row in rows if row.get('status') == 'failed'),
        'skipped': sum(1 for row in rows if row.get('status') == 'skipped'),
        'rows': rows,
    }
    (output / (phase.replace(' ', '-') + '.json')).write_text(json.dumps(summary, indent=2) + '\n')
    (output / 'logs.json').write_text(json.dumps({
        'phase': phase,
        'started': started,
        'frontend': frontend[-400:],
        'backend': _backend_log(started),
    }, indent=2) + '\n')
    dashboard = _write_dashboard(output)
    print(json.dumps({k: summary[k] for k in ('phase', 'passed', 'failed', 'skipped')}))
    print('dashboard', dashboard)
    return summary


if __name__ == '__main__':
    import sys
    audit(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else str(ROOT / '.local-runs' / 'button-audit'))
