"""Focused product contracts; injected time and synthetic inputs, not real-user acceptance."""
import datetime as dt
from pathlib import Path
import tempfile
import unittest
from dsh_personal.server import Application
from dsh_personal.core import TZ, Store, dumps
from dsh_personal.interaction import command

class ProductContracts(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.now=[dt.datetime(2026,10,22,7,0,tzinfo=TZ)]
  self.app=Application(self.temp.name,lambda:self.now[0]);self.s=self.app.store
 def test_sixteen_days_each_has_exactly_one_first_notification(self):
  for day in range(16):
   for hour,minute in [(8,30),(21,0),(21,30)]:
    self.now[0]=dt.datetime(2026,10,22,hour,minute,tzinfo=TZ)+dt.timedelta(days=day)
    acts=self.s.tick();self.s.tick()
   for a in acts:
    if day%2:self.s.control(a['id'],'skip')
    elif day%3==0:self.s.submit(a['id'],'short-'+a['id'],'收到')
  with self.s.db() as c:
   self.assertEqual(c.execute('SELECT count(*) FROM notifications').fetchone()[0],48)
   self.assertEqual(c.execute("SELECT count(*) FROM notifications WHERE kind!='first'").fetchone()[0],0)
   self.assertEqual(c.execute('SELECT count(*) FROM activities a WHERE NOT EXISTS (SELECT 1 FROM learning_releases r WHERE r.activity_id=a.id AND r.activated_at IS NULL)').fetchone()[0],48)
   self.assertEqual(c.execute('SELECT count(*) FROM learning_releases WHERE activated_at IS NULL').fetchone()[0],1)
  self.assertTrue(all(x['weekdays']==list(range(7)) for x in self.s.schedules()))
 def test_reflection_first_answer_restart_restores_question_progress_summary(self):
  a=self.s.ensure_activity('A2');self.s.submit(a['id'],'reflection-answer-before-restart','博士实验完成了一次预试')
  self.s.summarize_literal(a['id'])
  with self.s.db(True) as c:c.execute('INSERT INTO native_questions VALUES(?,?,?)',(a['id'],dumps([{'callId':'fixture-call','questions':[{'id':'startup','question':'科研创业这条线呢？'}]}]),self.s.stamp()))
  before=self.s.activity(a['id']);self.now[0]+=dt.timedelta(days=1)
  after=Application(self.temp.name,lambda:self.now[0]).store.activity(a['id'])
  self.assertEqual(after,before);self.assertEqual(after['activity']['current_step'],'startup')
 def test_startup_statement_does_not_answer_doctorate(self):
  a=self.s.ensure_activity('A2');self.s.submit(a['id'],'startup-only-statement','科研创业今天未投入，继续深入讨论这个选择？')
  self.assertEqual([x['line'] for x in self.s.activity(a['id'])['reflection']],['startup'])
 def test_task_network_retry_and_relative_date_keep_one_original_task(self):
  first=self.app.action({'op':'task_create','goal':'固定测试委托','submission_id':'task-network-retry'})
  self.now[0]+=dt.timedelta(days=1)
  again=Application(self.temp.name,lambda:self.now[0]).action({'op':'task_create','goal':'固定测试委托','submission_id':'task-network-retry'})
  self.assertEqual(first['id'],again['id']);self.assertEqual(first['run_at'],again['run_at'])
  with self.assertRaises(ValueError):self.app.action({'op':'task_create','goal':'更改的目标','submission_id':'task-network-retry'})
  body='明天完成：整理固定样本文本'
  first=command(self.s,body,submission_id='natural-task-retry')['task'];self.now[0]+=dt.timedelta(days=1)
  again=command(self.s,body,submission_id='natural-task-retry')['task']
  self.assertEqual(first['id'],again['id']);self.assertEqual(first['run_at'],again['run_at'])
  self.assertIsNone(command(self.s,'也许有空可以整理一下资料'))
  self.assertEqual(len(self.s.tasks()),2)
 def test_archive_covers_chat_routines_tasks_and_unsent_native_handoff(self):
  ids=[]
  for routine in ['CHAT','A1','A2','A3']:
   a=self.s.ensure_activity(routine);ids.append(self.s.submit(a['id'],'archive-'+routine,'固定原话-'+routine)['receipt'])
  task=self.s.create_task('固定委托原话');run=self.s.claim_task(task['id']);self.s.deliver_task(task['id'],run['version'],'固定成果原话')
  self.s.review_task(task['id'],1,'accept','固定测试用户评价，不是真实验收')
  rows=self.s.journal(query='固定');self.assertTrue(set(ids).issubset({x['id'] for x in rows}))
  self.assertTrue({'固定委托原话','固定成果原话','固定测试用户评价，不是真实验收'}.issubset({x['body'] for x in rows}))
  self.assertTrue(all(x['processing_state']=='pending' for x in rows if x['id'] in ids))
 def test_cleanup_preserves_pending_raw_attachment_and_shows_capacity(self):
  a=self.s.ensure_activity('A3');self.s.submit(a['id'],'cleanup-source-id','固定唯一原话')
  with self.s.db(True) as c:c.execute('INSERT INTO native_questions VALUES(?,?,?)',(a['id'],dumps([{'callId':'pending','questions':[]}]),self.s.stamp()))
  asset=self.app.action({'op':'attachment_upload','activity_id':a['id'],'filename':'fixture.txt','content_base64':'eA=='})
  cache=Path(self.temp.name)/'cache';cache.mkdir();(cache/'regenerable.tmp').write_text('cache')
  before=self.s.activity(a['id']);self.assertGreater(self.app.state()['health']['storage_bytes'],0)
  self.assertEqual(self.app.action({'op':'cleanup'})['removed_regenerable_files'],1)
  self.assertEqual(Application(self.temp.name).store.activity(a['id']),before)
  self.assertEqual((Path(self.temp.name)/'attachments'/asset['id']).read_bytes(),b'x')

if __name__=='__main__':unittest.main()
