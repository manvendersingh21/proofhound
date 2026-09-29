"""Save one Playwright codegen demo as a local skill, then replay it.

The recorder is Playwright codegen. The replay is the existing universal UI
runner. Spans go to the existing OTLP receiver. Coordination with codex,
claude, opencode, and agy is the existing bilateral HACP mailbox: one session
per agent, not a new orchestrator.
"""
import argparse
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.platform.hacp_mailbox import Mailbox, envelope
from services.universal_ui.from_demo import scenario_from_recording
from services.universal_ui.runner import execute
from services.universal_ui.skill_graph import agent_steps, executable, neo4j_uri, scenario_hash, store_skill
from services.universal_ui.spec import validate

SAMPLE = '''
page.goto("http://127.0.0.1:8765/")
page.get_by_test_id("buy").click()
page.get_by_test_id("buy").click()
page.get_by_test_id("buy").click()
expect(page.get_by_text("Created")).to_be_visible()
'''

AGENTS = ('codex', 'claude', 'opencode', 'agy')
COORDINATOR = 'urn:hacp:agent:proofhound'
MEMORY = ROOT / '.local-runs' / 'memory.json'
# The template's backend reads its Gemini key from here; the value never enters Proofhound files.
APP_ENV_NOTES = {
    'ai-agent-template': 'Gemini key: GOOGLE_API_KEY in '
                         '/Users/manubaba/Documents/fsat-generated/demo_ai_app/backend/.env (gitignored)',
}


def memory_path():
    return Path(os.environ.get('QA_MEMORY_PATH', MEMORY))


scenario_digest = scenario_hash


def last_entry(entries, spec):
    for entry in reversed(entries):
        if entry.get('scenario_id') == spec['id'] and entry.get('project') == spec.get('project_id'):
            return entry
    return None


def load_memory():
    path = memory_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def last_pass(entries, spec):
    """The newest PASS for this exact scenario, or None when it changed or never passed."""
    entry = last_entry(entries, spec)
    if entry and entry.get('digest') == scenario_digest(spec) and entry.get('verdict') == 'PASS':
        return entry
    return None


def remember(spec, record, agent_calls=0):
    entry = {
        'project': spec.get('project_id'),
        'scenario_id': spec['id'],
        'digest': scenario_digest(spec),
        'skill_hash': scenario_digest(spec),
        'agent_calls': agent_calls,
        'origin': spec.get('origin'),
        'verdict': record.get('verdict'),
        'trace_id': record.get('trace_id'),
        'run_id': record.get('run_id'),
        'steps': [step['name'] for step in spec['steps']],
        'failed_steps': sorted({finding['step'] for finding in record.get('findings', []) if finding.get('step')}),
        'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    path = memory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = load_memory()
    entries.append(entry)
    scratch = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    scratch.write_text(json.dumps(entries[-200:], indent=2) + '\n')
    scratch.replace(path)
    return entry


def skill_dir(name):
    return Path.home() / '.cursor' / 'skills' / f'qa-{name}'


def write_skill(name, spec, removed, recording):
    directory = skill_dir(name)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'scenario.json').write_text(json.dumps(spec, indent=2) + '\n')
    store_skill(name, spec, directory / 'scenario.json', last_entry(load_memory(), spec))
    (directory / 'recording.py').write_text(recording if recording.endswith('\n') else recording + '\n')
    (directory / 'SKILL.md').write_text(f'''---
name: qa-{name}
description: Replay the recorded "{name}" browser flow as QA. Use when the user asks to retest this UI, run the saved skill, or check this flow again.
---

# Recorded UI skill: {name}

Playwright codegen recorded this flow. Consecutive duplicate steps removed: {removed}.
The scenario the existing runner executes is `scenario.json` in this directory.

## Replay

From the proofhound checkout, with the recorded app already running:

```bash
python -m scripts.demo_skill replay --skill {name}
```

`replay` starts the existing loopback OTLP receiver, then runs `services.universal_ui`. Every step is a `qa.ui.step` span. Do not write a second browser runner.

It prints the last entry of `.local-runs/memory.json` first, and skips the run when this exact scenario already passed. Add `--force` to replay anyway. Each step is a Neo4j `Step` under a `Skill` node (`HAS_STEP`, `NEXT`) at bolt://127.0.0.1:7687. When every step is deterministic no coding CLI runs; otherwise only codex gets a `BRIEF.md` for the flagged steps. `--no-agents` skips it.

## A new demo

```bash
python -m playwright codegen --target python -o recording.py http://127.0.0.1:PORT
python -m scripts.demo_skill from-codegen recording.py --name short-name
```

That writes `~/.cursor/skills/qa-short-name/` and drops consecutive identical clicks, fills, and navigations.
''')
    return directory


def _free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def start_receiver(data_dir):
    """Start the existing OTLP receiver. Returns (endpoint, process)."""
    port = _free_port()
    token = secrets.token_hex(16)
    env = {**os.environ, 'QA_DATA_DIR': str(data_dir), 'QA_OTLP_INGEST_TOKEN': token}
    process = subprocess.Popen(
        [sys.executable, '-m', 'uvicorn', 'services.telemetry.receiver:app',
         '--host', '127.0.0.1', '--port', str(port), '--log-level', 'error'],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            detail = process.stderr.read().decode()[:500] if process.stderr else ''
            raise RuntimeError('OTLP receiver exited: ' + detail)
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    else:
        process.terminate()
        raise RuntimeError('OTLP receiver did not listen')
    os.environ['QA_DATA_DIR'] = str(data_dir)
    os.environ['QA_OTLP_INGEST_TOKEN'] = token
    return f'http://127.0.0.1:{port}/v1/traces', process


def stop_receiver(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    if process.stderr:
        process.stderr.close()


def span_names(data_dir, trace_id):
    path = Path(data_dir) / 'telemetry.sqlite3'
    if not path.exists() or not trace_id:
        return []
    with sqlite3.connect(path) as db:
        return [row[0] for row in db.execute(
            'SELECT name FROM spans WHERE trace_id=? ORDER BY start_ns', (trace_id,))]


def replay_spec(spec, output, endpoint):
    validate(spec)
    return execute(spec, output, endpoint, local_files=True)


def post_hacp(output, result, agents=AGENTS, kind='qa.replay.assigned', extra=None):
    """One bilateral HACP session per coding CLI. Keys stay in this process."""
    root = Path(output) / 'hacp'
    root.mkdir(parents=True, exist_ok=True)
    keys = {COORDINATOR: secrets.token_bytes(32)}
    peers = {name: f'urn:hacp:agent:{name}' for name in agents}
    keys.update({urn: secrets.token_bytes(32) for urn in peers.values()})
    mailbox = Mailbox(root / 'mailbox.sqlite3', keys)
    sessions = []
    for name, peer in peers.items():
        session = 'qa-' + uuid4().hex
        mailbox.open(session, COORDINATOR, peer)
        body = {
            'task_id': 'ui-replay-' + name,
            'summary': 'Replay the saved UI skill. OTLP export stays on. Do not rewrite the runner.',
            'verdict': result['verdict'],
            'trace_id': result['trace_id'],
            'run_id': result['run_id'],
            'steps_removed': result.get('steps_removed', 0),
        }
        body.update(extra or {})
        message = envelope(session, COORDINATOR, peer, kind, body)
        mailbox.deliver(message, mailbox.sign(message, COORDINATOR))
        delivered = mailbox.receive(session, peer)
        if len(delivered) != 1:
            raise RuntimeError(f'{name} did not receive the HACP assignment')
        sessions.append({'agent': name, 'kind': kind, 'session_id': session, 'message_id': message['message_id']})
    (root / 'assignments.json').write_text(json.dumps(sessions, indent=2) + '\n')
    return sessions


def _which(name):
    for directory in os.environ.get('PATH', '').split(os.pathsep):
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _tail(value):
    if isinstance(value, bytes):
        value = value.decode(errors='replace')
    return (value or '')[-400:]


def _agent_argv(name, binary, directory, prompt):
    # codex --yolo is a hidden alias for --dangerously-bypass-approvals-and-sandbox.
    # opencode run has no --dangerously-skip-permissions; --auto is its full-approval flag.
    if name == 'codex':
        return [binary, 'exec', '--yolo', '--skip-git-repo-check', '-C', str(directory), prompt]
    if name == 'claude':
        return [binary, '--dangerously-skip-permissions', '--add-dir', str(directory),
                '--add-dir', str(ROOT), '-p', prompt]
    if name == 'opencode':
        return [binary, 'run', '--dir', str(directory), '--auto', prompt]
    return [binary, '--dangerously-skip-permissions', '-p', prompt]


def _run_argv(argv, directory, timeout, use_pty):
    if not use_pty:
        return subprocess.run(
            argv, cwd=directory, text=True, capture_output=True,
            stdin=subprocess.DEVNULL, timeout=timeout)
    import pty
    master, slave = pty.openpty()
    try:
        return subprocess.run(
            argv, cwd=directory, text=True, stdin=slave,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    finally:
        os.close(master)
        os.close(slave)


def _run_agent(name, directory, prompt, timeout):
    binary = _which(name)
    if binary is None:
        return {'agent': name, 'status': 'missing'}
    try:
        completed = _run_argv(_agent_argv(name, binary, directory, prompt), directory, timeout, name == 'codex')
    except subprocess.TimeoutExpired as exc:
        return {'agent': name, 'status': 'timeout', 'stderr_tail': _tail(exc.stderr)}
    except OSError as exc:
        return {'agent': name, 'status': 'error', 'detail': str(exc)}
    report = directory / 'REPORT.json'
    body = None
    if report.exists():
        try:
            body = json.loads(report.read_text())
        except json.JSONDecodeError:
            body = {'unparsed': True}
    return {'agent': name, 'exit_code': completed.returncode, 'report': body,
            'stderr_tail': _tail(completed.stderr)}


def _brief(name, directory, result, skill, steps=None):
    if not skill:
        return ('Write REPORT.json in this directory containing exactly this JSON and no other files:\n'
                + json.dumps({'agent': name, 'verdict': result['verdict'],
                              'trace_id': result['trace_id'], 'ack': True}) + '\n')
    python = ROOT / '.venv' / 'bin' / 'python'
    scenario = skill_dir(skill) / 'scenario.json'
    run_dir = directory / 'run'
    focus = '\n'.join(f'- `{step["name"]}` ({step["action"]}): {step["reason"]}' for step in steps or [])
    return f'''# QA brief for {name}

You are helping QA one saved UI skill. Do not edit any source file in any repository.

- Skill scenario: {scenario}
- Proofhound checkout: {ROOT}
- Coordinator run: verdict {result['verdict']}, trace_id {result['trace_id']}

The skill graph says only these steps need judgment. Every other step already replayed deterministically:
{focus or '- (none listed)'}

Steps:
1. Read {scenario} and decide whether the steps listed above are correct user actions for the app at its `origin`.
2. Run this exact command once (it replays the skill in a real browser and exports OTLP spans):
   cd {ROOT} && {python} -m scripts.demo_skill replay --skill {skill} --force --no-agents --output {run_dir}
3. Its last output is JSON with `verdict` and `trace_id`. If you could not run it, use the coordinator values above and set "ran": false.
4. Write {directory / 'REPORT.json'} containing one JSON object:
   {{"agent": "{name}", "verdict": "<PASS|FAIL|INCONCLUSIVE|INFRA_ERROR>", "trace_id": "<32 hex>", "ack": true, "ran": <true|false>, "notes": "<one sentence on anything wrong with the flow>"}}
'''


def dispatch_live(output, result, timeout, agents=AGENTS, skill=None, steps=None):
    """Launch each CLI on a file-edge BRIEF.md with absolute paths; each writes REPORT.json."""
    briefs = Path(output) / 'crew'
    prompt = (
        'Read BRIEF.md in this directory and follow it exactly. '
        'Write REPORT.json in this directory.'
    )
    jobs = []
    for name in agents:
        directory = (briefs / name).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'REPORT.json').unlink(missing_ok=True)
        (directory / 'BRIEF.md').write_text(_brief(name, directory, result, skill, steps))
        jobs.append((name, directory))
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        reports = list(pool.map(lambda job: _run_agent(job[0], job[1], prompt, timeout), jobs))
    (briefs / 'reports.json').write_text(json.dumps(reports, indent=2) + '\n')
    return reports


def run_demo(output, live, timeout):
    from reference_apps.ui_fixture import ui_fixture
    spec, _, removed = scenario_from_recording(SAMPLE, 'shop')
    directory = write_skill('shop', spec, removed, SAMPLE)
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    log = root / 'server.log'
    log.touch()
    endpoint, receiver = start_receiver(root)
    try:
        with ui_fixture(log, broken=False) as (origin, _oracle):
            live_spec = json.loads(json.dumps(spec))
            live_spec['origin'] = origin
            record = replay_spec(live_spec, root, endpoint)
    finally:
        stop_receiver(receiver)
    names = span_names(root, record.get('trace_id'))
    result = {
        'skill': str(directory),
        'steps_in_recording': 5,
        'steps_removed': removed,
        'steps_replayed': record.get('steps_executed'),
        'verdict': record.get('verdict'),
        'run_id': record.get('run_id'),
        'trace_id': record.get('trace_id'),
        'otlp_endpoint': endpoint,
        'span_names': names,
        'error': record.get('error'),
    }
    assignments = post_hacp(root, result)
    result['hacp_sessions'] = assignments
    if live:
        result['agents'] = dispatch_live(root, result, timeout)
    (root / 'summary.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def replay_command(args):
    spec = json.loads((skill_dir(args.skill) / 'scenario.json').read_text())
    if args.origin:
        spec['origin'] = args.origin
    note = APP_ENV_NOTES.get(spec.get('project_id'))
    if note:
        print(note, file=sys.stderr)
    entries = load_memory()
    print('last memory entry: ' + (json.dumps(entries[-1]) if entries else 'none'), file=sys.stderr)
    previous = last_pass(entries, spec)
    if previous and not args.force:
        print(json.dumps({'skipped': True, 'reason': 'scenario unchanged since last PASS',
                          'verdict': previous['verdict'], 'trace_id': previous['trace_id'],
                          'agent_calls': 0, 'agent_calls_skipped': len(AGENTS)}, indent=2))
        return 0
    labels = store_skill(args.skill, spec, skill_dir(args.skill) / 'scenario.json',
                         last_entry(entries, spec))
    needed = agent_steps(args.skill, scenario_digest(spec))
    print(f'skill graph: {len(labels) - len(needed)} steps no-agent, {len(needed)} agent_required '
          f'({neo4j_uri()})', file=sys.stderr)
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    endpoint, receiver = start_receiver(root)
    try:
        record = replay_spec(executable(spec), root, endpoint)
    finally:
        stop_receiver(receiver)
    record['span_names'] = span_names(root, record.get('trace_id'))
    agents = ['codex'] if needed and not args.no_agents else []
    entry = remember(spec, record, agent_calls=len(agents))
    summary = {
        'skill': str(skill_dir(args.skill)),
        'origin': spec['origin'],
        'verdict': record['verdict'],
        'trace_id': record['trace_id'],
        'run_id': record['run_id'],
        'span_names': sorted(set(record['span_names'])),
        'span_count': len(record['span_names']),
        'telemetry_db': str(root / 'telemetry.sqlite3'),
        'failed_steps': entry['failed_steps'],
        'findings': [{'step': f.get('step'), 'category': f.get('category'), 'reason': f.get('reason')}
                     for f in record.get('findings', [])],
        'memory': str(memory_path()),
        'skill_graph': neo4j_uri(),
        'steps_no_agent': len(labels) - len(needed),
        'steps_agent_required': [step['name'] for step in needed],
        'agent_calls': len(agents),
        'agent_calls_skipped': len(AGENTS) - len(agents),
        'error': record.get('error'),
    }
    if agents:
        summary['hacp_sessions'] = post_hacp(root, record, agents, extra={
            'steps': [step['name'] for step in needed]})
        summary['agents'] = dispatch_live(root, record, args.agent_timeout, agents, args.skill, needed)
    else:
        reason = 'deterministic replay' if not needed else 'agents disabled with --no-agents'
        summary['hacp_sessions'] = post_hacp(root, record, AGENTS, 'qa.skipped_agents', {
            'skipped_agents': list(AGENTS), 'reason': reason})
    print(f'agent calls: {len(agents)}, skipped: {len(AGENTS) - len(agents)}', file=sys.stderr)
    (root / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))
    return 0 if record['verdict'] == 'PASS' else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description='Save a UI demo as a skill and replay it with OTLP')
    sub = parser.add_subparsers(dest='command', required=True)
    recorded = sub.add_parser('from-codegen', help='Convert a Playwright codegen file into a skill')
    recorded.add_argument('recording')
    recorded.add_argument('--name', required=True)
    recorded.add_argument('--replay', action='store_true')
    recorded.add_argument('--output', default='.local-runs/demo-skill')
    again = sub.add_parser('replay', help='Replay a skill already saved on this computer')
    again.add_argument('--skill', required=True)
    again.add_argument('--origin', help='Override the recorded origin when the app is on another port')
    again.add_argument('--output', default='.local-runs/demo-skill')
    again.add_argument('--force', action='store_true', help='Replay even when memory says this exact scenario passed')
    again.add_argument('--no-agents', action='store_true', help='Skip launching the coding CLIs')
    again.add_argument('--agent-timeout', type=int, default=240)
    demo = sub.add_parser('demo', help='Record the bundled shop demo, save the skill, replay it')
    demo.add_argument('--output', default='.local-runs/demo-skill')
    demo.add_argument('--live', action='store_true', help='Also ask codex, claude, opencode, and agy to acknowledge')
    demo.add_argument('--agent-timeout', type=int, default=90)
    args = parser.parse_args(argv)
    if args.command == 'from-codegen':
        text = Path(args.recording).read_text()
        spec, _, removed = scenario_from_recording(text, args.name)
        directory = write_skill(args.name, spec, removed, text)
        print(json.dumps({'skill': str(directory), 'steps_removed': removed, 'origin': spec['origin']}, indent=2))
        if not args.replay:
            return 0
        args.skill = args.name
        args.origin = None
        args.force = False
        args.no_agents = True
        args.agent_timeout = 0
        args.command = 'replay'
    if args.command == 'replay':
        return replay_command(args)
    result = run_demo(args.output, args.live, args.agent_timeout)
    print(json.dumps(result, indent=2))
    ok = result['verdict'] == 'PASS' and 'qa.ui.run' in result['span_names'] and result['steps_removed'] == 2
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
