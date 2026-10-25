#!/usr/bin/env python3
"""Final targeted review; only remaining/modified risky paths, isolated fixture data."""
from pathlib import Path
import sys,tempfile,datetime as dt,json,hashlib
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
files=['dsh_personal/core.py','dsh_personal/worker.py','dsh_personal/server.py','dsh_personal/interaction.py']
source_hashes={p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in files}
from dsh_personal.server import Application
from dsh_personal.worker import Worker
from dsh_personal.core import UTC
root=Path(tempfile.mkdtemp(prefix='dsh-review-round3-'));now=[dt.datetime(2026,10,22,12,tzinfo=UTC)]
out={'kind':'final_targeted_business_regressions','root':str(root),'reviewed_files':source_hashes,'checks':[]}
def check(name,passed,actual):out['checks'].append({'name':name,'status':'PASS' if passed else 'FAIL','actual':actual})
class Bridge:
 def __init__(self):self.event_rows=[];self.questions=[];self.answers=[];self.resumptions=[];self.on_events=None
 def events(self,*a,**kw):
  if self.on_events:self.on_events();self.on_events=None
  return self.event_rows
 def pending_questions(self,*a):return self.questions
 def answer_pending(self,*a):self.answers.append(a);return {'accepted':True}
 def resume_processing(self,*a):self.resumptions.append(a);return {'accepted':True}
 def cancel_processing(self,*a):return {'accepted':True}
 def reconcile(self,*a):return {'accepted':True}
 def ensure_session(self,*a,**kw):return 'fixture-native-session'
def setup(name):
 app=Application(root/name,lambda:now[0]);bridge=Bridge()
 return app,bridge,Worker(app.store,bridge,app.records,lambda:{})

app,b,w=setup('natural');s=app.store
body='补记昨天睡眠：一般；补记 2026-10-19 睡眠：不错'
a=app.action({'op':'natural','submission_id':'stable-natural-1','body':body})
now[0]+=dt.timedelta(days=1)
bb=app.action({'op':'natural','submission_id':'stable-natural-1','body':body})
with s.db() as c:count=c.execute("SELECT COUNT(*) FROM messages WHERE role='user'").fetchone()[0]
check('natural_request_id_and_relative_date_survive_midnight',count==2 and [x['id'] for x in a['activities']]==[x['id'] for x in bb['activities']],{'user_rows':count,'first_dates':[x['business_date'] for x in a['activities']],'retry_dates':[x['business_date'] for x in bb['activities']]})
error=None
try:
 same={'op':'natural','submission_id':'stable-natural-same-date','body':'补记 2026-10-17 睡眠：入睡好；补记 2026-10-17 睡眠：早上醒得早'}
 app.action(same);app.action(same)
except Exception as exc:error=str(exc)
with s.db() as c:rows=[r[0] for r in c.execute("SELECT m.body FROM messages m JOIN activities a ON m.activity_id=a.id WHERE a.business_date='2026-10-17' AND m.role='user'")]
check('same_date_segments_distinct_and_retry_idempotent',len(rows)==2 and error is None,{'rows':rows,'error':error})

for status in ['delivered','needs_input','permission_required']:
 app,b,w=setup('task-'+status);s=app.store;t=s.create_task('固定样本');run=s.claim_task(t['id'])
 with s.db(True) as c:c.execute("UPDATE task_runs SET session_id='native-structured' WHERE id=?",(run['run_id'],))
 result={'status':status,'artifact':'固定测试成果','verification':['fixture'],'limitations':[],'reason':'固定样本缺少材料或权限'}
 b.event_rows=[{'type':'assistant/message','seq':1,'data':{'turn':1,'message':{'role':'assistant','content':[{'type':'text','text':json.dumps(result,ensure_ascii=False)}]}}},{'type':'turn/end','seq':2,'data':{'reason':{'kind':'completed'}}}]
 w.sync_all();actual=s.task(t['id']);expected='awaiting_acceptance' if status=='delivered' else status
 check('nested_native_message_structured_'+status,actual['status']==expected,{'expected':expected,'actual':actual['status'],'run_status':actual['deliveries'][0]['status']})

app,b,w=setup('cancel-race');s=app.store;t=s.create_task('随后用户取消的任务');run=s.claim_task(t['id'])
with s.db(True) as c:c.execute("UPDATE task_runs SET session_id='native-cancel-race' WHERE id=?",(run['run_id'],))
b.event_rows=[{'type':'assistant/message','seq':1,'data':{'message':{'content':[{'type':'text','text':json.dumps({'status':'needs_input','reason':'迟到回包'})}]}}},{'type':'turn/end','seq':2,'data':{'reason':{'kind':'completed'}}}]
b.on_events=lambda:s.review_task(t['id'],run['version'],'cancel','测试用户取消，随后迟到结果不可重开')
w.sync_all();actual=s.task(t['id'])
check('cancelled_task_cannot_be_reopened_by_late_result',actual['status']=='cancelled' and actual['deliveries'][0]['status']=='cancelled',{'task_status':actual['status'],'run_status':actual['deliveries'][0]['status']})

app,b,w=setup('batch');s=app.store;a=s.ensure_activity('A3','2026-10-20')
with s.db(True) as c:c.execute("UPDATE activities SET session_id='fixture-native-session',session_state='ready' WHERE id=?",(a['id'],))
b.questions=[{'callId':'call-two','state':'continued','questions':[{'id':'one','question':'一'},{'id':'two','question':'二'}]}]
receipts=[]
for qid,body in [('one','睡得一般'),('two','醒来有点困')]:
 receipts.append(app.action({'op':'message','activity_id':a['id'],'submission_id':'stable-answer-'+qid,'body':body,'call_id':'call-two','question_id':qid}));w.work_one()
rows=[m for m in s.activity(a['id'])['messages'] if m['role']=='user']
check('targeted_batch_all_receipts_settle_after_final_answer',len(b.answers)==1 and all(m['processing_state']=='accepted_by_dsh' for m in rows),{'answer_calls':b.answers,'processing_states':[m['processing_state'] for m in rows]})
with s.db(True) as c:c.execute("UPDATE jobs SET status='done'")
app.action({'op':'retry','activity_id':a['id']});w.work_one()
check('targeted_answer_retry_uses_original_native_question_source',len(b.resumptions)==1 and b.resumptions[0][2]=='question:call-two',{'resume_calls':b.resumptions})

out['source_unchanged_during_check']=all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==sha for p,sha in source_hashes.items())
out['status']='PASS' if all(x['status']=='PASS' for x in out['checks']) and out['source_unchanged_during_check'] else 'FAIL'
print(json.dumps(out,ensure_ascii=False,indent=2))
