"""JSON API behind the ProofHound web app: every pipeline step as an HTTP call.

onboard -> review workflows -> approve -> run (background) -> inspect evidence,
correlation and bug report -> onboard the next version -> run selected regressions.

Local, single-tenant and loopback-only like the rest of the control plane. Runs are
serialized through one worker thread (the browser engine and the process-wide OTLP
exporter are not meant to be shared by concurrent runs).
"""
import json
import os
import re
import threading
import time
import traceback
from collections import Counter, OrderedDict
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix='/api')
NAME = re.compile(r'^[A-Za-z0-9_-]{1,80}$')
_lock = threading.Lock()
_jobs = OrderedDict()
_sample = {}


def data_dir():
    return Path(os.getenv('QA_DATA_DIR', '.local-runs')).resolve()


def local_files():
    """Log tailing reads local files; only when the owner launched the app with it enabled."""
    return os.getenv('QA_API_LOCAL_FILES') == '1'


def _store():
    from services.engine.store import Store
    return Store(data_dir())


def _safe(name, what):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise HTTPException(422, f'invalid {what}')
    return name


def _observed(record):
    try:
        return json.loads(_store().evidence_bytes(record['evidence_sha256']))
    except (ValueError, OSError):
        return None


# ---------------------------------------------------------------- overview

@router.get('/overview')
def overview(limit: int = 200):
    if not 1 <= limit <= 500:
        raise HTTPException(422, 'limit must be 1..500')
    from services.universal_ui.issues import IssueIndex
    runs = _all_runs(limit)
    verdicts = Counter(r['verdict'] for r in runs)
    by_day = {}
    for run in runs:
        day = run['created_at'][:10]
        by_day.setdefault(day, Counter())[run['verdict']] += 1
    scenarios = {}
    for run in sorted(runs, key=lambda r: r['created_at']):
        key = (run['project_id'], run['scenario_id'])
        scenarios.setdefault(key, []).append({'verdict': run['verdict'], 'run_id': run['run_id'],
                                              'created_at': run['created_at']})
    sources = Counter()
    for run in runs[:50]:
        observed = _observed(run)
        for event in (observed or {}).get('events', []):
            if event.get('level') == 'error':
                sources[event['source']] += 1
    decided = verdicts['PASS'] + verdicts['FAIL']
    return {
        'kpis': {'runs': len(runs), 'pass': verdicts['PASS'], 'fail': verdicts['FAIL'],
                 'inconclusive': verdicts['INCONCLUSIVE'], 'infra_error': verdicts['INFRA_ERROR'],
                 'pass_rate': round(verdicts['PASS'] / decided, 3) if decided else None,
                 'latest': runs[0] if runs else None},
        'by_day': [{'day': day, **{v: c[v] for v in ('PASS', 'FAIL', 'INCONCLUSIVE', 'INFRA_ERROR')}}
                   for day, c in sorted(by_day.items())],
        'scenarios': [{'project_id': p, 'scenario_id': s, 'history': h[-20:], 'latest': h[-1]['verdict']}
                      for (p, s), h in sorted(scenarios.items(), key=lambda kv: kv[1][-1]['created_at'], reverse=True)],
        'issues': IssueIndex(data_dir()).list(limit=20),
        'error_events_by_source': dict(sources.most_common()),
        'error_events_scope': 'last 50 runs',
    }


def _all_runs(limit):
    import sqlite3
    store = _store()
    with sqlite3.connect(store.path) as db:
        rows = db.execute('SELECT details FROM runs ORDER BY created_at DESC LIMIT ?', (limit,)).fetchall()
    return [json.loads(row[0]) for row in rows]


# ---------------------------------------------------------------- runs, logs

@router.get('/runs')
def runs(project_id: str | None = None, limit: int = 50):
    try:
        return _store().list(project_id, limit)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get('/runs/{run_id}')
def run_detail(run_id: str):
    from services.telemetry.correlate import correlate
    from services.universal_ui import bugreport
    record = _store().get(run_id)
    if record is None:
        raise HTTPException(404, 'run not found')
    observed = _observed(record)
    result = {'record': record, 'observed': observed, 'correlation': None, 'scenario': None, 'bug_report': None}
    scenario_path = data_dir() / 'api-scenarios' / (run_id + '.json')
    if scenario_path.is_file():
        result['scenario'] = json.loads(scenario_path.read_text())
    if record.get('kind') == 'universal-ui' and observed is not None:
        try:
            result['correlation'] = correlate(record, data_dir())
        except Exception as exc:  # correlation is additive evidence; never hide the run
            result['correlation_error'] = type(exc).__name__
        if result['scenario']:
            result['bug_report'] = bugreport.render(record, result['scenario'], result['correlation'])
    return result


@router.get('/logs')
def logs(source: str | None = None, level: str | None = None, run_id: str | None = None,
         project_id: str | None = None, runs_scanned: int = 30, limit: int = 500):
    if not 1 <= runs_scanned <= 100 or not 1 <= limit <= 2000:
        raise HTTPException(422, 'runs_scanned 1..100, limit 1..2000')
    records = [_store().get(run_id)] if run_id else _store().list(project_id, runs_scanned)
    events = []
    for record in records:
        if record is None:
            continue
        observed = _observed(record) or {}
        steps = observed.get('steps', [])
        for event in observed.get('events', []):
            if source and event['source'] != source: continue
            if level and event['level'] != level: continue
            step = next((s['step'] for s in steps if s['event_seq_start'] < event['seq'] <= s['event_seq_end']), None)
            events.append({**event, 'run_id': record['run_id'], 'scenario_id': record['scenario_id'],
                           'project_id': record['project_id'], 'verdict': record['verdict'], 'step': step})
    events.sort(key=lambda e: e['at'], reverse=True)
    return {'events': events[:limit], 'total': len(events), 'runs_scanned': len(records),
            'note': 'log message text is only retained for runs with log_messages=true'}


@router.get('/traces/{trace_id}')
def trace(trace_id: str):
    from services.telemetry.receiver import read_trace
    try:
        return {'trace_id': trace_id, 'spans': read_trace(trace_id)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


# ---------------------------------------------------------------- onboarding

class OnboardRequest(BaseModel):
    origin: str
    project_id: str = 'local'
    version: str = 'v1'
    auth_env: str | None = None
    max_pages: int = Field(10, ge=1, le=30)
    fixture_relay: bool = False


@router.post('/onboard')
def onboard_app(payload: OnboardRequest):
    from services.universal_ui.onboard import onboard
    _safe(payload.project_id, 'project_id')
    _safe(payload.version, 'version')
    spec = {'id': 'onboard', 'project_id': payload.project_id, 'version': payload.version, 'origin': payload.origin}
    if payload.auth_env:
        spec['auth_env'] = payload.auth_env
    if payload.fixture_relay:
        spec['fixture_relay'] = True
    try:
        with _lock:
            return onboard(spec, data_dir(), max_pages=payload.max_pages)
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f'{type(exc).__name__}: onboarding unavailable') from exc


def _onboarding(project):
    path = data_dir() / 'onboarding' / project / 'latest.json'
    if not path.is_file():
        raise HTTPException(404, 'project not onboarded yet')
    return json.loads(path.read_text())


def _approvals(project):
    folder = data_dir() / 'approvals' / project
    return {p.stem: json.loads(p.read_text()) for p in sorted(folder.glob('*.json'))} if folder.is_dir() else {}


def _failing(project):
    latest = {}
    for run in sorted(_store().list(project, 100), key=lambda r: r['created_at']):
        latest[run['scenario_id']] = run['verdict']
    return {scenario for scenario, verdict in latest.items() if verdict == 'FAIL'}


@router.get('/projects')
def projects():
    root = data_dir() / 'onboarding'
    found = []
    for folder in sorted(root.iterdir()) if root.is_dir() else []:
        latest = folder / 'latest.json'
        if not latest.is_file():
            continue
        data = json.loads(latest.read_text())
        versions = sorted(p.stem for p in folder.glob('*.json') if p.name != 'latest.json')
        found.append({'project_id': folder.name, 'origin': data['origin'], 'version': data['version'],
                      'versions': versions, 'summary': data['summary'],
                      'approved': len(_approvals(folder.name))})
    return found


@router.get('/projects/{project}')
def project_detail(project: str):
    from services.universal_ui.onboard import select_regressions
    _safe(project, 'project_id')
    data = _onboarding(project)
    approvals = _approvals(project)
    failing = _failing(project)
    for workflow in data['workflows']:
        workflow['approved'] = workflow['id'] in approvals
        workflow['last_verdict'] = next((r['verdict'] for r in _store().list(project, 100)
                                         if r['scenario_id'] == workflow['id']), None)
    selection = select_regressions(data.get('changes', {}), data['workflows'], failing) if data.get('changes') or failing else None
    return {**data, 'approvals': approvals, 'regression_selection': selection}


class ApproveRequest(BaseModel):
    project_id: str
    workflow_id: str
    approval: dict


@router.post('/approve')
def approve_workflow(payload: ApproveRequest):
    from services.universal_ui.onboard import approve
    from services.universal_ui.spec import validate
    project = _safe(payload.project_id, 'project_id')
    workflow_id = _safe(payload.workflow_id, 'workflow_id')
    workflow = next((w for w in _onboarding(project)['workflows'] if w['id'] == workflow_id), None)
    if workflow is None:
        raise HTTPException(404, 'workflow not found')
    try:
        scenario = approve(workflow, payload.approval)
        validate(scenario, local_files=local_files())
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:  # jsonschema.ValidationError and friends
        raise HTTPException(422, f'{type(exc).__name__}: {str(exc)[:300]}') from exc
    folder = data_dir() / 'approvals' / project
    folder.mkdir(parents=True, exist_ok=True)
    (folder / (workflow_id + '.json')).write_text(json.dumps(payload.approval, indent=2))
    return {'workflow_id': workflow_id, 'approved': True, 'scenario': scenario}


# ---------------------------------------------------------------- execution

def _scenario_for(project, workflow_id, version=None):
    from services.universal_ui.onboard import approve
    data = _onboarding(project)
    workflow = next((w for w in data['workflows'] if w['id'] == workflow_id), None)
    if workflow is None or 'scenario' not in workflow:
        raise HTTPException(404, 'workflow not found or not generated')
    approval = _approvals(project).get(workflow_id)
    if workflow.get('mutating'):
        if approval is None:
            raise HTTPException(409, 'workflow changes state: approve it first')
        return approve(workflow, {**approval, 'version': version or data['version']})
    return {**workflow['scenario'], **({'version': version} if version else {})}


def _worker(job_id, scenario):
    from services.universal_ui.runner import execute
    job = _jobs[job_id]
    with _lock:
        job['status'] = 'running'
        job['started_at'] = time.time()
        try:
            record = execute(scenario, data_dir(), local_files=local_files())
            folder = data_dir() / 'api-scenarios'
            folder.mkdir(parents=True, exist_ok=True)
            (folder / (record['run_id'] + '.json')).write_text(json.dumps(scenario, indent=2))
            job.update(status='done', run_id=record['run_id'], verdict=record['verdict'])
        except Exception as exc:
            job.update(status='error', error=f'{type(exc).__name__}: {str(exc)[:300]}')
            traceback.print_exc()
        job['finished_at'] = time.time()


def _enqueue(scenario, label):
    from services.universal_ui.spec import validate
    try:
        validate(scenario, local_files=local_files())
    except Exception as exc:
        raise HTTPException(422, f'{type(exc).__name__}: {str(exc)[:300]}') from exc
    job_id = uuid4().hex[:12]
    _jobs[job_id] = {'job_id': job_id, 'label': label, 'scenario_id': scenario['id'], 'status': 'queued',
                     'queued_at': time.time()}
    while len(_jobs) > 200:
        _jobs.popitem(last=False)
    threading.Thread(target=_worker, args=(job_id, scenario), daemon=True).start()
    return _jobs[job_id]


class RunRequest(BaseModel):
    project_id: str | None = None
    workflow_id: str | None = None
    scenario: dict | None = None


@router.post('/runs', status_code=202)
def start_run(payload: RunRequest):
    if payload.scenario is not None:
        return _enqueue(payload.scenario, 'custom scenario')
    if not payload.project_id or not payload.workflow_id:
        raise HTTPException(422, 'give project_id + workflow_id, or a scenario')
    project = _safe(payload.project_id, 'project_id')
    return _enqueue(_scenario_for(project, _safe(payload.workflow_id, 'workflow_id')), 'workflow')


@router.post('/projects/{project}/regressions', status_code=202)
def run_regressions(project: str):
    _safe(project, 'project_id')
    selection = project_detail(project)['regression_selection']
    if not selection or not selection['selected']:
        return {'started': [], 'skipped': [], 'note': 'nothing selected: no UI changes and no failing workflows'}
    started, skipped = [], []
    for item in selection['selected']:
        try:
            job = _enqueue(_scenario_for(project, item['id']), 'regression: ' + '; '.join(item['reasons']))
            started.append({**job, 'reasons': item['reasons']})
        except HTTPException as exc:
            skipped.append({'id': item['id'], 'reason': exc.detail})
    return {'started': started, 'skipped': skipped}


@router.get('/jobs')
def jobs():
    return list(reversed(_jobs.values()))


@router.get('/jobs/{job_id}')
def job(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(404, 'job not found')
    return _jobs[job_id]


# ---------------------------------------------------------------- sample app

class SampleRequest(BaseModel):
    release: str = Field('v2', pattern='^v[23]$')
    broken: bool = True


@router.get('/sample-app')
def sample_status():
    return {k: v for k, v in _sample.items() if k in ('origin', 'oracle', 'release', 'broken', 'auth_env', 'log_file')} or {'running': False}


@router.post('/sample-app')
def sample_start(payload: SampleRequest):
    """Start the bundled lending-library app (synthetic data, loopback) to try every step."""
    from reference_apps.lending_library import lending_library
    sample_stop()
    token_env = 'QA_TARGET_TOKEN_SAMPLE_APP'
    os.environ[token_env] = 'sample-' + uuid4().hex  # synthetic, never shown in the UI
    folder = data_dir() / 'sample-app'
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / f'{payload.release}-{"defect" if payload.broken else "fixed"}.log'
    log.touch()
    context = lending_library(log, broken=payload.broken, token=os.environ[token_env], release=payload.release,
                              otlp_endpoint=os.getenv('QA_OTLP_TRACES_ENDPOINT') or None)
    origin, oracle = context.__enter__()
    _sample.update(context=context, origin=origin, oracle=oracle, release=payload.release, broken=payload.broken,
                   auth_env=token_env, log_file=str(log) if local_files() else None, running=True)
    return sample_status()


@router.delete('/sample-app')
def sample_stop():
    context = _sample.pop('context', None)
    if context is not None:
        context.__exit__(None, None, None)
    _sample.clear()
    return {'running': False}


@router.get('/settings')
def settings():
    return {'data_dir': str(data_dir()), 'local_files': local_files(),
            'log_root': os.getenv('QA_LOG_ROOT'), 'otlp_endpoint': os.getenv('QA_OTLP_TRACES_ENDPOINT'),
            'token_auth': bool(os.getenv('QA_API_TOKEN'))}
