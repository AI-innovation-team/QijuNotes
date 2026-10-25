import concurrent.futures, datetime as dt, tempfile, unittest
from pathlib import Path
from dsh_personal.core import Store,TZ
from dsh_personal.server import Application

class CoreTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.clock=[dt.datetime(2026,10,22,7,0,tzinfo=TZ)]
  self.s=Store(Path(self.tmp.name)/'db.sqlite',lambda:self.clock[0])
 def tearDown(self):self.tmp.cleanup()
 def advance(self,**kw):self.clock[0]+=dt.timedelta(**kw)
 def test_daily_silence_skip_short_and_single_scheduler(self):
  ids=[]
  for day in range(16):
   self.clock[0]=dt.datetime(2026,10,22,22,tzinfo=TZ)+dt.timedelta(days=day)
   activities=self.s.tick();self.assertEqual(len(activities),3)
   for a in activities:
    ids.append(a['id'])
    if day%2:self.s.control(a['id'],'skip')
    if day%3==0:self.s.submit(a['id'],'msg-'+a['id'],'收到')
   self.assertEqual([a['id'] for a in self.s.tick()],[a['id'] for a in activities])
  self.assertEqual(len(set(ids)),48)
  self.assertTrue(all(s['weekdays']==list(range(7)) for s in self.s.schedules()))
 def test_explicit_pause_expiry_and_non_schedule_day(self):
  self.s.change_schedule('A3','08:30',[0,1,2,3,4,5,6],'2026-10-23','用户明确暂停到23日')
  self.clock[0]=dt.datetime(2026,10,23,22,tzinfo=TZ)
  self.assertNotIn('A3',[a['routine'] for a in self.s.tick()])
  self.clock[0]=dt.datetime(2026,10,24,9,tzinfo=TZ)
  self.assertIn('A3',[a['routine'] for a in self.s.tick()])
  with self.assertRaises(PermissionError):self.s.change_schedule('A3','08:30',[1],None,'模型评价',actor='agent')
  self.assertEqual(next(s for s in self.s.schedules() if s['routine']=='A1')['weekdays'],list(range(7)))
 def test_concurrent_unique_and_receipt_survives_restart(self):
  with concurrent.futures.ThreadPoolExecutor(8) as pool:
   acts=list(pool.map(lambda _:self.s.ensure_activity('A3'),range(16)))
  self.assertEqual(len({a['id'] for a in acts}),1);aid=acts[0]['id']
  with concurrent.futures.ThreadPoolExecutor(8) as pool:
   receipts=list(pool.map(lambda _:self.s.submit(aid,'offline-repeat-id','睡得一般'),range(16)))
  self.assertEqual(len({r['receipt'] for r in receipts}),1)
  other=Store(self.s.path,lambda:self.clock[0]);self.assertEqual(len([m for m in other.activity(aid)['messages'] if m['role']=='user']),1)
  with self.assertRaises(ValueError):other.submit(aid,'offline-repeat-id','不同文本')
 def test_summary_race_correction_survives_new_answer(self):
  a=self.s.ensure_activity('A3');self.s.submit(a['id'],'sleep-msg-1','00:30睡，08:10起，3分')
  old=self.s.summary_snapshot(a['id']);self.s.summarize_literal(a['id'])
  self.s.correct(a['id'],{'quality':4},'实际是4分')
  self.assertFalse(self.s.commit_summary(old,{'fields':{'quality':1}}))
  self.s.submit(a['id'],'sleep-msg-2','还想深入谈谈睡眠规律');self.s.summarize_literal(a['id'])
  view=self.s.activity(a['id']);self.assertEqual(view['summary']['fields']['quality'],4)
  self.assertIsNone(view['summary']['fields']['exercise_actual'])
  self.assertEqual(view['activity']['id'],a['id'])
 def test_history_multi_day_exact_dates_and_today(self):
  out=self.s.natural('补记昨天的睡眠：睡得一般；补记前天的睡眠：睡得很好')
  self.assertEqual([a['business_date'] for a in out['activities']],['2026-10-21','2026-10-20'])
  for a in out['activities']:
   self.assertEqual(self.s.ensure_activity('A3',a['business_date'])['id'],a['id'])
   self.s.summarize_literal(a['id']);v=self.s.activity(a['id']);self.assertEqual(v['summary']['business_date'],a['business_date'])
   self.assertTrue(all(m['received_at'].startswith('2026-10-21T23:') for m in v['messages']))
  with self.s.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM notifications').fetchone()[0],0)
  self.clock[0]=dt.datetime(2026,10,22,9,tzinfo=TZ)
  self.assertEqual(self.s.tick()[0]['business_date'],'2026-10-22')
 def test_recovery_only_today_silent(self):
  self.clock[0]=dt.datetime(2026,10,28,22,tzinfo=TZ)
  out=self.s.tick(recovery=True);self.assertEqual(len(out),3)
  self.assertTrue(all(a['business_date']=='2026-10-28' and a['notification_state']=='recovery_silent' for a in out))
  self.assertTrue(all(g['status']=='before_start' for g in self.s.gaps('2026-10-01')))
 def test_three_routines_never_close_after_end_and_late_reply(self):
  for routine in ['A1','A2','A3']:
   a=self.s.ensure_activity(routine);self.s.control(a['id'],'end');self.advance(days=1)
   for n in range(5):self.s.submit(a['id'],f'{routine}-late-{n}','继续深入谈这个问题')
   reloaded=Store(self.s.path,lambda:self.clock[0]);self.assertEqual(len([m for m in reloaded.activity(a['id'])['messages'] if m['role']=='user']),5)
 def test_budget_atomic_persistent_unknown_and_save(self):
  self.s.set_meta('daily_tokens',100)
  def take(_):
   try:return self.s.reserve(60)
   except RuntimeError:return None
  with concurrent.futures.ThreadPoolExecutor(2) as pool:ids=list(pool.map(take,range(2)))
  self.assertEqual(sum(x is not None for x in ids),1)
  reloaded=Store(self.s.path,lambda:self.clock[0])
  with self.assertRaises(RuntimeError):reloaded.reserve(60)
  a=reloaded.ensure_activity('A3');self.assertTrue(reloaded.submit(a['id'],'budget-save-id','睡得一般')['receipt'])
 def test_task_versioned_human_authority(self):
  t=self.s.create_task('整理给定文本');r=self.s.claim_task(t['id']);self.s.deliver_task(t['id'],1,'成果一')
  with self.assertRaises(PermissionError):self.s.review_task(t['id'],1,'accept','自检通过',actor='agent')
  self.s.review_task(t['id'],1,'revise','补充来源');r=self.s.claim_task(t['id']);self.assertEqual(r['version'],2)
  self.s.deliver_task(t['id'],2,'成果二')
  with self.assertRaises(ValueError):self.s.review_task(t['id'],1,'accept','旧版通过')
  self.s.review_task(t['id'],2,'accept','第二版验收通过')
  self.assertEqual(self.s.task(t['id'])['status'],'accepted')
 def test_delete_source_and_raw_then_no_replay(self):
  a=self.s.ensure_activity('A3');r=self.s.submit(a['id'],'to-delete-id','睡眠原文');self.s.summarize_literal(a['id'])
  self.s.delete_message(r['receipt'],'明确删除')
  self.assertNotIn('睡眠原文',str(self.s.journal()))
  self.assertIsNone(self.s.activity(a['id'])['summary'])
  self.assertEqual(self.s.activity(a['id'])['activity']['session_state'],'deletion_pending')
  with self.assertRaises(ValueError):self.s.submit(a['id'],'to-delete-id','睡眠原文')
 def test_attachment_real_content_and_cleanup(self):
  app=Application(Path(self.tmp.name)/'app',lambda:self.clock[0]);a=app.store.ensure_activity('A3')
  r=app.action({'op':'attachment_upload','activity_id':a['id'],'filename':'x.txt','content_base64':'eA=='})
  app.action({'op':'cleanup'});self.assertEqual((app.data/'attachments'/r['id']).read_bytes(),b'x')
if __name__=='__main__':unittest.main()

class NaturalRetryTests(unittest.TestCase):
 setUp=CoreTests.setUp
 tearDown=CoreTests.tearDown
 advance=CoreTests.advance
 def test_natural_stable_segment_ids_and_midnight_retry(self):
  body='补记昨天睡眠：好；补记昨天睡眠：起床有点晚'
  first=self.s.natural(body,'request-midnight')
  aid=first['activity']['id'];self.advance(days=1)
  second=self.s.natural(body,'request-midnight')
  self.assertEqual(second['activity']['id'],aid)
  self.assertEqual(len([m for m in self.s.activity(aid)['messages'] if m['role']=='user']),2)
 def test_ambiguous_later_segment_writes_no_partial_fact(self):
  self.s.natural('补记昨天睡眠：好；前几天也一般','ambiguous-id')
  with self.s.db() as c:self.assertEqual(c.execute("SELECT count(*) FROM messages WHERE role='user'").fetchone()[0],0)
