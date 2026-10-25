#!/usr/bin/env python3
"""Targeted independent regression checks for round-one findings, isolated only."""
from pathlib import Path
import sys,tempfile,datetime as dt,json,hashlib,threading,http.server,urllib.request
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dsh_personal.server import Application
from dsh_personal.gateway import Gateway
from dsh_personal.worker import Worker
from dsh_personal.core import UTC
root=Path(tempfile.mkdtemp(prefix='dsh-review-round2-'));now=[dt.datetime(2026,10,22,12,tzinfo=UTC)]
out={'kind':'isolated_business_regressions_with_fixture_bridges','root':str(root),'checks':[]}
class Bridge:
 def __init__(self):self.submissions=[];self.resumptions=[];self.cancels=[];self.event_rows=[]
 def ensure_session(self,*a,**kw):return 'fixture-native-session'
 def reconcile(self,*a,**kw):return {'accepted':True}
 def submit(self,*a,**kw):self.submissions.append([a,kw]);return {'accepted':True}
 def resume_processing(self,*a,**kw):self.resumptions.append([a,kw]);return {'accepted':True}
 def cancel_processing(self,*a,**kw):self.cancels.append(a);return {'accepted':True}
 def pending_questions(self,*a,**kw):return []
 def events(self,*a,**kw):return self.event_rows

def check(name,passed,actual):out['checks'].append({'name':name,'status':'PASS' if passed else 'FAIL','actual':actual})
def setup(name):
 app=Application(root/name,lambda:now[0]);bridge=Bridge()
 return app,bridge,Worker(app.store,bridge,app.records,lambda:{'model':{'name':'fixture'}})

app,b,w=setup('retry');s=app.store;a=s.ensure_activity('A3','2026-10-21');receipt=s.submit(a['id'],'stable-message-1','睡得一般')
with s.db(True) as c:
 c.execute("UPDATE jobs SET status='done'")
 c.execute("UPDATE messages SET processing_state='accepted_by_dsh' WHERE role='user'")
 c.execute("UPDATE activities SET session_id='fixture-native-session',session_state='ready' WHERE id=?",(a['id'],))
app.action({'op':'retry','activity_id':a['id']});w.work_one()
with s.db() as c:native_jobs=[dict(r) for r in c.execute("SELECT kind,status FROM jobs WHERE kind='native_retry'")]
check('native_failure_retry_schedules_control',len(b.resumptions)==1 and len(b.submissions)==0,{'resume_calls':b.resumptions,'prompt_calls':b.submissions,'jobs':native_jobs})

app,b,w=setup('deadline');s=app.store;deadline=(now[0]-dt.timedelta(days=1)).isoformat()
t=s.create_task('固定样本过期整理',deadline=deadline,catchup_policy='skip_missed');w.dispatch_tasks();t=s.task(t['id'])
check('deadline_policy_persisted_and_enforced',t['deadline']==deadline and t['catchup_policy']=='skip_missed' and t['status']=='needs_input' and not b.submissions,{'deadline':t['deadline'],'policy':t['catchup_policy'],'state':t['status'],'sends':len(b.submissions)})

app,b,w=setup('timeout');s=app.store;t=s.create_task('不可达执行');r=s.claim_task(t['id'])
with s.db(True) as c:c.execute("UPDATE task_runs SET session_id='unreachable' WHERE id=?",(r['run_id'],))
class DeadBridge(Bridge):
 def events(self,*a,**kw):raise RuntimeError('fixture unavailable')
 def cancel_processing(self,*a,**kw):self.cancels.append(a);raise RuntimeError('fixture unavailable')
b=DeadBridge();w.bridge=b;now[0]+=dt.timedelta(minutes=10);w.sync_all();t=s.task(t['id'])
check('unreachable_timeout_is_bounded_and_cancel_unknown',t['status']=='needs_input' and t['deliveries'][0]['status']=='failed' and len(b.cancels)==1 and '未知' in t['error'],{'state':t['status'],'run_state':t['deliveries'][0]['status'],'error':t['error'],'cancel_calls':len(b.cancels)})

app,b,w=setup('delivery');s=app.store;t=s.create_task('完整成果');r=s.claim_task(t['id'])
with s.db(True) as c:c.execute("UPDATE task_runs SET session_id='native-delivery' WHERE id=?",(r['run_id'],))
b.event_rows=[{'type':'assistant/message','seq':1,'data':{'content':[{'type':'text','text':'开始整理'}]}}]
w.sync_all();incomplete=s.task(t['id'])['status']
b.event_rows+=[{'type':'turn/end','seq':2,'data':{'reason':{'kind':'completed'}}}]
w.sync_all();completed=s.task(t['id'])['status']
check('incomplete_turn_cannot_deliver',incomplete=='running',{'without_turn_end':incomplete,'completed_unstructured_text':completed})
check('non_artifact_text_cannot_deliver',completed!='awaiting_acceptance',{'completed_unstructured_text':completed})

app,b,w=setup('natural');s=app.store
body='补记 2026-10-20 睡眠：一般；补记 2026-10-19 睡眠：不错'
r1=app.action({'op':'natural','submission_id':'request-natural','body':body});r2=app.action({'op':'natural','submission_id':'request-natural','body':body})
with s.db() as c:before=c.execute("SELECT COUNT(*) FROM messages WHERE role='user'").fetchone()[0]
clarification=app.action({'op':'natural','submission_id':'request-ambiguous','body':'补记 2026-10-18 睡眠：挺好；前几天也一般'})
with s.db() as c:after=c.execute("SELECT COUNT(*) FROM messages WHERE role='user'").fetchone()[0]
check('natural_same_request_dedup_and_parse_before_write',before==2 and after==2,{'user_rows_after_retry':before,'after_ambiguous_request':after,'clarification':clarification})
now[0]+=dt.timedelta(days=1)
app.action({'op':'natural','submission_id':'request-natural','body':body})
with s.db() as c:midnight=c.execute("SELECT COUNT(*) FROM messages WHERE role='user'").fetchone()[0]
check('natural_request_id_survives_midnight',midnight==2,{'user_rows_before':before,'user_rows_after':midnight})
error=None
try:app.action({'op':'natural','submission_id':'request-same-date','body':'补记 2026-10-17 睡眠：入睡好；补记 2026-10-17 睡眠：早上醒得早'})
except Exception as exc:error=str(exc)
with s.db() as c:sameday=c.execute("SELECT COUNT(*) FROM messages m JOIN activities a ON m.activity_id=a.id WHERE a.business_date='2026-10-17' AND m.role='user'").fetchone()[0]
check('same_date_segments_have_distinct_stable_ids',error is None and sameday==2,{'rows':sameday,'error':error})

app,b,w=setup('limits');s=app.store;captured=[]
class Upstream(http.server.BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));captured.append(body)
  raw=json.dumps({'choices':[{'message':{'role':'assistant','content':'fixture'}}],'usage':{'total_tokens':8}}).encode()
  self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
u=http.server.ThreadingHTTPServer(('127.0.0.1',0),Upstream);threading.Thread(target=u.serve_forever,daemon=True).start()
(root/'key').write_text('fixture-key');app.config_path.write_text(json.dumps({'model':{'base_url':f'http://127.0.0.1:{u.server_port}/v1','name':'fixture','api_key_file':str(root/'key')}}))
g=Gateway(s,app.config_path,'fixture-local').serve(0)
req=urllib.request.Request(f'http://127.0.0.1:{g.server_port}/v1/chat/completions',json.dumps({'model':'foreground-model','messages':[{'role':'user','content':'fixture'}],'max_tokens':5,'max_completion_tokens':20000}).encode(),{'Content-Type':'application/json','Authorization':'Bearer fixture-local'})
try:
 with urllib.request.urlopen(req) as response:response.read()
 with s.db() as c:reservation=dict(c.execute('SELECT reserved,actual FROM usage').fetchone())
 cap={k:captured[-1].get(k) for k in ['max_tokens','max_completion_tokens']}
 check('outbound_single_effective_limit_reserved',cap['max_tokens'] is None and cap['max_completion_tokens']==8192 and reservation['reserved']>=8192,{'outbound':cap,'reservation':reservation})
finally:g.shutdown();u.shutdown()
out['reviewed_files']={p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in ['dsh_personal/core.py','dsh_personal/worker.py','dsh_personal/gateway.py','dsh_personal/server.py','dsh_personal/interaction.py']}
out['status']='PASS' if all(x['status']=='PASS' for x in out['checks']) else 'FAIL'
print(json.dumps(out,ensure_ascii=False,indent=2))
