#!/usr/bin/env python3
"""Actual 0.1.5-rc.1 -> 0.2.1-rc.2 isolated native data compatibility probe.
Installed packages are read-only. Every writable path is a fresh /tmp directory.
"""
from pathlib import Path
import argparse,hashlib,http.server,json,os,re,shutil,signal,subprocess,sys,tempfile,threading,time,uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime.dsh_bridge import DSHBridge,DSHError
from runtime.probe import Fixture,free_port,wait_for
from runtime.node_runtime import NodeRuntimeError,node_version,resolve_node
ROOT=Path(__file__).resolve().parents[1]
NEW=ROOT/'runtime/npm'
REPORT=ROOT/'evidence/P4/native-version-check.json'

class OldBridge(DSHBridge):
 def projections(self,sid):
  rows=self.rpc('session/list',{'_request':{}})
  if isinstance(rows,dict):rows=rows.get('items',[])
  entry=next((r for r in rows if r['sessionId']==sid),None)
  return None if entry is None else dict(kind='sequenced',**entry.get('projections',{}))

def hashes(root):return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
def main():
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--old-runtime',default=os.environ.get('DSH_OLD_RUNTIME'),help='Old npm installation root (or DSH_OLD_RUNTIME). Read only.')
 args=parser.parse_args()
 if not args.old_runtime:parser.error('Supply --old-runtime or DSH_OLD_RUNTIME; no existing installation is selected implicitly.')
 OLD=Path(args.old_runtime).expanduser().resolve()
 for app in (OLD,NEW):
  if not (app/'node_modules/@deepseek-ai/dsh/package.json').is_file() or not (app/'package-lock.json').is_file():parser.error('Each runtime must contain package-lock.json and node_modules/@deepseek-ai/dsh/package.json.')
 try:NODE=str(resolve_node(ROOT));version=node_version(Path(NODE))
 except NodeRuntimeError as exc:parser.exit(1,str(exc)+'\n')
 root=Path(tempfile.mkdtemp(prefix='dsh-native-version-'));os.chmod(root,0o700)
 work=root/'workspace';work.mkdir();port=free_port();process=None;reader=None
 old_home=root/'old-original';old_home.mkdir();new_home=root/'new-upgrade-copy';rollback_home=root/'old-rollback-copy'
 Fixture.requests=[];Fixture.unavailable=False
 fixture=http.server.ThreadingHTTPServer(('127.0.0.1',0),Fixture);threading.Thread(target=fixture.serve_forever,daemon=True).start()
 patch=json.loads((ROOT/'runtime/personal.patch.json').read_text())
 next(r for r in patch if r.get('id')=='llm-pi-ai')['config']['providers']['dsh-personal']['baseURL']=f'http://127.0.0.1:{fixture.server_port}/v1'
 (root/'new.patch.json').write_text(json.dumps(patch))
 old_patch=[r for r in patch if 'insert' not in r and r.get('id')!='llm-deepseek-account']
 (root/'old.patch.json').write_text(json.dumps(old_patch))
 preset=old_home/'.agent-presets/personal';preset.mkdir(parents=True)
 (preset/'preset.yml').write_text('name: P4 isolated fixture\ndescription: Text and question only\norder: 0\n')
 (preset/'agent.cordis.yml').write_text(json.dumps([{'id':'persona','name':'@deepseek-ai/dsh-persona','config':{'prefix':'You are an isolated test fixture. Reply with fixture text only.'}},{'id':'tool-ask-user','name':'@deepseek-ai/dsh-tool-ask-user'}]))
 result={'scope':'actual_native_version_pair_on_same_host_with_fixture_model','old_version':json.loads((OLD/'node_modules/@deepseek-ai/dsh/package.json').read_text())['version'],'new_version':json.loads((NEW/'node_modules/@deepseek-ai/dsh/package.json').read_text())['version'],'node':version,'isolated_root':str(root),'started_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'checks':[],'install_paths_read_only':[str(OLD),str(NEW)]}
 result['installed_manifest_hashes']={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [OLD/'package-lock.json',NEW/'package-lock.json']}
 def record(name,status,details):
  result['checks'].append({'name':name,'status':status,'actual':details});print(name+': '+status,flush=True)
 def stop():
  nonlocal process,reader
  if process is not None and process.poll() is None:
   process.send_signal(signal.SIGTERM)
   try:process.wait(12)
   except subprocess.TimeoutExpired:process.kill();process.wait()
  if reader:reader.join(timeout=2)
  process=None;reader=None
 def start(app,home,which):
  nonlocal process,reader
  home.mkdir(parents=True,exist_ok=True);auth=home/'bridge-auth.json'
  if auth.exists():auth.unlink()
  env=dict(os.environ,DSH_HOME=str(home),DSH_AGENTS_HOME=str(home/'agents'),DSH_TELEMETRY_DISABLED='1',NODE_NO_WARNINGS='1',DSH_GATEWAY_KEY='isolated-fixture-not-a-secret')
  command=[NODE,str(app/'node_modules/@deepseek-ai/dsh/lib/bin.js'),'--profile','web','--patch',str(root/('old.patch.json' if app==OLD else 'new.patch.json')),'--host','127.0.0.1','--port',str(port),'--no-open']
  process=subprocess.Popen(command,cwd=work,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
  child=process
  def capture():
   with (root/(which+'.log')).open('w') as log:
    for line in child.stdout:
     match=re.search(r'dsh web: (http://[^\s]+)',line)
     if match and '?token=' in match[1]:
      auth.write_text(json.dumps({'launch_url':match[1],'pid':child.pid}));os.chmod(auth,0o600)
     log.write(re.sub(r'([?&]token=)[^\s)]+',r'\1<redacted>',line));log.flush()
  reader=threading.Thread(target=capture,daemon=True);reader.start()
  started=time.monotonic()
  while not auth.exists():
   if process.poll() is not None:raise RuntimeError(f'{which} exited {process.returncode}: '+(root/(which+'.log')).read_text()[-5000:])
   if time.monotonic()-started>45:raise TimeoutError(which+' did not start')
   time.sleep(.1)
  b=(OldBridge if app==OLD else DSHBridge)(f'http://127.0.0.1:{port}',home)
  wait_for(lambda:b.rpc('session/list',{'_request':{}}) is not None,10)
  return b
 def create(b,sid):
  created=b.rpc('session/create',{'request':{'sessionId':sid,'cwd':str(work),'agentPreset':'personal'}})
  assert created['sessionId']==sid
  return created
 def send(b,sid,rid,text):
  b.rpc('session/selectModel',{'request':{'sessionId':sid,'provider':'dsh-personal','model':'configured-model'}})
  return b.rpc('session/prompt',{'request':{'sessionId':sid,'requestId':rid,'mode':'queue','clientTimeZone':'Asia/Shanghai','content':[{'type':'text','text':text}]}})
 def wait_events(b,sid,predicate):
  def ready():
   events=b.events(sid)
   return events if predicate(events) else None
  return wait_for(ready,30)
 sid=str(uuid.uuid4());question_sid=str(uuid.uuid4());old_backup_hashes=None
 try:
  b=start(OLD,old_home,'old-create');record('old_isolated_host_started','PASS',{'version':result['old_version'],'home':str(old_home)})
  create(b,sid);receipt=send(b,sid,'p4-original','P4_TEXT 旧版本原文固定样本。')
  old_events=wait_events(b,sid,lambda events:any(e['type']=='assistant/message' for e in events))
  record('old_session_real_model_fixture_and_original_saved','PASS',{'receipt':receipt,'session_id':sid,'event_count':len(old_events),'projection_keys':list(b.projections(sid).get('values',{}))})
  create(b,question_sid);send(b,question_sid,'p4-old-question','P0_SEED 旧版本固定待答问题。')
  question_events=wait_events(b,question_sid,lambda events:any(e['type']=='tool/call' for e in events))
  question_projection=b.projections(question_sid)
  record('old_question_tool_called','PASS',{'session_id':question_sid,'tool_call_count':sum(e['type']=='tool/call' for e in question_events),'projection_keys':list(question_projection.get('values',{}))})
  record('old_timed_durable_question_contract','NOT_SUPPORTED',{'reason':'0.1.5-rc.1 tool-ask-user calls userQuestions.ask({questions,agent,signal}) and has no timed mode or durable userQuestions projection. Cannot claim pending-question migration from an absent old contract.','old_projection_has_userQuestions':'userQuestions' in question_projection.get('values',{})})
  stop();old_backup_hashes=hashes(old_home);shutil.copytree(old_home,new_home,symlinks=True);shutil.copytree(old_home,rollback_home,symlinks=True)
  record('stopped_old_data_copied_for_upgrade_and_rollback','PASS',{'source_files':len(old_backup_hashes),'upgrade_home':str(new_home),'rollback_home':str(rollback_home)})
  try:
   b=start(NEW,new_home,'new-upgrade');record('new_version_starts_with_copied_old_data','PASS',{'home':str(new_home),'version':result['new_version']})
   state=b.projections(sid);result['upgraded_projection_before_write_open']=state
   if state.get('kind')=='migration-required':
    b.rpc('session/selectModel',{'request':{'sessionId':sid,'provider':'dsh-personal','model':'configured-model'}})
    state=b.projections(sid)
    record('public_session_activation_migrates_supported_history','PASS',{'entry':'session/selectModel resolves existing Agent via native persistence write-open','result_kind':state.get('kind')})
   result['upgraded_projection']=state
   upgraded_events=b.events(sid)
   assert any(e['type']=='user/message' and e['data'].get('source',{}).get('rpcId')=='p4-original' for e in upgraded_events)
   assert any(e['type']=='assistant/message' for e in upgraded_events)
   record('new_version_reads_old_transcript','PASS',{'session_id':sid,'event_count':len(upgraded_events),'projection_kind':state.get('kind')})
   if b.projections(question_sid).get('kind')=='migration-required':
    b.rpc('session/selectModel',{'request':{'sessionId':question_sid,'provider':'dsh-personal','model':'configured-model'}})
   new_pending=b.pending_questions(question_sid)
   record('old_question_pending_projection_after_upgrade','NOT_SUPPORTED' if not new_pending else 'PASS',{'active_count':len(new_pending),'note':'Old blocking question is not a new timed pending question; do not synthesize a user answer.'})
   before=sum(e['type']=='assistant/message' for e in b.events(question_sid))
   late=send(b,question_sid,'p4-upgraded-late-answer','对升级前的睡眠问题补答：睡得一般。这是固定测试输入。')
   late_events=wait_events(b,question_sid,lambda events:sum(e['type']=='assistant/message' for e in events)>before)
   record('new_version_late_freeform_answer_in_old_session','PASS',{'session_id':question_sid,'receipt':late,'user_message_count':sum(e['type']=='user/message' for e in late_events),'targeted_old_pending_not_available':True})
  except Exception as exc:
   record('upgrade_path','FAIL',{'type':type(exc).__name__,'error':str(exc)[:5000]})
  finally:stop()
  unchanged=hashes(old_home)==old_backup_hashes
  record('upgrade_does_not_mutate_preserved_old_data','PASS' if unchanged else 'FAIL',{'unchanged':unchanged})
  b=start(OLD,rollback_home,'old-rollback');rolled=b.events(sid)
  assert any(e['type']=='user/message' and e['data'].get('source',{}).get('rpcId')=='p4-original' for e in rolled)
  assert not any(e['type']=='user/message' and e['data'].get('source',{}).get('rpcId')=='p4-upgraded-late-answer' for e in b.events(question_sid))
  before=sum(e['type']=='assistant/message' for e in rolled)
  send(b,sid,'p4-rollback-followup','P4_TEXT 回滚程序与回滚旧数据配对后的继续输入。')
  wait_for(lambda:sum(e['type']=='assistant/message' for e in b.events(sid))>before,30)
  record('old_program_plus_preserved_old_data_pair_rollback','PASS',{'old_version':result['old_version'],'rollback_home':str(rollback_home),'original_request_present':True,'new_version_only_input_absent':True,'followup_model_fixture_completed':True})
 except Exception as exc:record('probe_execution','FAIL',{'type':type(exc).__name__,'error':str(exc)[:5000]})
 finally:
  stop();fixture.shutdown()
  result['native_generations']={label:[str(p.relative_to(home)) for p in home.rglob('session.v*.jsonl*') if p.is_file()] for label,home in [('old',old_home),('upgraded',new_home),('rollback',rollback_home)]}
  predecessors=[p for p in old_home.rglob('session.v3.jsonl*') if p.is_file()]
  result['upgraded_copy_preserves_v3_bytes']=bool(predecessors) and all((new_home/p.relative_to(old_home)).exists() and hashlib.sha256(p.read_bytes()).digest()==hashlib.sha256((new_home/p.relative_to(old_home)).read_bytes()).digest() for p in predecessors)
  result['installed_manifests_unchanged']=all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in result['installed_manifest_hashes'].items())
  result['requests']=Fixture.requests;result['finished_at']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
  result['status']='FAIL' if any(c['status']=='FAIL' for c in result['checks']) else 'PASS_WITH_DOCUMENTED_LIMITATIONS'
  REPORT.parent.mkdir(parents=True,exist_ok=True);REPORT.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
 return 0 if result['status']!='FAIL' else 1
if __name__=='__main__':sys.exit(main())
