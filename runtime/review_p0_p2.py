#!/usr/bin/env python3
"""Independent risk reproductions against actual business functions; temporary data only."""
from pathlib import Path
import sys,tempfile,datetime as dt,json,hashlib,threading,http.server,urllib.request
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dsh_personal.server import Application
from dsh_personal.gateway import Gateway
from dsh_personal.worker import Worker
from dsh_personal.core import Store,UTC
root=Path(tempfile.mkdtemp(prefix='dsh-review-'));now=[dt.datetime(2026,10,22,12,tzinfo=UTC)]
app=Application(root/'app',lambda:now[0]);s=app.store;out={}
a=s.ensure_activity('A3','2026-10-21');r=s.submit(a['id'],'stable-message-1','睡得一般')
r2=s.submit(a['id'],'stable-message-1','睡得一般')
assert r2['duplicate'];assert len([x for x in s.activity(a['id'])['messages'] if x['role']=='user'])==1
out['durable_receipt_idempotency']='PASS'
snapshot=s.summary_snapshot(a['id']);s.correct(a['id'],{'quality':'不错'},'测试用户明确更正')
assert not s.commit_summary(snapshot,{'fields':{'quality':'旧值'}})
s.submit(a['id'],'stable-message-2','今天还是有点困');s.summarize_literal(a['id']);assert s.activity(a['id'])['summary']['fields']['quality']=='不错'
out['summary_version_and_persistent_correction']='PASS'
s.set_meta('daily_tokens','10');ids=[];errors=[]
def reserve():
 try:ids.append(s.reserve(6))
 except RuntimeError:errors.append('denied')
ts=[threading.Thread(target=reserve) for _ in range(2)]
for t in ts:t.start()
for t in ts:t.join()
assert len(ids)==1 and len(errors)==1;assert Store(s.path,lambda:now[0]).budget()['usage'][0]['reserved']==6
out['concurrent_budget_restart']='PASS'
class Bridge:
 def ensure_session(self,*a,**kw):return 'fixture-native-session'
 def reconcile(self,*a,**kw):return {'accepted':True}
 def submit(self,*a,**kw):return {'accepted':True}
 def events(self,*a,**kw):return []
w=Worker(s,Bridge(),app.records,lambda:{'model':{'name':'fixture'}})
# Original native handoff done but model turn failed: retry does not queue model processing.
with s.db(True) as c:
 c.execute("UPDATE jobs SET status='done'")
 c.execute("UPDATE messages SET processing_state='accepted_by_dsh' WHERE role='user'")
app.action({'op':'retry','activity_id':a['id']})
with s.db() as c:out['retry_native_failure_jobs']=c.execute("SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0]
# Deadline given by user is silently discarded.
t=s.create_task('固定样本整理',deadline=(now[0]-dt.timedelta(days=1)).isoformat(),catchup_policy='skip_missed')
out['task_deadline_after_create']={'deadline':t['deadline'],'policy':t['catchup_policy']}
# A running job never settles if DSH is unreachable even after five minutes.
r=s.claim_task(t['id'])
with s.db(True) as c:c.execute('UPDATE task_runs SET session_id=? WHERE id=?',('unreachable',r['run_id']))
class DeadBridge(Bridge):
 def events(self,*a,**kw):raise RuntimeError('fixture DSH unavailable')
w.bridge=DeadBridge();now[0]+=dt.timedelta(minutes=10);w.sync_all()
out['task_after_timeout_unreachable']=s.task(t['id'])['status']
# Verify outbound wire cap with an actual local HTTP server.
captured=[]
class Upstream(http.server.BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));captured.append(body)
  raw=json.dumps({'choices':[{'message':{'role':'assistant','content':'fixture'}}],'usage':{'total_tokens':8}}).encode()
  self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
u=http.server.ThreadingHTTPServer(('127.0.0.1',0),Upstream);threading.Thread(target=u.serve_forever,daemon=True).start()
(root/'key').write_text('fixture-key')
app.config_path.write_text(json.dumps({'model':{'base_url':f'http://127.0.0.1:{u.server_port}/v1','name':'fixture','api_key_file':str(root/'key')}}))
g=Gateway(s,app.config_path,'fixture-local').serve(0)
req=urllib.request.Request(f'http://127.0.0.1:{g.server_port}/v1/chat/completions',json.dumps({'model':'foreground-model','messages':[{'role':'user','content':'fixture'}],'max_tokens':5,'max_completion_tokens':20000}).encode(),{'Content-Type':'application/json','Authorization':'Bearer fixture-local'})
with urllib.request.urlopen(req) as response:response.read()
out['outbound_limits']={k:captured[-1].get(k) for k in ['max_tokens','max_completion_tokens']}
g.shutdown();u.shutdown()
out['reviewed_files']={p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in ['dsh_personal/core.py','dsh_personal/worker.py','dsh_personal/gateway.py','dsh_personal/server.py']}
print(json.dumps(out,ensure_ascii=False,indent=2))
