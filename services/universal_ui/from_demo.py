"""Turn a Playwright codegen recording into a Proofhound scenario.

Playwright's own `codegen` command is the recorder. This module only keeps the
calls the existing UI runner already knows how to replay, and drops consecutive
identical steps so a shaky demo does not become a double-submit.
"""
import json
import re
from urllib.parse import urlsplit

_STRING = r"""(?P<q>['"])(?P<value>(?:\\.|(?!(?P=q)).)*)(?P=q)"""


def _unescape(value):
    return bytes(value, 'utf-8').decode('unicode_escape')


def _pick(match, group='value'):
    return _unescape(match.group(group))


def _action_call(line):
    """Return (method, argument or None) for a trailing Playwright action."""
    found = re.search(
        r'\.(?P<method>click|fill|press|check|uncheck|hover|select_option)\(\s*(?:' + _STRING + r')?\s*\)\s*;?\s*$',
        line)
    if not found:
        return None
    method = 'select' if found.group('method') == 'select_option' else found.group('method')
    argument = _pick(found) if found.group('value') is not None else None
    return method, argument


def _locator(by, value):
    return {'by': by, 'value': value}


def _step(action, **fields):
    record = {'action': action}
    record.update({key: value for key, value in fields.items() if value is not None})
    return record


def parse_line(line):
    """Parse one codegen line. Returns a step dict, or None when the line is not a UI action."""
    text = line.strip()
    if not text or text.startswith(('#', '//', 'import ', 'from ', 'def ', 'async ')):
        return None
    goto = re.search(r'\.goto\(\s*' + _STRING, text)
    if goto and 'page' in text:
        url = _pick(goto)
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.netloc:
            raise ValueError('codegen goto must be an absolute http(s) URL')
        path = parts.path or '/'
        if parts.query or parts.fragment or not path.startswith('/'):
            raise ValueError('codegen goto cannot include a query string or fragment')
        return _step('goto', path=path, _origin=f'{parts.scheme}://{parts.netloc}')
    title = re.search(r'to_have_title\(\s*' + _STRING, text)
    if title and 'expect' in text:
        return _step('assert_title', expected=_pick(title))
    # The scenario locator stores one string. Role+name recordings keep the visible name
    # and wait for it, which is the call the existing runner can replay.
    waiting = 'to_be_visible' in text or 'to_have_text' in text
    action = _action_call(text)
    if action is None and not waiting:
        return None
    method, argument = action if action else ('wait_for', None)
    role = re.search(r"""get_by_role\(\s*['\"][^'\"]+['\"]\s*,\s*name\s*=\s*""" + _STRING, text)
    role = role or re.search(r"""getByRole\(\s*['\"][^'\"]+['\"]\s*,\s*\{\s*name:\s*""" + _STRING, text)
    if role:
        return _step(method, locator=_locator('text', _pick(role)), value=argument)
    for by, pattern in (
        ('test_id', r'get(?:_by_test_id|ByTestId)\(\s*' + _STRING),
        ('label', r'get(?:_by_label|ByLabel)\(\s*' + _STRING),
        ('placeholder', r'get(?:_by_placeholder|ByPlaceholder)\(\s*' + _STRING),
        ('text', r'get(?:_by_text|ByText)\(\s*' + _STRING),
    ):
        found = re.search(pattern, text)
        if found:
            return _step(method, locator=_locator(by, _pick(found)), value=argument)
    css = re.search(r"""\.locator\(\s*""" + _STRING, text)
    if css:
        return _step(method, locator=_locator('css', _pick(css)), value=argument)
    return None


def _key(step):
    return (step['action'], json.dumps(step.get('locator'), sort_keys=True),
            step.get('path'), step.get('value'), json.dumps(step.get('expected'), sort_keys=True))


def dedupe(steps):
    """Drop consecutive identical actions. A later different step keeps the next repeat."""
    kept = []
    removed = []
    for step in steps:
        if kept and _key(kept[-1]) == _key(step):
            removed.append(step)
            continue
        kept.append(step)
    return kept, removed


def _slug(step, index):
    raw = step['action']
    if 'locator' in step:
        raw += '_' + step['locator']['value']
    elif 'path' in step:
        raw += '_' + step['path']
    slug = re.sub(r'[^A-Za-z0-9]+', '_', raw).strip('_')[:60] or f'step_{index}'
    if slug[0].isdigit():
        slug = 's_' + slug
    return slug


def assign_names(steps):
    seen = {}
    named = []
    for index, step in enumerate(steps, start=1):
        base = _slug(step, index)
        seen[base] = seen.get(base, 0) + 1
        name = base if seen[base] == 1 else f'{base}_{seen[base]}'
        named.append({'name': name[:80], **{k: v for k, v in step.items() if not k.startswith('_')}})
    return named


def with_qa_checks(steps):
    present = {step['action'] for step in steps}
    extra = []
    for action, name in (
        ('assert_no_console_errors', 'js_errors'),
        ('assert_no_http_errors', 'http_errors'),
        ('assert_no_page_errors', 'page_errors'),
    ):
        if action not in present:
            extra.append({'name': name, 'action': action})
    return steps + extra


def scenario_from_recording(text, name):
    """Parse, dedupe, and return (scenario, origin, removed_count). Origin comes from the first goto."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', name):
        raise ValueError('skill name must be 1-80 letters, numbers, underscores, or hyphens')
    parsed = []
    origin = None
    for line in text.splitlines():
        needs_agent = line.rstrip().endswith('# needs_agent')
        if needs_agent:
            line = line.rstrip()[:-len('# needs_agent')]
        step = parse_line(line)
        if step is None:
            continue
        if needs_agent:
            step['needs_agent'] = True
        timeout = re.search(r'timeout\s*[=:]\s*(\d+)', line)
        if timeout and step['action'] != 'goto':
            step['timeout_ms'] = max(100, min(int(timeout.group(1)), 30000))
        if step['action'] == 'goto':
            origin = step.pop('_origin')
        parsed.append(step)
    if not parsed or origin is None:
        raise ValueError('recording needs at least one page.goto to an absolute URL')
    kept, removed = dedupe(parsed)
    steps = with_qa_checks(assign_names(kept))
    mutating = any(step['action'] in {'click', 'fill', 'press', 'check', 'uncheck', 'select'} for step in steps)
    spec = {
        'id': name,
        'project_id': name,
        'version': 'recorded-v1',
        'origin': origin,
        'allow_mutations': mutating,
        'propagate_trace': True,
        'steps': steps,
    }
    return spec, origin, len(removed)
