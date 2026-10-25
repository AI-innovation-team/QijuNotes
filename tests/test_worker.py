import datetime as dt,tempfile,unittest,json,threading,http.server
from pathlib import Path
from dsh_personal.server import Application
from dsh_personal.core import TZ
from dsh_personal.worker import Worker
class Bridge:
 def __init__(self):self.calls=[];self.event_list=[]
 def ensure_session(self,aid):return 'sid-'+aid
 def reconcile(self,*a):return {'accepted':False}
 def submit(self,*a,**k):self.calls.append((a,k));return {'accepted':True}
 def events(self,*a):return self.event_list
 def pending_questions(self,*a):return []
 def cancel_processing(self,*a):self.calls.append(('cancel',a))
class WorkerTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.now=[dt.datetime(2026,10,22,21,30,tzinfo=TZ)]
  self.app=Application(self.tmp.name,lambda:self.now[0]);self.s=self.app.store;self.b=Bridge();self.w=Worker(self.s,self.b,self.app.records,lambda:{'model':{'name':'test-fixture'}})
 def tearDown(self):self.tmp.cleanup()
 def test_learning_context_uses_published_replies_and_tracks_deletion_provenance(self):
  from unittest.mock import patch
  a=self.s.ensure_activity('A1');sid=self.w.ensure_session(a['id'])
  with self.s.db(True) as db:
   for i in range(5):
    db.execute('INSERT INTO messages VALUES(?,?,NULL,?,?,?,?,?,NULL,?)',
      ('published-'+str(i),a['id'],'assistant','VISIBLE '+str(i),self.s.stamp(),
       'learning_edited' if i==3 else 'learning_reviewed',str(i),1 if i==4 else 0))
   db.execute('INSERT INTO messages VALUES(?,?,NULL,?,?,?,?,?,NULL,0)',
     ('unpublished',a['id'],'assistant','UNPUBLISHED DRAFT',self.s.stamp(),'native','6'))
  with patch('dsh_personal.deletion.register_context') as register:
   result=self.w.contextual_input(a['id'],'继续讨论')
  context=json.loads(result.split('\n')[1]);published=context['published_learning_replies']
  self.assertEqual([m['message_id'] for m in published],['published-1','published-2','published-3'])
  self.assertNotIn('UNPUBLISHED DRAFT',result);self.assertNotIn('VISIBLE 4',result)
  refs=[ref for call in register.call_args_list for ref in call.args[2]]
  self.assertIn('message:published-3',refs);self.assertNotIn('message:unpublished',refs)
 def test_background_immediate_scheduled_recurring_version(self):
  t=self.s.create_task('测试成果',interval_days=1);self.w.dispatch_tasks();t=self.s.task(t['id']);self.assertEqual(t['status'],'running');self.assertEqual(len(self.b.calls),1)
  self.b.event_list=[{'type':'assistant/message','data':{'message':{'content':[{'type':'text','text':json.dumps({'status':'delivered','artifact':'固定测试成果','verification':['固定样本'],'limitations':[]})}]}}},{'type':'turn/end','data':{'reason':{'kind':'completed'}}}]
  self.w.sync_all();self.assertEqual(self.s.task(t['id'])['status'],'awaiting_acceptance')
  self.now[0]+=dt.timedelta(days=1);self.w.dispatch_tasks();self.assertEqual(self.s.task(t['id'])['version'],2)
  once=self.s.create_task('明天排期',run_at=(self.now[0]+dt.timedelta(hours=1)).isoformat());self.w.dispatch_tasks();self.assertEqual(self.s.task(once['id'])['status'],'queued')
  self.now[0]+=dt.timedelta(hours=1);self.w.dispatch_tasks();self.assertEqual(self.s.task(once['id'])['status'],'running')
 def test_permission_and_input_are_not_deliveries(self):
  for status in ['needs_input','permission_required']:
   t=self.s.create_task('需外部访问的任务');run=self.s.claim_task(t['id']);run['id']=run['run_id'];run['session_id']='sid'
   self.w.finish_task(run,json.dumps({'status':status,'reason':'缺少授权或材料'}));self.assertEqual(self.s.task(t['id'])['status'],status)
   with self.assertRaises(ValueError):self.s.review_task(t['id'],1,'accept','错误接受')
  self.now[0]+=dt.timedelta(minutes=6);self.w.dispatch_tasks();self.assertEqual(self.s.task(t['id'])['status'],'needs_input')
 def test_three_dimensions_actual_multi_answer_and_depth(self):
  a=self.s.ensure_activity('A2');self.s.submit(a['id'],'reflection-multi-1','博士实验有进展，创业今天无投入，文化方面读了一章；继续深入实验问题？')
  view=self.s.activity(a['id']);self.assertEqual({r['line'] for r in view['reflection']},{'doctorate','startup','culture'})
  self.assertEqual(view['activity']['status'],'basic_complete');self.assertEqual(view['activity']['current_step'],'doctorate')
  self.s.submit(a['id'],'reflection-multi-2','我还想接着讨论');self.assertEqual(len([m for m in self.s.activity(a['id'])['messages'] if m['role']=='user']),2)
 def test_exact_utc_time_and_foreground_ack_policy(self):
  self.now[0]=dt.datetime(2026,10,22,12,59,tzinfo=dt.timezone.utc);self.s.tick()
  self.assertFalse([a for a in self.app.state(view='chat')['activities'] if a['routine']=='A1'])
  self.now[0]+=dt.timedelta(minutes=1);acts=self.s.tick();a=next(x for x in acts if x['routine']=='A1')
  self.assertEqual(a['scheduled_at'],'2026-10-22T21:00:00+08:00')
  a=self.s.ensure_activity('A2',origin='scheduled')
  response=self.app.action({'op':'presented','activity_id':a['id'],'visible':True,'interaction_at':self.s.stamp(),'lease':'old'},'browser')
  self.assertFalse(response['suppressed'])
  self.app.leases['live']={'session':'browser'}
  response=self.app.action({'op':'presented','activity_id':a['id'],'visible':True,'interaction_at':self.s.stamp(),'lease':'live'},'browser')
  self.assertTrue(response['suppressed'])
 def test_notification_quiet_hours_once_and_unknown_no_blind_retry(self):
  requests=[]
  class H(http.server.BaseHTTPRequestHandler):
   def log_message(self,*a):pass
   def do_POST(self):
    requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))));self.send_response(200);self.end_headers()
  server=http.server.ThreadingHTTPServer(('127.0.0.1',0),H);threading.Thread(target=server.serve_forever,daemon=True).start()
  self.w.config=lambda:{'notification':{'url':f'http://127.0.0.1:{server.server_port}'}}
  a=self.s.ensure_activity('A2',origin='scheduled');self.now[0]=dt.datetime(2026,10,22,23,30,tzinfo=TZ)
  self.w.notifications();self.assertEqual(len(requests),0)
  self.now[0]+=dt.timedelta(hours=9);self.w.notifications();self.w.notifications();self.assertEqual(len(requests),1)
  self.assertNotIn('body',requests[0]);self.assertEqual(requests[0]['message'],'活动已就绪');server.shutdown()
 def test_slow_jobs_do_not_block_scheduler_and_pool_is_bounded(self):
  from dsh_personal.operations import maintenance_lock
  self.now[0]=dt.datetime(2026,10,22,20,59,59,tzinfo=TZ)
  first=self.s.ensure_activity('A2','2026-10-20');second=self.s.ensure_activity('A2','2026-10-21')
  with self.s.db(True) as c:
   c.execute('DELETE FROM jobs')
   self.s._enqueue(c,'summary',first['id'],1)
   self.s._enqueue(c,'summary',first['id'],2)
   self.s._enqueue(c,'summary',second['id'],1)
  release=threading.Event();both_started=threading.Event();state_lock=threading.Lock()
  seen=[];active=[0];peak=[0];timed_out=[]
  def slow_summary(aid):
   with state_lock:
    seen.append(aid);active[0]+=1;peak[0]=max(peak[0],active[0])
    if active[0]==2:both_started.set()
   try:
    if not release.wait(10):timed_out.append(aid)
   finally:
    with state_lock:active[0]-=1
  self.w.semantic_summary=slow_summary
  ticked=threading.Event();synced=threading.Event();dispatched=threading.Event();notified=threading.Event()
  original_tick=self.s.tick
  def observed_tick(recovery=False):
   rows=original_tick(recovery=recovery)
   if not recovery and any(a['routine']=='A1' for a in rows):ticked.set()
   return rows
  self.s.tick=observed_tick;self.w.sync_all=synced.set;self.w.dispatch_tasks=dispatched.set;self.w.notifications=notified.set
  try:
   self.w.start();self.assertTrue(both_started.wait(3),'two independent jobs should start')
   self.w.start()  # Idempotent start must neither reset claims nor create another pool.
   self.assertEqual(len(self.w.job_threads),2)
   self.assertTrue(all(t.is_alive() for t in self.w.job_threads))
   self.assertEqual(set(seen),{first['id'],second['id']})
   with self.s.db() as c:
    self.assertEqual(c.execute("SELECT count(*) FROM jobs WHERE status='running'").fetchone()[0],2)
    self.assertEqual(c.execute("SELECT status FROM jobs WHERE target=? AND version=2",(first['id'],)).fetchone()[0],'pending')
   # Both slow jobs own checkpoint protection even while doing no SQLite work.
   with self.assertRaises(RuntimeError):
    with maintenance_lock(self.s.path.parent,exclusive=True,timeout=0.03):pass
   self.now[0]=dt.datetime(2026,10,22,21,0,tzinfo=TZ)
   self.assertTrue(ticked.wait(3),'21:00 activity should trigger while model jobs are still blocked')
   self.assertTrue(synced.wait(1));self.assertTrue(dispatched.wait(1));self.assertTrue(notified.wait(1))
   self.assertFalse(release.is_set());self.assertEqual(active[0],2);self.assertEqual(peak[0],2)
   with self.s.db() as c:
    row=c.execute("SELECT origin,scheduled_at FROM activities WHERE routine='A1'").fetchone()
    self.assertEqual(row['origin'],'scheduled');self.assertEqual(row['scheduled_at'],'2026-10-22T21:00:00+08:00')
  finally:
   self.w.stop.set();release.set()
   for thread in [*self.w.job_threads,*([self.w.thread,self.w.maintenance_thread,self.w.notification_thread] if hasattr(self.w,'thread') else [])]:thread.join(3)
  self.assertFalse(timed_out)
  self.assertTrue(all(not t.is_alive() for t in self.w.job_threads));self.assertFalse(self.w.thread.is_alive());self.assertFalse(self.w.maintenance_thread.is_alive())
  with maintenance_lock(self.s.path.parent,exclusive=True,timeout=0.1):pass
 def test_blocked_sync_does_not_delay_schedule_tick(self):
  from dsh_personal.operations import maintenance_lock
  self.now[0]=dt.datetime(2026,10,22,21,29,59,tzinfo=TZ)
  self.w.config=lambda:{}
  release=threading.Event();sync_started=threading.Event();sync_finished=threading.Event();ticked=threading.Event()
  calls=[];timed_out=[];dispatched=threading.Event();notified=threading.Event()
  def blocked_sync():
   calls.append(threading.current_thread());sync_started.set()
   if not release.wait(10):timed_out.append(True)
   sync_finished.set()
  original_tick=self.s.tick
  def observed_tick(recovery=False):
   rows=original_tick(recovery=recovery)
   if not recovery and any(a['routine']=='A2' for a in rows):ticked.set()
   return rows
  self.s.tick=observed_tick;self.w.sync_all=blocked_sync
  self.w.dispatch_tasks=dispatched.set;self.w.notifications=notified.set
  try:
   self.w.start();self.assertTrue(sync_started.wait(3))
   thread=self.w.maintenance_thread;self.w.start();self.assertIs(self.w.maintenance_thread,thread)
   self.assertIs(calls[0],thread);self.assertEqual(len(self.w.job_threads),2)
   with self.assertRaises(RuntimeError):
    with maintenance_lock(self.s.path.parent,exclusive=True,timeout=0.03):pass
   self.now[0]=dt.datetime(2026,10,22,21,30,tzinfo=TZ)
   self.assertTrue(ticked.wait(3),'21:30 trigger must not wait for native sync to finish')
   self.assertFalse(sync_finished.is_set());self.assertEqual(len(calls),1)
   with self.s.db() as c:
    row=c.execute("SELECT origin,scheduled_at FROM activities WHERE routine='A2'").fetchone()
    self.assertEqual(row['origin'],'scheduled');self.assertEqual(row['scheduled_at'],'2026-10-22T21:30:00+08:00')
   self.assertTrue(notified.wait(1),'due notification transport must not wait for native sync')
   # Task dispatch resumes on its maintenance thread after the blocked RPC.
   release.set();self.assertTrue(dispatched.wait(1))
  finally:
   self.w.stop.set();release.set()
   for thread in [*self.w.job_threads,*([self.w.thread,self.w.maintenance_thread,self.w.notification_thread] if hasattr(self.w,'thread') else [])]:thread.join(3)
  self.assertFalse(timed_out);self.assertEqual(len(calls),1)
  self.assertFalse(self.w.maintenance_thread.is_alive());self.assertFalse(self.w.thread.is_alive())
 def test_changed_or_disabled_profiles_rotate_and_old_question_is_not_replayed(self):
  p=self.app.records.add_profile('OLD_PROFILE_FIXTURE',confirmed=True,user_input='confirmed')
  a=self.s.ensure_activity('A2');sid=self.w.ensure_session(a['id'])
  self.s.submit(a['id'],'history-context','retained user history')
  self.w.contextual_input(a['id'],'retained user history')
  self.app.records.revise_profile(p['item_id'],1,'confirmed correction',content='NEW_PROFILE_FIXTURE')
  mid=self.s.submit(a['id'],'answer-after-revision','new explicit reply','old-call','old-question')['receipt']
  with self.s.db(True) as c:
   c.execute('DELETE FROM jobs');self.s._enqueue(c,'handoff',mid)
  self.assertTrue(self.w.work_one())
  new_sid=self.s.activity(a['id'])['activity']['session_id'];self.assertNotEqual(new_sid,sid)
  text=self.b.calls[-1][0][2];self.assertNotIn('OLD_PROFILE_FIXTURE',text);self.assertIn('NEW_PROFILE_FIXTURE',text)
  self.assertIn('retained user history',text);self.assertIn('question_context_expired',text)
  self.assertEqual(self.s.meta('stale_question:'+mid),'true')
  # Duplicate worker invocation cannot direct the old question to the new session.
  with self.s.db(True) as c:c.execute("UPDATE jobs SET status='pending' WHERE target=?",(mid,))
  self.assertTrue(self.w.work_one())
  self.app.records.disable_profile(p['item_id'],2,'explicit disable')
  disabled_sid=self.w.ensure_session(a['id']);self.assertNotEqual(new_sid,disabled_sid)
  self.assertNotIn('NEW_PROFILE_FIXTURE',self.w.contextual_input(a['id'],'next question'))
  self.assertEqual(disabled_sid,self.w.ensure_session(a['id']))
  self.assertEqual(len(self.app.records.list_profile(history=True,include_inactive=True)),3)
 def test_stale_native_sync_cannot_restore_old_question_after_context_rotation(self):
  a=self.s.ensure_activity('A2');old=self.w.ensure_session(a['id'])
  p=self.app.records.add_profile('new current profile',confirmed=True,user_input='fixture')
  def rotate_during_rpc(sid):
   self.assertEqual(sid,old);self.w.ensure_session(a['id'])
   return [{'callId':'stale-call','questions':[{'id':'q','text':'old question'}]}]
  self.b.pending_questions=rotate_during_rpc
  self.w.sync_all()
  self.assertEqual(self.s.activity(a['id'])['questions'],[])
  self.assertNotEqual(self.s.activity(a['id'])['activity']['session_id'],old)
 def test_queued_retry_cannot_resume_old_question_after_another_job_rotates(self):
  p=self.app.records.add_profile('old confirmed profile',confirmed=True,user_input='fixture')
  a=self.s.ensure_activity('A2');old=self.w.ensure_session(a['id'])
  mid=self.s.submit(a['id'],'old-question-reply','Explicit reply','old-call','old-question')['receipt']
  self.s.set_meta('question_session:old-call',old)
  resumed=[];self.b.resume_processing=lambda *args:resumed.append(args)
  with self.s.db(True) as c:
   c.execute('DELETE FROM jobs');self.s._enqueue(c,'native_retry',a['id'],1)
  self.app.action({'op':'profile_save','item_id':p['item_id'],'version':1,'content':'new confirmed profile','confirmed':True,'user_input':'fixture'})
  # Another job rotates first, at the same timestamp as the queued retry.
  new=self.w.ensure_session(a['id']);self.assertNotEqual(new,old)
  self.assertTrue(self.w.work_one());self.assertEqual(resumed,[])
  with self.s.db() as c:
   retry=c.execute("SELECT status,error FROM jobs WHERE kind='native_retry' AND version=1").fetchone()
   self.assertEqual(retry['status'],'cancelled');self.assertIn('未重放',retry['error'])
  # A stale question that was explicitly re-sent as ordinary continuation may
  # retry only once its submission is reconciled in this new session.
  self.s.set_meta('stale_question:'+mid,'true')
  with self.s.db(True) as c:self.s._enqueue(c,'native_retry',a['id'],2)
  self.assertTrue(self.w.work_one());self.assertEqual(resumed,[])
  with self.s.db() as c:self.assertEqual(c.execute("SELECT status FROM jobs WHERE kind='native_retry' AND version=2").fetchone()[0],'cancelled')
  self.b.reconcile=lambda sid,source:{'accepted':sid==new and source=='old-question-reply'}
  with self.s.db(True) as c:self.s._enqueue(c,'native_retry',a['id'],3)
  self.assertTrue(self.w.work_one());self.assertEqual(len(resumed),1)
  self.assertEqual((resumed[0][0],resumed[0][2]),(new,'old-question-reply'))
 def test_work_one_remains_directly_callable(self):
  a=self.s.ensure_activity('A2')
  with self.s.db(True) as c:
   c.execute('DELETE FROM jobs');self.s._enqueue(c,'summary',a['id'])
  self.w.config=lambda:{}
  self.assertTrue(self.w.work_one());self.assertFalse(self.w.work_one())
  self.assertEqual(self.w.job_threads,[])
  with self.s.db() as c:self.assertEqual(c.execute('SELECT status FROM jobs').fetchone()[0],'done')
if __name__=='__main__':unittest.main()
