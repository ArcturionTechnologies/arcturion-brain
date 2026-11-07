import plistlib
from engine.extractors.plist import extract_plist

def test_valid_array_metadata_does_not_declare_a_service(tmp_path):
 p=tmp_path/'BuildVersionHistory.plist';p.write_bytes(plistlib.dumps([{'version':'owned fixture'}]))
 node,edges=extract_plist(p)
 assert node['type']=='Config' and node['status']=='reference'
 assert node['body_excerpt'] is None and node['metadata']['entry_count']==1
 assert edges==[] and 'extract_error' not in node['metadata']

def test_malformed_template_remains_a_visible_source_error(tmp_path):
 p=tmp_path/'example.plist';p.write_text('<plist><unclosed>')
 node,edges=extract_plist(p)
 assert node['status']=='error' and node['metadata']['extract_error'].startswith('parse:')
 assert edges==[]

def test_launchd_declaration_retains_configured_semantics(tmp_path):
 p=tmp_path/'job.plist';p.write_bytes(plistlib.dumps({'Label':'owned.fixture','ProgramArguments':['/bin/echo','fixture']}))
 node,edges=extract_plist(p)
 assert node['type']=='Automation' and node['status']=='configured'
 assert any(e['predicate']=='configures' for e in edges)
