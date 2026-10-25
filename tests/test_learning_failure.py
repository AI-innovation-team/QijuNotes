"""A1 terminal failure visibility: isolated fixtures, no provider calls."""
import datetime as dt
import json
import tempfile
import unittest
from dsh_personal.core import TZ
from dsh_personal.learning import prepare_learning
from dsh_personal.server import Application
from dsh_personal.worker import Worker


class Bridge:
    def __init__(self):
        self.event_list=[];self.resumed=[];self.sent=[];self.questions=[]
    def ensure_session(self, aid):return 'sid-'+aid
    def pending_questions(self, sid):return self.questions
    def events(self, sid):return self.event_list
    def reconcile(self, sid, submission):
        return {'accepted':any(e.get('data',{}).get('source',{}).get('rpcId')==submission for e in self.event_list)}
    def submit(self, sid, submission, prompt, **kwargs):
        # The actual bridge also reconciles before writing a user/inbox event.
        if not self.reconcile(sid,submission)['accepted']:self.sent.append((sid,submission))
        return {'accepted':True}
    def resume_processing(self, sid, retry, source):self.resumed.append((sid,retry,source))


class LearningFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.now=dt.datetime(2026,10,23,21,30,tzinfo=TZ)
        self.app=Application(self.tmp.name,lambda:self.now);self.s=self.app.store
        self.b=Bridge();self.w=Worker(self.s,self.b,self.app.records,lambda:{})
        self.a=self.s.ensure_activity('A1',origin='scheduled');self.aid=self.a['id']
        self.sid=self.w.ensure_session(self.aid);self.a=self.s.activity(self.aid)['activity']
        self.selected=prepare_learning(self.s,self.aid)
        with self.s.db(True) as c:c.execute("UPDATE jobs SET status='done' WHERE target=?",(self.aid,))
        self.b.event_list=[
            {'seq':1,'type':'user/message','data':{'source':{'rpcId':'learning-'+self.aid},'content':[]}},
            {'seq':2,'type':'turn/start','data':{}},
            self.failure(3)]
    def tearDown(self):self.tmp.cleanup()
    def failure(self,seq,kind='error',private=''):
        return {'seq':seq,'type':'turn/end','data':{'reason':{'kind':kind,'error':{'code':'RATE_LIMIT','message':'dsh_personal_gateway: 后台预算不足；原文仍可保存，日程保持不变 '+private}}}}
    def jobs(self):
        with self.s.db() as c:return [dict(r) for r in c.execute('SELECT * FROM jobs WHERE target=?',(self.aid,))]
    def view(self):return self.s.activity(self.aid)
    def test_budget_failure_is_independent_deduplicated_and_does_not_schedule(self):
        original=self.view()['messages'];self.w.sync_all();self.w.sync_all()
        view=self.view();self.assertEqual(view['messages'],original)
        self.assertEqual(view['activity']['status'],'preparing');self.assertIsNone(view['activity']['ready_at'])
        self.assertEqual(view['processing']['reason'],'local_budget');self.assertIn('活动信息中重试',view['processing']['message'])
        self.assertNotIn('自动',view['processing']['message']);self.assertEqual(len(self.jobs()),2)
        self.assertEqual(next(j for j in self.jobs() if j['kind']=='learning')['status'],'failed')
        with self.s.db() as c:self.assertEqual(c.execute("SELECT count(*) FROM observations WHERE kind='learning_native_failure'").fetchone()[0],1)
        self.assertEqual(self.b.sent,[]);self.assertEqual(self.b.resumed,[])
    def test_late_inbox_acknowledgment_does_not_hide_terminal_failure(self):
        with self.s.db(True) as c:c.execute("UPDATE jobs SET status='running' WHERE kind='learning' AND target=?",(self.aid,))
        self.w.sync_all()
        with self.s.db(True) as c:c.execute("UPDATE jobs SET status='done' WHERE kind='learning' AND target=?",(self.aid,))
        self.w.sync_all()
        self.assertEqual(next(j for j in self.jobs() if j['kind']=='learning')['status'],'failed')
        with self.s.db() as c:self.assertEqual(c.execute("SELECT count(*) FROM observations WHERE kind='learning_native_failure'").fetchone()[0],1)

    def test_upstream_private_error_is_not_persisted(self):
        self.b.event_list[-1]=self.failure(3,private='PRIVATE_ERROR_SENTINEL')
        self.b.event_list[-1]['data']['reason']['error']['code']='UNKNOWN'
        self.w.sync_all();self.assertEqual(self.view()['processing']['reason'],'native_error')
        with self.s.db() as c:
            for table,column in [('meta','value'),('observations','details'),('jobs','error')]:
                self.assertFalse(c.execute(f'SELECT 1 FROM {table} WHERE {column} LIKE ?',('%PRIVATE_ERROR_SENTINEL%',)).fetchone())
    def test_new_turn_or_native_input_after_failure_is_not_old_failure(self):
        for etype in ('turn/start','user/message'):
            with self.subTest(etype=etype):
                self.b.event_list=self.b.event_list[:3]+[{'seq':4,'type':etype,'data':{}}]
                self.w.sync_all();self.assertIsNone(self.view()['processing'])
        # Event order need not be relied on; the newest sequence wins.
        self.b.event_list=[self.failure(7),{'seq':6,'type':'turn/start','data':{}},self.failure(3)]
        self.w.sync_all();self.assertEqual(self.view()['processing']['failure_seq'],7)
    def test_isolation_and_recovery_hold_do_not_mutate_failure_state(self):
        self.s.set_meta('isolated','true');self.w.sync_learning_failure(self.a,self.b.event_list,[])
        self.assertIsNone(self.view()['processing']);self.assertTrue(all(j['status']=='done' for j in self.jobs()))
        self.s.set_meta('isolated','false')
        with self.s.db(True) as c:c.execute("UPDATE jobs SET status='recovery_hold' WHERE kind='learning' AND target=?",(self.aid,))
        self.w.sync_learning_failure(self.a,self.b.event_list,[]);self.assertIsNone(self.view()['processing'])
        self.assertEqual(next(j for j in self.jobs() if j['kind']=='learning')['status'],'recovery_hold')
    def test_session_rotation_questions_skip_and_new_local_input_suppress_stale_failure(self):
        self.w.sync_all();self.assertIsNotNone(self.view()['processing'])
        with self.s.db(True) as c:c.execute("UPDATE activities SET session_id='changed' WHERE id=?",(self.aid,))
        self.w.sync_learning_failure(self.a,self.b.event_list,[]);self.assertIsNone(self.view()['processing'])
        with self.s.db(True) as c:c.execute('UPDATE activities SET session_id=? WHERE id=?',(self.sid,self.aid))
        self.w.sync_learning_failure(self.a,self.b.event_list,[{'callId':'question'}]);self.assertIsNone(self.view()['processing'])
        for status in ('skipped','user_ended'):
            with self.s.db(True) as c:c.execute('UPDATE activities SET status=? WHERE id=?',(status,self.aid))
            self.w.sync_learning_failure(self.a,self.b.event_list,[]);self.assertIsNone(self.view()['processing'])
        with self.s.db(True) as c:c.execute("UPDATE activities SET status='preparing' WHERE id=?",(self.aid,))
        self.w.sync_all();self.s.submit(self.aid,'new-user-input-fixture','新的本人输入')
        self.assertIsNone(self.view()['processing']);self.w.sync_all();self.assertIsNone(self.view()['processing'])
    def test_explicit_retry_preserves_original_request_and_success_clears_notice(self):
        before=self.view()['messages'];self.w.sync_all()
        self.app.action({'op':'retry','activity_id':self.aid})
        while self.w.work_one():pass
        self.assertEqual(len(self.b.resumed),1);self.assertEqual(self.b.resumed[0][::2],(self.sid,'learning-'+self.aid))
        self.assertEqual(self.b.sent,[]);self.assertEqual(self.view()['messages'],before)
        self.assertEqual(self.view()['processing']['status'],'resuming')
        self.w.sync_all();self.assertEqual(self.view()['processing']['status'],'resuming')
        self.b.event_list.append({'seq':4,'type':'turn/start','data':{}})
        self.w.sync_all();self.assertEqual(self.view()['processing']['status'],'resuming')
        manifest=self.selected['manifest'];body='\n'.join(c['source_url'] for c in manifest['cards'])+'\n'+manifest['world_fun_fact']['source_url']
        self.b.event_list.extend([{'seq':5,'type':'assistant/message','data':{'content':[{'type':'text','text':body}]}},{'seq':6,'type':'turn/end','data':{'reason':{'kind':'completed'}}}])
        self.w.sync_all();view=self.view()
        self.assertEqual(view['processing']['status'],'pending');self.assertIsNone(view['activity']['ready_at'])
        self.assertEqual(view['messages'],before)
        # The native retry succeeded, but publication now requires a separate review.
        from dsh_personal import learning_guard as guard
        candidate=guard.get_pending(self.s,self.aid)
        attempt=guard.record_attempt(self.s,candidate['id'])
        guard.commit(self.s,candidate['id'],True,attempt_token=attempt['attempt_token'])
        view=self.view()
        self.assertIsNone(view['processing']);self.assertIsNotNone(view['activity']['ready_at'])
        self.assertEqual(view['activity']['status'],'awaiting_reply')
        self.assertEqual(next(j for j in self.jobs() if j['kind']=='learning')['status'],'done')
        # A late obsolete failure must never replace successful readiness.
        self.w.sync_learning_failure(self.a,[self.failure(7)],[])
        self.assertIsNone(self.view()['processing']);self.assertIsNotNone(self.view()['activity']['ready_at'])
    def test_new_native_failure_before_retry_rpc_returns_stays_failed(self):
        self.w.sync_all()
        def resume_with_immediate_failure(sid,retry,source):
            self.b.resumed.append((sid,retry,source))
            self.b.event_list.extend([{'seq':4,'type':'turn/start','data':{}},self.failure(5)])
            self.w.sync_all()
        self.b.resume_processing=resume_with_immediate_failure
        self.app.action({'op':'retry','activity_id':self.aid})
        while self.w.work_one():pass
        self.w.sync_all();state=self.view()['processing']
        self.assertEqual(state['failure_seq'],5);self.assertEqual(state['status'],'failed')
        self.assertEqual(len(self.b.resumed),1)

    def test_retry_failure_with_new_sequence_updates_safe_notice_without_automatic_job(self):
        self.w.sync_all();self.b.event_list.extend([{'seq':4,'type':'turn/start','data':{}},self.failure(5)])
        self.w.sync_all();self.assertEqual(self.view()['processing']['failure_seq'],5)
        self.assertFalse(any(j['kind']=='native_retry' for j in self.jobs()))


if __name__=='__main__':unittest.main()
