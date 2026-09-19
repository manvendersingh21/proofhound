"""Single-tenant development control plane. Bind to loopback; do NOT expose as SaaS.

Optional QA_API_TOKEN protects all non-health endpoints. Multi-tenant auth,
rate limits, persistent worker supervision and isolated untrusted execution
are deliberately NOT claimed by this service.
"""
import base64
import binascii
import hmac
import html
import json
import os
import sqlite3
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from services.runtime.queue import Queue
from services.runtime.worker import work
from services.engine.manifest import validate_manifest
from services.engine.runner import execute
from services.engine.store import Store
from services.graph.graph import Graph
from benchmarks.benchmark import benchmark

DATA = Path(os.getenv('QA_DATA_DIR', '.local-runs')).resolve()
DATA.mkdir(parents=True, exist_ok=True)
QUEUE = Queue(DATA / 'jobs.sqlite3')
STORE = Store(DATA)
GRAPH = Graph(DATA / 'graph.sqlite3')
app = FastAPI(title='ProofHound local development API', version='0.5.0')
KINDS = {'agentic', 'fullstack', 'fault-injection'}


@app.middleware('http')
async def development_auth(request: Request, call_next):
    token = os.getenv('QA_API_TOKEN')
    if request.client and request.client.host not in ('127.0.0.1', '::1', 'testclient') and os.getenv('QA_ALLOW_PUBLIC_DEV_API') != '1':
        return Response(status_code=403, content='Development API restricted to loopback')
    # /otlp has its own ingest token (QA_OTLP_INGEST_TOKEN); the runner's exporter sends no API token.
    if token and request.url.path != '/health' and not request.url.path.startswith('/otlp/'):
        supplied = request.headers.get('x-qa-api-token', '')
        basic = request.headers.get('authorization', '')
        if basic.startswith('Basic '):
            try:
                username, password = base64.b64decode(basic[6:], validate=True).decode().split(':', 1)
                if username == 'qa':
                    supplied = password
            except (binascii.Error, UnicodeError, ValueError):
                pass
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            return Response(status_code=401, content='Authentication required',
                            headers={'WWW-Authenticate': 'Basic realm=ProofHound'})
    if request.headers.get('content-length'):
        try:
            if int(request.headers['content-length']) > 131072:
                return Response(status_code=413, content='Payload too large')
        except ValueError:
            return Response(status_code=400, content='Invalid content length')
    return await call_next(request)


class Submission(BaseModel):
    kind: str
    name: str = 'demo'
    retry: bool = True


@app.get('/health')
def health():
    return {'status': 'ok', 'mode': 'single-tenant-loopback-development',
            'token_auth_enabled': bool(os.getenv('QA_API_TOKEN'))}


@app.post('/jobs', status_code=202)
def submit(payload: Submission):
    if payload.kind not in KINDS:
        raise HTTPException(422, 'unsupported scenario')
    if len(payload.name) > 80:
        raise HTTPException(422, 'name too long')
    return {'job_id': QUEUE.submit(payload.model_dump()), 'status': 'pending'}


@app.post('/workers/run-once')
def run_once():
    # Only bind to loopback. This worker is not a sandbox for arbitrary code.
    return work(QUEUE, DATA, 'api-local-worker') or {'status': 'idle'}


@app.get('/jobs/{job_id}')
def status(job_id: str):
    with sqlite3.connect(QUEUE.path) as db:
        row = db.execute('SELECT status,result,attempts FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, 'job not found')
    return {'job_id': job_id, 'status': row[0], 'result': json.loads(row[1]) if row[1] else None, 'attempts': row[2]}


@app.get('/runs')
def runs(limit: int = 25):
    if not 1 <= limit <= 100:
        raise HTTPException(422, 'limit must be 1..100')
    path = DATA / 'history.sqlite3'
    if not path.exists():
        return []
    with sqlite3.connect(path) as db:
        rows = db.execute('SELECT run_id,kind,verdict,digest,created_at FROM history ORDER BY rowid DESC LIMIT ?', (limit,)).fetchall()
    return [dict(zip(('run_id', 'kind', 'verdict', 'evidence_sha256', 'created_at'), row)) for row in rows]


@app.post('/engine/execute', status_code=200)
def run_manifest(manifest: dict):
    try:
        validate_manifest(manifest)
        return execute(manifest, DATA, graph=GRAPH)
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f'{type(exc).__name__}: execution unavailable') from exc


@app.get('/engine/runs')
def engine_runs(project_id: str | None = None, limit: int = 50):
    try:
        return STORE.list(project_id, limit)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get('/engine/runs/{run_id}')
def engine_run(run_id: str):
    record = STORE.get(run_id)
    if record is None:
        raise HTTPException(404, 'run not found')
    return record


@app.get('/engine/evidence/{digest}')
def engine_evidence(digest: str):
    try:
        return Response(STORE.evidence_bytes(digest), media_type='application/json',
                        headers={'Cache-Control': 'no-store'})
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(404, 'evidence not found') from exc
    except IOError as exc:
        raise HTTPException(500, 'evidence integrity failure') from exc


@app.post('/benchmark/run')
def run_benchmark():
    report = benchmark(DATA)
    (DATA/'benchmark-latest.json').write_text(json.dumps(report, indent=2))
    return report


@app.get('/benchmark/latest')
def benchmark_latest():
    path = DATA/'benchmark-latest.json'
    if not path.is_file():
        raise HTTPException(404, 'benchmark not run yet')
    return json.loads(path.read_text())


@app.get('/graph/summary')
def graph_summary():
    with GRAPH.connect() as db:
        nodes = db.execute('SELECT kind, COUNT(*) FROM nodes GROUP BY kind').fetchall()
        edges = db.execute('SELECT relation, COUNT(*) FROM edges GROUP BY relation').fetchall()
    return {'nodes': dict(nodes), 'edges': dict(edges)}


class DiscoveryRequest(BaseModel):
    relative_path: str
    project_id: str = 'local'
    version: str = 'local'
    max_files: int = 500


@app.post('/projects/discover')
def discover_source(payload: DiscoveryRequest):
    from services.discovery.project import discover_project
    from services.discovery.suggestions import suggest
    root=Path(os.getenv('QA_PROJECTS_ROOT', Path(__file__).resolve().parents[2])).resolve()
    if not payload.relative_path or Path(payload.relative_path).is_absolute() or '\x00' in payload.relative_path:
        raise HTTPException(422, 'repository must be a relative path within QA_PROJECTS_ROOT')
    resolved=(root/payload.relative_path).resolve()
    if not resolved.is_relative_to(root) or not resolved.is_dir():
        raise HTTPException(422, 'repository outside project root or not a directory')
    if not 1<=payload.max_files<=3000:
        raise HTTPException(422, 'max_files must be 1..3000')
    try:
        discovered=discover_project(resolved,GRAPH,project=payload.project_id,
                                    version=payload.version,limit=payload.max_files)
        drafts=suggest(GRAPH,payload.project_id,payload.version)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc
    return {'project_id':payload.project_id,'version':payload.version,
            'discovery':discovered,'proposed_scenarios':drafts,
            'tests_automatically_approved':False}


@app.get('/telemetry/traces/{trace_id}')
def stored_trace(trace_id: str):
    from services.telemetry.receiver import read_trace
    try:
        spans=read_trace(trace_id)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc
    if not spans:
        raise HTTPException(404,'trace not found')
    return {'trace_id':trace_id,'spans':spans}


STATIC = Path(__file__).resolve().parent / 'static'


@app.get('/', response_class=HTMLResponse)
@app.get('/app', response_class=HTMLResponse)
@app.get('/dashboard', response_class=HTMLResponse)
def web_app():
    # Single-page app; all data comes from /api/* and is rendered as text, never as HTML.
    return HTMLResponse((STATIC / 'index.html').read_text(), headers={
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
        'Content-Security-Policy': "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'"})


from fastapi.staticfiles import StaticFiles
from services.platform.app_api import router as app_router
from services.telemetry.receiver import app as otlp_receiver
app.mount('/static', StaticFiles(directory=STATIC), name='static')
app.include_router(app_router)
app.mount('/otlp', otlp_receiver)

# Intentionally limited A2A surface, authenticated by the same development middleware.
from services.platform.a2a import router as a2a_router
app.include_router(a2a_router)


@app.post('/ui/execute')
def execute_ui_scenario(spec: dict):
    """Remote API cannot access owner-local log files or baseline paths."""
    from services.universal_ui.spec import validate
    from services.universal_ui.runner import execute as run_ui
    try:
        validate(spec, local_files=False)
        return run_ui(spec, DATA, graph=GRAPH, local_files=False)
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f'{type(exc).__name__}: UI engine unavailable') from exc


@app.post('/ui/discover')
def discover_ui(spec: dict, max_pages: int = 10):
    """Read-only same-origin crawl, no actions or baseline/file access."""
    from services.universal_ui.discovery import discover
    try:
        return discover(spec, max_pages=max_pages, graph=GRAPH)
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f'{type(exc).__name__}: UI discovery unavailable') from exc


@app.get('/ui/issues')
def ui_issues(project_id: str | None = None, limit: int = 50):
    from services.universal_ui.issues import IssueIndex
    try:
        return IssueIndex(DATA).list(project_id, limit)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get('/ui/issues/{fingerprint}')
def ui_issue(fingerprint: str):
    from services.universal_ui.issues import IssueIndex
    try:
        issue = IssueIndex(DATA).get(fingerprint)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if issue is None: raise HTTPException(404, 'finding not found')
    return issue


@app.get('/ui/runs/{run_id}/report', response_class=HTMLResponse)
def ui_report(run_id: str):
    from services.universal_ui.report import render
    record = STORE.get(run_id)
    if record is None or record['kind'] != 'universal-ui':
        raise HTTPException(404, 'UI run not found')
    try:
        observed = json.loads(STORE.evidence_bytes(record['evidence_sha256']))
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(404, 'evidence not found') from exc
    except (OSError, IOError) as exc:
        raise HTTPException(500, 'evidence integrity failure') from exc
    return HTMLResponse(render(record, observed), headers={'Cache-Control': 'no-store',
                       'X-Content-Type-Options': 'nosniff'})


@app.post('/ui/auto-audit')
def ui_auto_audit(spec: dict, max_pages: int = 10):
    from services.universal_ui.autopilot import audit
    try:
        return audit(spec, DATA, max_pages=max_pages, graph=GRAPH, local_files=False)
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f'{type(exc).__name__}: UI auto-audit unavailable') from exc
