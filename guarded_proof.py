"""Trusted-source proof only: audit hooks do not confine native code, metadata, dir_fd or inherited descriptors."""
import os
from pathlib import Path
import sys

candidate = Path(__file__).resolve().parent
raw_work = os.environ.get('ARCTURION_BRAIN_WORK_ROOT', '')
work = Path(raw_work).resolve() if raw_work else None
if not work or work.parent != candidate or not work.name.startswith('.proof-') or work.exists():
    raise SystemExit('A new .proof-* directory directly inside the candidate is required.')
if os.environ.get('HOME') != str(work / 'home'):
    raise SystemExit('HOME must be the invented work directory.')
work.mkdir(mode=0o700)
(work / 'home').mkdir(mode=0o700)
read_roots = (candidate / 'engine', candidate / 'canon', work,
              Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve(), Path('/usr/lib'), Path('/System/Library'))
denied = []

def inside(path, roots):
    if isinstance(path, int):
        return True
    if not isinstance(path, (str, bytes, os.PathLike)):
        return False
    try:
        p = Path(os.fsdecode(path)).resolve()
        return any(p == root or p.is_relative_to(root) for root in roots)
    except (OSError, ValueError, RuntimeError):
        return False

def guard(event, args):
    reject = event.startswith(('socket.', 'subprocess.', 'os.exec', 'os.spawn', 'os.fork', 'os.posix_spawn', 'ctypes.')) or event == 'os.system'
    if event == 'open':
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        write = (isinstance(mode, str) and any(ch in mode for ch in 'wax+')) or (isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)))
        reject |= not inside(args[0], (work,) if write else read_roots)
    if event == 'sqlite3.connect':
        reject |= not inside(args[0], (work,))
    if event in ('os.listdir', 'os.scandir', 'os.chdir'):
        package_root = bool(args and isinstance(args[0], (str, bytes, os.PathLike)) and Path(os.fsdecode(args[0])).resolve() == candidate)
        reject |= not args or not (package_root or inside(args[0], read_roots))
    if event in ('os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod', 'os.chown', 'os.truncate', 'os.utime'):
        reject |= not inside(args[0], (work,))
    if event in ('os.rename', 'os.replace', 'os.link', 'os.symlink'):
        reject |= not all(inside(p, (work,)) for p in args[:2])
    if reject:
        denied.append(event)
        raise PermissionError('guard denied ' + event)

sys.path[:] = [str(candidate)] + [p for p in sys.path if p and inside(p, read_roots) and not Path(p).resolve().is_relative_to(candidate)]
sys.addaudithook(guard)
import json
policy_path = work / 'invented-policy.json'
policy_path.write_text(json.dumps({
    'excluded_agents': {'invented-private-agent': 'invented exclusion'},
    'skip_path_fragments': ['invented-private-directory/'],
    'privacy_topic_pattern': 'invented-sensitive-topic',
    'privacy_person_pattern': 'invented-person',
    'privacy_path_marker': 'invented-sensitive-path',
    'restricted_recovery_root': str(work / 'desktop' / 'private_data'),
    'legacy_protected_root': str(work / 'universe' / 'private%data'),
}))
os.environ.pop('ARCTURION_BRAIN_POLICY_PATH', None)
for invalid in (None, '{}'):
    if invalid is not None:
        invalid_path = work / 'invalid-policy.json'
        invalid_path.write_text(invalid)
        os.environ['ARCTURION_BRAIN_POLICY_PATH'] = str(invalid_path)
    try:
        import engine.config
    except RuntimeError:
        pass
    else:
        raise AssertionError('invalid policy was accepted')
os.environ['ARCTURION_BRAIN_POLICY_PATH'] = str(policy_path)
for invalid_canon in ('relative-canon', str(work / 'missing-canon')):
    os.environ['ARCTURION_BRAIN_CANON_DIR'] = invalid_canon
    try:
        import engine.config
    except RuntimeError:
        pass
    else:
        raise AssertionError('invalid ontology override was accepted')
import runpy
fixture_canon = work / 'alternate-canon'
fixture_canon.mkdir()
(fixture_canon / '0007_ONTOLOGY.yaml').write_text((candidate / 'canon' / '0007_ONTOLOGY.yaml').read_text())
os.environ['ARCTURION_BRAIN_CANON_DIR'] = str(fixture_canon)
if runpy.run_path(str(candidate / 'engine' / 'config.py'))['CANON_DIR'] != fixture_canon:
    raise AssertionError('valid ontology override not selected')
os.environ.pop('ARCTURION_BRAIN_CANON_DIR', None)
print('policy refusal and ontology override checks: PASS')

try:
    import engine.config as C
    import engine.db as DB
    import engine.ingest as ingest
    import engine.brain as brain
    import engine.http_server as http
    import engine.mcp_server as mcp
    import engine.capacity_admission as admission
    rich_yaml = os.environ.get('ARCTURION_BRAIN_TEST_YAML') == '1'
    if rich_yaml:
        from engine import ontology
        data = ontology.load()
        if not ontology.is_transitive('contains'):
            raise RuntimeError('default ontology API failed')
        if not {'cites', 'supports', 'depends_on'} <= (set(data['predicates']) - set(data['retired_predicates'])):
            raise RuntimeError('ontology active predicates missing')
        print('YAML ontology active predicates: PASS')
    print('guarded candidate imports: PASS')
    # Exercise the public write path and SQLite triggers against literal aliases.
    test_conn = DB.connect(work / 'path-regression.sqlite')
    DB.init_db(test_conn)
    path_cases = [
        (str(C.DESKTOP_ROOT / 'private_data' / 'direct.md'), True),
        ('private_data/direct.md', True),
        ('desktop/private_data/direct.md', True),
        (str(C.DESKTOP_ROOT / 'privateXdata' / 'direct.md'), False),
        ('privateXdata/direct.md', False),
        (str(C.UNIVERSE_ROOT / 'private%data' / 'direct.md'), True),
        ('private%data/direct.md', True),
        ('universe/private%data/direct.md', True),
        (str(C.UNIVERSE_ROOT / 'privateXdata' / 'direct.md'), False),
        ('private%data-extra/direct.md', False),
    ]
    for index, (stored_path, protected) in enumerate(path_cases):
        node = {'id': f'fixture-path-{index}', 'type': 'Source', 'family': 'document',
                'title': 'Invented source', 'path': stored_path, 'content_hash': f'fixture-{index}',
                'body_excerpt': 'Invented content'}
        DB.upsert_node(test_conn, node)
        row = test_conn.execute('SELECT status,path,body_excerpt FROM nodes WHERE id=?', (node['id'],)).fetchone()
        if (row['status'] == 'privacy_excluded') != protected:
            raise RuntimeError(f'protected path regression case {index} failed')
        if not protected and (row['path'] != stored_path or row['body_excerpt'] != 'Invented content'):
            raise RuntimeError(f'allowed path regression case {index} was altered')
    for index, (stored_path, protected) in enumerate(path_cases):
        node_id = f'fixture-trigger-{index}'
        test_conn.execute(
            'INSERT INTO nodes (id,type,family,title,path,status,content_hash,body_excerpt) VALUES (?,?,?,?,?,?,?,?)',
            (node_id, 'Source', 'document', 'Invented source', stored_path, 'active', f'trigger-{index}', 'Invented content'))
        row = test_conn.execute('SELECT status,path,body_excerpt FROM nodes WHERE id=?', (node_id,)).fetchone()
        if (row['status'] == 'privacy_excluded') != protected:
            raise RuntimeError(f'protected trigger regression case {index} failed')
        if not protected and (row['path'] != stored_path or row['body_excerpt'] != 'Invented content'):
            raise RuntimeError(f'allowed trigger regression case {index} was altered')
    test_conn.execute("UPDATE nodes SET path=?,body_excerpt=? WHERE id=?",
                      ('private_data/updated.md', 'Invented content', 'fixture-trigger-4'))
    row = test_conn.execute("SELECT status FROM nodes WHERE id='fixture-trigger-4'").fetchone()
    if row['status'] != 'privacy_excluded':
        raise RuntimeError('protected update trigger regression failed')
    test_conn.close()
    print('literal protected-path aliases: PASS')
    docs = C.DESKTOP_ROOT / 'invented'
    docs.mkdir(parents=True, exist_ok=True)
    admission.REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    admission.REGISTRY_PATH.write_text('[]')
    a = docs / 'amber-workflow.md'
    b = docs / 'amber-decision.md'
    a.write_text('---\nid: invented-workflow\ntype: Decision\ntitle: Amber workflow\nstatus: active\n---\nAmber workflow uses a reviewed synthetic procedure.\n')
    b.write_text('---\nid: invented-decision\ntype: Decision\ntitle: Amber decision\nstatus: active\n---\nAmber decision explains the invented procedure.\n')
    result = ingest.ingest_paths([a, b], embed=False, verbose=False)
    print('invented ingest result:', result)
    conn = DB.connect()
    rows = [dict(row) for row in conn.execute('SELECT id,title,path,content_hash FROM nodes')]
    print('invented indexed count:', len(rows))
    if len(rows) < 2:
        raise RuntimeError('invented documents not indexed')
    workflow = next(row for row in rows if row['path'].endswith('amber-workflow.md'))
    decision = next(row for row in rows if row['path'].endswith('amber-decision.md'))
    if rich_yaml and (workflow['title'] != 'Amber workflow' or decision['title'] != 'Amber decision'):
        raise RuntimeError('rich YAML frontmatter not applied')
    if not DB.insert_edge(conn, {'from': workflow['id'], 'to': decision['id'], 'predicate': 'cites', 'family': 'evidence', 'asserted_by': 'synthetic'}):
        raise RuntimeError('invented typed edge was not stored')
    conn.commit()
    conn.close()
    pack = brain.query('Amber workflow', token_budget=1000, capacity='business')
    print('invented query:', len(pack.nodes), 'nodes')
    if not any(e['from_id'] == workflow['id'] and e['to_id'] == decision['id'] and e['predicate'] == 'cites' for e in pack.edges):
        raise RuntimeError('query lacks nodes, edges or citations')
    import json
    edge_text = f"`{workflow['id']}` —[cites]→ `{decision['id']}`"
    def verify_pack(value):
        ids = {item['id'] for item in value['sources']}
        return ({workflow['id'], decision['id']} <= ids and workflow['path'] in value['citations'] and decision['path'] in value['citations'] and edge_text in value['text_bundle'])
    if not verify_pack(pack.to_dict()):
        raise RuntimeError('query did not return expected sources, citations and directed edge')
    response = mcp.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'brain_query', 'arguments': {'topic': 'Amber workflow', 'format': 'json'}}})
    if not isinstance(response, dict) or response.get('id') != 1 or 'result' not in response:
        raise RuntimeError('MCP dispatch failed')
    if not verify_pack(json.loads(response['result']['content'][0]['text'])):
        raise RuntimeError('MCP query omitted expected sources or edge')
    print('MCP query dispatch: PASS')
    handler = object.__new__(http.Handler)
    handler.path = '/query?topic=Amber%20workflow&format=json'
    replies = []
    handler._json = lambda status, body: replies.append((status, body))
    handler._send = lambda status, body, *args: replies.append((status, body))
    handler._do_GET()
    if not replies or replies[0][0] != 200 or not verify_pack(replies[0][1]):
        raise RuntimeError('HTTP query dispatch failed')
    print('HTTP query dispatch: PASS')
    previous = workflow['content_hash']
    a.write_text(a.read_text() + '\nNew invented information for the revised workflow.\n')
    revised = ingest.ingest_paths([a], embed=False, verbose=False)
    conn = DB.connect()
    after = conn.execute('SELECT content_hash FROM nodes WHERE id=?', (workflow['id'],)).fetchone()
    if not after or after['content_hash'] == previous or revised['upserted'] != 1:
        raise RuntimeError('source update was not indexed')
    print('invented source update: PASS')
    b.unlink()
    from engine.maintenance.content_dedup import gc_missing_path
    cleaned = gc_missing_path(conn, dry_run=False, verbose=False)
    remaining = conn.execute('SELECT count(*) FROM nodes WHERE id=?', (decision['id'],)).fetchone()[0]
    if cleaned['removed'] != 1 or remaining:
        raise RuntimeError('missing invented source was not reconciled')
    conn.close()
    print('invented source deletion reconciliation: PASS')
    # Actual query with corroborated invented journal; recorder never starts a worker.
    from engine import evidence_fields as EF
    if EF.ENABLED or EF.ASYNC:
        raise RuntimeError('judging must default to disabled')
    original_brain_db = C.BRAIN_DB
    C.BRAIN_DB = work / "journal-regression.sqlite"
    conn = DB.connect()
    DB.init_db(conn)
    for node_id, title, metadata in [('origin', 'Citrine evidence', {}),
        ('journal:amber', 'Citrine journal', {'journal': {'source_node': 'origin', 'source_sha256': 'hash-origin'}})] + [
        (f'other{i}', f'brown{i}', {}) for i in range(12)]:
        DB.upsert_node(conn, {'id': node_id, 'type': 'Source', 'family': 'document', 'title': title,
            'path': f'invented/{node_id}.md', 'content_hash': f'hash-{node_id}',
            'summary': title, 'body_excerpt': f'invented text {title}', 'metadata': metadata})
    DB.insert_edge(conn, {'from': 'origin', 'to': 'journal:amber', 'predicate': 'supports',
                          'family': 'evidence', 'asserted_by': 'synthetic'})
    conn.commit()
    conn.close()
    seen = []
    original_enqueue = EF._enqueue
    EF._enqueue = lambda topic, node, key: seen.append((topic, node['id']))
    try:
        brain.query('Citrine', token_budget=1000, capacity='business')
        if seen:
            raise RuntimeError('default query queued a judge')
        EF.ENABLED = EF.ASYNC = True
        os.environ['ARCTURION_BRAIN_CODEX_BIN'] = str(work / 'invented-provider')
        brain.query('Citrine', token_budget=1000, capacity='business')
        if not any(node_id == 'journal:amber' for _, node_id in seen):
            raise RuntimeError('enabled journal did not reach recorder')
    finally:
        EF.ENABLED = EF.ASYNC = False
        EF._enqueue = original_enqueue
        C.BRAIN_DB = original_brain_db
        os.environ.pop('ARCTURION_BRAIN_CODEX_BIN', None)
    print('answer judge opt-in: PASS (recorder only)')
    if denied:
        raise RuntimeError('guard denied unexpected access')
    print('denied audit events:', sorted(set(denied)))
except Exception as exc:
    print('guarded candidate imports: FAIL:', type(exc).__name__, str(exc))
    print('denied audit events:', sorted(set(denied)))
    raise
