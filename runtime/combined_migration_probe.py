#!/usr/bin/env python3
"""One encrypted recovery point contains real native pending state and related app data.
All writes are isolated fixtures. Restored application remains isolated throughout.
"""
from pathlib import Path
import base64,hashlib,http.server,io,json,os,shutil,signal,sqlite3,subprocess,sys,tempfile,threading,time,zipfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime.probe import Fixture,free_port,wait_for
from runtime.dsh_bridge import DSHBridge
from dsh_personal.server import Application
from dsh_personal.worker import Worker
from dsh_personal.operations import backup,create_key,decrypt,restore_bundle,verify_bundle,_key
ROOT=Path(__file__).resolve().parents[1]
REPORT=ROOT/'evidence/P4/combined-migration.json'

class Host:
 def __init__(self,home,patch,log):self.home=home;self.patch=patch;self.log=log;self.child=None;self.port=free_port();self.handle=None
 def start(self):
  self.home.mkdir(parents=True,exist_ok=True);auth=self.home/'bridge-auth.json';auth.unlink(missing_ok=True)
  self.handle=self.log.open('a')
  self.child=subprocess.Popen([sys.executable,str(ROOT/'runtime/launch_dsh.py'),'--home',str(self.home),'--port',str(self.port),'--patch',str(self.patch)],env=dict(os.environ,DSH_GATEWAY_KEY='combined-isolated-fixture-key'),stdout=self.handle,stderr=self.handle)
  def ready():
   if self.child.poll() is not None:raise RuntimeError('isolated DSH exited: '+self.log.read_text()[-3000:])
   return auth.exists()
  wait_for(ready,45)
  bridge=DSHBridge(f'http://127.0.0.1:{self.port}',self.home)
  wait_for(lambda:bridge.rpc('session/list',{'_request':{}}) is not None,15)
  return bridge
 def stop(self):
  if self.child and self.child.poll() is None:
   self.child.send_signal(signal.SIGTERM)
   try:self.child.wait(15)
   except subprocess.TimeoutExpired:self.child.kill();self.child.wait()
  self.child=None
  if self.handle:self.handle.close();self.handle=None

def file_hashes(root):return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
def main():
 root=Path(tempfile.mkdtemp(prefix='dsh-combined-migration-'));os.chmod(root,0o700)
 source=root/'source';destination=root/'restored-empty';app=Application(source);hosts=[]
 Fixture.requests=[];Fixture.unavailable=False
 fixture=http.server.ThreadingHTTPServer(('127.0.0.1',0),Fixture);threading.Thread(target=fixture.serve_forever,daemon=True).start()
 patch=json.loads((ROOT/'runtime/personal.patch.json').read_text())
 next(p for p in patch if p.get('id')=='llm-pi-ai')['config']['providers']['dsh-personal']['baseURL']=f'http://127.0.0.1:{fixture.server_port}/v1'
 # Test-only first-search index proves native derivation can be rebuilt from restored logs.
 patch.append({'id':'session-query-sqlite','config':{'path':':memory:','openAt':'first-search'}})
 patch_path=root/'fixture.patch.json';patch_path.write_text(json.dumps(patch))
 result={'scope':'single_encrypted_bundle_real_DSH_and_business_fixture_restore_to_empty_directory_same_host','dsh_version':'0.2.1-rc.2','isolated_root':str(root),'started_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'checks':[]}
 def record(name,actual):result['checks'].append({'name':name,'status':'PASS','actual':actual});print(name+': PASS',flush=True)
 try:
  host=Host(source/'dsh',patch_path,root/'source-host.log');hosts.append(host);bridge=host.start()
  activity=app.store.ensure_activity('A3','2026-10-20');aid=activity['id']
  original='P0_SEED combinedmigrationtoken 固定测试原文：睡得一般，请提出睡眠问题。'
  receipt=app.action({'op':'message','activity_id':aid,'submission_id':'combined-stable-original','body':original})
  worker=Worker(app.store,bridge,app.records,app.config);assert worker.work_one()
  sid=app.store.activity(aid)['activity']['session_id']
  pending=wait_for(lambda:[q for q in bridge.pending_questions(sid) if q['state']=='continued'])
  wait_for(lambda:any(e['type']=='turn/end' and e['data'].get('reason',{}).get('kind')=='completed' for e in bridge.events(sid)))
  worker.sync_all();app.store.summarize_literal(aid);app.store.correct(aid,{'quality':'测试更正：睡得较好'},'固定样本用户明确更正，并非真实用户评价')
  worker.sync_summary_refs(aid)
  source_view=app.store.activity(aid)
  assert source_view['questions'] and source_view['summary']['fields']['quality']=='测试更正：睡得较好'
  record('source_real_native_pending_and_business_receipt',{'activity_id':aid,'session_id':sid,'submission_id':'combined-stable-original','receipt':receipt['receipt'],'call_id':pending[0]['callId'],'business_date':source_view['activity']['business_date'],'summary_correction_version':source_view['summary']['correction_version']})
  imported={}
  chatgpt=json.dumps([{'id':'combined-chatgpt','title':'固定导入样本','mapping':{'u':{'parent':None,'message':{'id':'combined-cg-u','author':{'role':'user'},'create_time':1792600000,'content':{'content_type':'text','parts':['固定ChatGPT来源样本']}}}}}],ensure_ascii=False)
  codex='\n'.join(json.dumps(x,ensure_ascii=False) for x in [{'timestamp':'2026-10-20T00:00:00Z','type':'session_meta','payload':{'id':'combined-codex'}},{'timestamp':'2026-10-20T00:00:01Z','type':'response_item','payload':{'type':'message','id':'combined-cx-u','role':'user','content':[{'type':'input_text','text':'固定Codex来源样本'}]}}])
  kimi=json.dumps({'id':'combined-kimi','messages':[{'id':'combined-kimi-u','role':'user','content':'固定Kimi来源样本','created_at':'2026-10-20T10:20:00+08:00'}]},ensure_ascii=False)
  for channel,content,name in [('chatgpt',chatgpt,'conversations.json'),('codex',codex,'rollout.jsonl'),('kimi',kimi,'kimi.json')]:
   imported[channel]=app.records.import_apply(channel,content,name)
   assert imported[channel]['counts']['new']>=1
  import_rows=app.records.imported_messages(include_quarantined=True)
  profile=app.records.add_profile('固定样本偏好：保留原文来源','preference',source_refs=['message:'+receipt['receipt'],'import:'+import_rows[0]['message_id']],confirmed=True,user_input='固定测试用户确认，仅用于恢复路径')
  growth_ids={}
  for line in ['doctorate','startup','culture']:
   goal=app.records.put_growth(line,'goal',{'target':'固定'+line+'目标'},source_refs=['activity:'+aid,'message:'+receipt['receipt']],business_date='2026-10-20')
   plan=app.records.put_growth(line,'plan',{'next_action':'固定样本计划'},parent_id=goal['item_id'],source_refs=[goal['item_id']],business_date='2026-10-20')
   action=app.records.put_growth(line,'action',{'status':'fixture-done'},parent_id=goal['item_id'],source_refs=[plan['item_id']],business_date='2026-10-20')
   outcome=app.records.put_growth(line,'outcome',{'artifact':'固定样本文本'},parent_id=goal['item_id'],source_refs=[action['item_id'],'activity:'+aid],business_date='2026-10-20')
   growth_ids[line]=[goal['item_id'],plan['item_id'],action['item_id'],outcome['item_id']]
  attachment_bytes=b'COMBINED_MIGRATION_FIXTURE_ATTACHMENT\n'
  attachment=app.action({'op':'attachment_upload','activity_id':aid,'filename':'fixture.txt','content_base64':base64.b64encode(attachment_bytes).decode()})
  task=app.store.create_task('固定组合恢复任务',inputs='固定样本；活动来源 '+aid,source_activity_id=aid)
  run=app.store.claim_task(task['id']);task_sid=bridge.ensure_session('combined-task:'+run['run_id'],source/'workspace')
  with app.store.db(True) as db:db.execute('UPDATE task_runs SET session_id=? WHERE id=?',(task_sid,run['run_id']))
  bridge.submit(task_sid,run['run_id'],'交付固定样本文本，仅验证恢复路径。',kind='delegated')
  wait_for(lambda:any(e['type']=='turn/end' and e['data'].get('reason',{}).get('kind')=='completed' for e in bridge.events(task_sid)))
  worker.sync_all();task_before=app.store.task(task['id']);assert task_before['status']=='awaiting_acceptance'
  assert task_before['deliveries'][0]['body']
  expected_growth={item['item_id']:item for item in app.records.growth(include_history=True)['items']}
  expected_profile=app.records.get_profile(profile['item_id']);expected_activity_ref=next(r for r in app.records.activity_evidence() if r['activity_id']==aid)
  record('source_linked_entities_and_actual_task_delivery',{'import_channels':sorted(imported),'import_messages':len(import_rows),'profile_id':profile['item_id'],'growth_lines':growth_ids,'growth_count':len(expected_growth),'task_id':task['id'],'task_native_session':task_sid,'task_version':task_before['version'],'task_status':task_before['status'],'attachment_id':attachment['id'],'attachment_sha256':hashlib.sha256(attachment_bytes).hexdigest()})
  source_search=bridge.rpc('session/search',{'request':{'query':'combinedmigrationtoken'}})
  assert any(r['sessionId']==sid for r in source_search['items'])
  host.stop()
  key_path=root/'keys'/'backup.key';create_key(key_path)
  backup_report=backup(app,key_path,destination=root/'encrypted-backups')
  encrypted=Path(backup_report['path']).read_bytes();payload=decrypt(encrypted,_key(key_path));manifest=verify_bundle(payload)
  ledger=json.loads(decrypt(Path(backup_report['ledger']['path']).read_bytes(),_key(key_path)))
  with zipfile.ZipFile(io.BytesIO(payload)) as archive:
   names=archive.namelist();assert 'dsh/bridge-auth.json' not in names
   assert any(name.startswith('dsh/sessions/') and name.endswith('.jsonl.zstd') for name in names)
   assert any(name.startswith('attachments/') for name in names)
   for private in [app.access_token.encode(),app.gateway_key.encode()]:assert not any(private in archive.read(name) for name in names)
  source_baseline=file_hashes(source)
  record('single_encrypted_consistent_bundle_contains_all_layers',{'encryption':backup_report['encrypted'],'bytes':backup_report['bytes'],'bundle_files':len(manifest['files']),'native_generations':len([n for n in names if n.startswith('dsh/sessions/') and n.endswith('.jsonl.zstd')]),'credentials_excluded':True,'host_stopped_before_snapshot':True,'backup_sha256':backup_report['sha256']})
  assert not destination.exists()
  restored_report=restore_bundle(payload,destination,latest_ledger=ledger)
  restored=Application(destination)
  assert restored.store.meta('isolated')=='true' and restored_report['isolated']
  assert not restored.config()['model'] and not restored.config()['notification']
  # Explicit diagnostic rebuilds in fixture copy, without releasing isolation or running workers.
  with restored.store.db(True) as db:
   indexes=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")]
   db.execute('REINDEX');integrity=db.execute('PRAGMA integrity_check').fetchone()[0]
   foreign=list(db.execute('PRAGMA foreign_key_check'))
  assert integrity=='ok' and not foreign
  restored.store.summarize_literal(aid)
  actual=restored.store.activity(aid)
  kept=next(m for m in actual['messages'] if m['id']==receipt['receipt'])
  assert kept['body']==original and kept['submission_id']=='combined-stable-original'
  assert actual['activity']['session_id']==sid and actual['activity']['business_date']=='2026-10-20'
  assert actual['summary']['fields']['quality']=='测试更正：睡得较好'
  assert actual['corrections']==source_view['corrections']
  assert restored.records.get_profile(profile['item_id'])==expected_profile
  for ident,expected in expected_growth.items():assert restored.records.get_growth(ident)==expected
  actual_refs=next(r for r in restored.records.activity_evidence() if r['activity_id']==aid)
  assert actual_refs==expected_activity_ref
  restored_imports=restored.records.imported_messages(include_quarantined=True)
  assert {m['message_id'] for m in restored_imports}=={m['message_id'] for m in import_rows}
  assert all(m['quarantined'] for m in restored_imports)
  assert restored.records.context()==[]
  task_after=restored.store.task(task['id'])
  assert task_after['source_activity_id']==aid and task_after['version']==task_before['version']
  assert task_after['deliveries'][0]['body']==task_before['deliveries'][0]['body']
  assert task_after['deliveries'][0]['session_id']==task_sid
  assert task_after['status']=='needs_input' and not task_after['reviews']
  with restored.store.db() as db:
   asset=dict(db.execute('SELECT * FROM attachments WHERE id=?',(attachment['id'],)).fetchone())
   auto_jobs=db.execute("SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0]
   auto_notifications=db.execute("SELECT COUNT(*) FROM notifications WHERE state IN ('pending','sending')").fetchone()[0]
  assert asset['activity_id']==aid and (destination/asset['path']).read_bytes()==attachment_bytes
  assert not auto_jobs and not auto_notifications
  record('restored_business_relationships_and_index_rebuild',{'activity_session_same':True,'original_receipt_same':True,'business_date_same':True,'summary_correction_preserved_after_rebuild':True,'profile_and_source_refs_equal':True,'growth_items_equal':len(expected_growth),'growth_parent_and_source_refs_equal':True,'import_channels':sorted({m['channel'] for m in restored_imports}),'all_imports_quarantined':True,'task_body_version_and_native_session_equal':True,'task_status':task_after['status'],'attachment_verified':True,'sql_reindex_count':len(indexes),'integrity':integrity,'foreign_key_violations':len(foreign),'automatic_jobs':auto_jobs,'automatic_notifications':auto_notifications})
  # Rebuild native projections from logs rather than trusting copied cache JSON.
  cache=destination/'dsh/storages/session_projcache';removed_caches=len(list(cache.rglob('*.json'))) if cache.exists() else 0
  if cache.exists():shutil.rmtree(cache)
  restored_host=Host(destination/'dsh',patch_path,root/'restored-host.log');hosts.append(restored_host);new_bridge=restored_host.start()
  before_model_calls=len(Fixture.requests)
  native_pending=new_bridge.pending_questions(sid)
  assert native_pending and native_pending[0]['callId']==pending[0]['callId']
  assert native_pending[0]['questions']==pending[0]['questions']
  assert native_pending[0]['state']=='continued'
  restored_search=new_bridge.rpc('session/search',{'request':{'query':'combinedmigrationtoken'}})
  assert any(r['sessionId']==sid for r in restored_search['items'])
  assert len(Fixture.requests)==before_model_calls
  record('native_pending_and_search_rebuilt_from_same_bundle',{'session_id':sid,'call_id':native_pending[0]['callId'],'questions_equal':True,'pending_state':'continued','discarded_derived_projection_cache_files':removed_caches,'fresh_in_memory_search_index_matches_original_session':True,'read_and_rebuild_model_calls':0})
  before_assistants=sum(e['type']=='assistant/message' for e in new_bridge.events(sid))
  answer=new_bridge.answer_pending(sid,native_pending[0]['callId'],[{'id':'sleep','selected':[],'custom':'固定恢复验证晚答：睡得一般。'}])
  assert answer['accepted']
  wait_for(lambda:not new_bridge.pending_questions(sid))
  wait_for(lambda:sum(e['type']=='assistant/message' for e in new_bridge.events(sid))>before_assistants)
  reply_events=[e for e in new_bridge.events(sid) if e['type']=='user/message' and e['data'].get('source',{}).get('kind')=='user-question-reply']
  assert len(reply_events)==1
  assert restored.store.meta('isolated')=='true' and restored.records.context()==[]
  restored_host.stop()
  source_after=file_hashes(source);assert source_after==source_baseline
  record('restored_same_session_late_answer_and_source_unchanged',{'accepted':answer['accepted'],'session_id':sid,'native_reply_events':len(reply_events),'business_isolated':True,'source_files_unchanged':len(source_baseline),'source_hash_manifest_equal':True,'note':'Explicit fixture-only native answer; restored app workers and notifications never started.'})
  result['status']='PASS';result['restore_report']=restored_report
 except Exception as exc:
  result['status']='FAIL';result['error']={'type':type(exc).__name__,'message':str(exc)};print(type(exc).__name__+': '+str(exc),flush=True)
 finally:
  for host in hosts:host.stop()
  fixture.shutdown();result['model_fixture_requests']=Fixture.requests;result['finished_at']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
  REPORT.parent.mkdir(parents=True,exist_ok=True);REPORT.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
 return 0 if result['status']=='PASS' else 1
if __name__=='__main__':sys.exit(main())
