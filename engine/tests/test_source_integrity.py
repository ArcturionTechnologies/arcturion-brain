from datetime import datetime, timezone
from engine import db as DB
from engine import source_integrity as S
from engine import brain as B
from engine.resolver import is_archive_path

def test_private_version_is_denied_and_sanitized_replacement_is_allowed(tmp_path):
    c=DB.connect(tmp_path/'test.sqlite'); DB.init_db(c)
    n={'id':'personal','type':'Source','family':'document','title':'Founder preference',
       'summary':'Synthetic Founder alcohol-history fixture.', 'content_hash':'old'}
    DB.upsert_node(c,n)
    assert S.filter_retrieval(c,[n],historical=True)==[]
    clean={**n,'summary':'Prefers concise explanations.','content_hash':'new'}
    DB.upsert_node(c,clean)
    assert S.filter_retrieval(c,[clean])==[clean]
    assert S.filter_retrieval(c,[{**n,'summary':'','title':'hidden'}])==[]
    c.close()

def test_general_nonpersonal_knowledge_is_retained(tmp_path):
    c=DB.connect(tmp_path/'test.sqlite'); DB.init_db(c)
    n={'id':'general','summary':'An alcohol thermometer measures temperature.','title':'Science','content_hash':'x'}
    assert S.filter_retrieval(c,[n])==[n]
    c.close()

def test_absent_current_source_remains_historically_retrievable(tmp_path):
    c=DB.connect(tmp_path/'test.sqlite'); DB.init_db(c)
    n={'id':'old','content_hash':'x','summary':'Old doctrine.'}
    c.execute('INSERT INTO node_retrieval_quarantine(node_id,content_hash,reason,scope) VALUES (?,?,?,?)',('old','x','source-absent','current'))
    assert S.filter_retrieval(c,[n])==[]
    assert S.filter_retrieval(c,[n],historical=True)==[n]
    c.close()

def test_history_classification_and_unknown_health(tmp_path):
    assert is_archive_path('HQ/health/SESSION_BACKUPS/old.md')
    assert not is_archive_path('HQ/session_backups-guide.md')
    assert B._validity_tier({'path':'HQ/session_backups/old.md'},datetime.now(timezone.utc))==0
    c=DB.connect(tmp_path/'test.sqlite'); DB.init_db(c)
    DB.upsert_node(c,{'id':'x','type':'Source','family':'document','summary':'Normal knowledge'})
    h=S.health(c)
    assert h['unknown_review_total']==1 and h['unknown_is_fresh'] is False
    assert h['metadata_by_type']['Source']['missing_hash']==1
    c.close()

def test_existing_identifier_is_addressable_after_privacy_scope(tmp_path):
    c=DB.connect(tmp_path/'test.sqlite'); DB.init_db(c)
    DB.upsert_node(c,{'id':'mem_exact_historical_123','type':'Source','family':'document',
                     'path':'HQ/session_backups/old.md','title':'Old source','summary':'Historical policy.'})
    seeds=B._seed_nodes(c,'mem_exact_historical_123',historical=True)
    assert [n['id'] for n in seeds]==['mem_exact_historical_123']
    c.execute('INSERT INTO node_retrieval_quarantine(node_id,content_hash,reason,scope) VALUES (?,?,?,?)',
              ('mem_exact_historical_123','','privacy','all'))
    assert B._seed_nodes(c,'mem_exact_historical_123',historical=True)==[]
    c.close()

def test_database_guard_redacts_old_client_reads_and_denies_reharvest(tmp_path):
    c=DB.connect(tmp_path/'test.sqlite'); DB.init_db(c)
    n={'id':'private','type':'Source','family':'document','title':'Founder preference',
       'summary':'Synthetic Founder alcohol-history fixture.','body_excerpt':'Synthetic private test body.',
       'content_hash':'denied','path':'COVENANT/Boot/Founder.md','owner':'COVENANT'}
    DB.upsert_node(c,n)
    stored=dict(c.execute('SELECT * FROM nodes WHERE id=?',('private',)).fetchone())
    assert stored['title']=='[Privacy-excluded source]'
    assert stored['summary'] is None and stored['body_excerpt'] is None and stored['path'] is None
    assert c.execute('SELECT count(*) FROM fts_nodes WHERE node_id=?',('private',)).fetchone()[0]==0
    # An already-imported old writer can update nodes directly; SQLite still redacts.
    c.execute('UPDATE nodes SET title=?,summary=?,body_excerpt=?,path=? WHERE id=?',
              (n['title'],n['summary'],n['body_excerpt'],n['path'],'private'))
    assert c.execute('SELECT summary FROM nodes WHERE id=?',('private',)).fetchone()[0] is None
    c.execute('INSERT INTO nodes(id,type,family,title,summary,content_hash) VALUES (?,?,?,?,?,?)',
              ('alternate','Source','document',n['title'],n['summary'],'denied'))
    assert c.execute('SELECT summary FROM nodes WHERE id=?',('alternate',)).fetchone()[0] is None
    DB.upsert_embedding(c,'alternate',b'1234',1,'fixture')
    assert c.execute('SELECT count(*) FROM embeddings WHERE node_id=?',('alternate',)).fetchone()[0]==0
    DB.upsert_node(c,n)
    assert c.execute('SELECT count(*) FROM fts_nodes WHERE node_id=?',('private',)).fetchone()[0]==0
    clean={**n,'content_hash':'sanitized','summary':'Concise explanations.','body_excerpt':'Clean fixture.'}
    DB.upsert_node(c,clean)
    assert c.execute('SELECT summary FROM nodes WHERE id=?',('private',)).fetchone()[0]=='Concise explanations.'
    c.close()
