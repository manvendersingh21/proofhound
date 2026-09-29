"""Store a saved UI skill in Neo4j and label which steps need an LLM agent.

Steps the universal UI runner can execute from the recording alone are labeled
agent_required=false and replay through Playwright + OTLP. Only steps the
recording cannot decide are labeled agent_required=true.

Local bolt: bolt://127.0.0.1:7687, user neo4j, password from QA_NEO4J_PASSWORD
(default proofhound-local, matching the proofhound-neo4j container).
"""
import hashlib
import json
import os
from contextlib import contextmanager

from neo4j import GraphDatabase
from services.universal_ui.spec import ACTIONS, EXPECT_NEEDED, LOCATOR_NEEDED

PROVENANCE = 'recording:from_demo'
RETRY_VERDICTS = ('FAIL', 'INCONCLUSIVE')


def scenario_hash(spec):
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]


def label_step(step, previous=None, changed=False):
    """Return (agent_required, reason) for one scenario step."""
    action = step.get('action')
    if step.get('needs_agent'):
        return True, 'user marked needs_agent'
    if action not in ACTIONS:
        return True, f'runner has no deterministic action "{action}"'
    if action == 'goto' and 'path' not in step:
        return True, 'navigation without a path'
    if action in LOCATOR_NEEDED and 'locator' not in step:
        return True, 'no locator and no path'
    if action in EXPECT_NEEDED and 'expected' not in step:
        return True, 'assertion without an expected value'
    if action == 'assert_visual' and 'baseline' not in step:
        return True, 'visual check without an approved baseline'
    if changed and previous and previous.get('verdict') in RETRY_VERDICTS:
        if step['name'] in previous.get('failed_steps', []) or step['name'] not in previous.get('steps', []):
            return True, f'last run was {previous["verdict"]} and the scenario changed at this step'
    return False, 'deterministic replay'


def label_steps(spec, previous=None):
    changed = bool(previous) and previous.get('skill_hash', previous.get('digest')) != scenario_hash(spec)
    return [(step, *label_step(step, previous, changed)) for step in spec['steps']]


def neo4j_uri():
    return os.environ.get('QA_NEO4J_URI', 'bolt://127.0.0.1:7687')


@contextmanager
def _session():
    driver = GraphDatabase.driver(
        neo4j_uri(),
        auth=(os.environ.get('QA_NEO4J_USER', 'neo4j'),
              os.environ.get('QA_NEO4J_PASSWORD', 'proofhound-local')))
    try:
        with driver.session() as session:
            yield session
    finally:
        driver.close()


def store_skill(name, spec, scenario_path, previous=None):
    """Write Skill, Step, HAS_STEP and NEXT in Neo4j. Returns the labeled steps."""
    version = scenario_hash(spec)
    rows = []
    labeled = []
    for index, (step, required, reason) in enumerate(label_steps(spec, previous)):
        rows.append({
            'id': f'skill:{name}:step:{index:03d}:{step["name"]}',
            'name': step['name'], 'action': step['action'],
            'locator': json.dumps(step.get('locator')),
            'idx': index, 'agent_required': required, 'reason': reason,
        })
        labeled.append({'name': step['name'], 'action': step['action'],
                        'agent_required': required, 'reason': reason})
    with _session() as session:
        session.run(
            '''
            MERGE (s:Skill {name: $name})
            SET s.origin = $origin, s.scenario_path = $scenario_path, s.scenario_hash = $hash,
                s.provenance = $provenance
            WITH s
            OPTIONAL MATCH (s)-[:HAS_STEP]->(old:Step)
            DETACH DELETE old
            WITH DISTINCT s
            UNWIND $steps AS step
            CREATE (n:Step {id: step.id})
            SET n.name = step.name, n.action = step.action, n.locator = step.locator,
                n.idx = step.idx, n.agent_required = step.agent_required, n.reason = step.reason
            CREATE (s)-[:HAS_STEP]->(n)
            WITH n
            ORDER BY n.idx
            WITH collect(n) AS nodes
            UNWIND range(0, size(nodes) - 2) AS i
            WITH nodes[i] AS left, nodes[i + 1] AS right
            CREATE (left)-[:NEXT]->(right)
            ''',
            name=name, origin=spec.get('origin'), scenario_path=str(scenario_path),
            hash=version, provenance=PROVENANCE, steps=rows)
    return labeled


def agent_steps(name, version):
    """Steps of the current skill version that Neo4j says need an agent."""
    with _session() as session:
        records = session.run(
            '''
            MATCH (s:Skill {name: $name, scenario_hash: $version})-[:HAS_STEP]->(n:Step)
            WHERE n.agent_required = true
            RETURN n.name AS name, n.action AS action, n.reason AS reason
            ORDER BY n.idx
            ''',
            name=name, version=version)
        return [{'name': row['name'], 'action': row['action'], 'agent_required': True,
                 'reason': row['reason']} for row in records]


def skill_record(name):
    """Skill hash plus HAS_STEP and NEXT counts, for tests and status."""
    with _session() as session:
        row = session.run(
            '''
            MATCH (s:Skill {name: $name})
            RETURN s.scenario_hash AS hash,
                   COUNT { (s)-[:HAS_STEP]->() } AS has_step,
                   COUNT { (s)-[:HAS_STEP]->()-[:NEXT]->() } AS next_count
            ''',
            name=name).single()
    if row is None:
        return None
    return {'scenario_hash': row['hash'], 'has_step': row['has_step'], 'next_count': row['next_count']}


def executable(spec):
    """The runner's schema rejects unknown keys, so drop graph-only labels before replay."""
    clean = json.loads(json.dumps(spec))
    for step in clean['steps']:
        step.pop('needs_agent', None)
    return clean
