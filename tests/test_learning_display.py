"""Presentation of blocked initial lessons without changing scheduler state."""
import datetime as dt
import tempfile
import unittest
from dsh_personal import learning_guard as guard
from dsh_personal.core import TZ,dumps
from dsh_personal.learning import prepare_learning
from dsh_personal.server import Application
from dsh_personal.worker import Worker

class Bridge:
    def ensure_session(self, identity):return 'display-fixture:'+identity

class LearningDisplayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=Application(self.temp.name,lambda:dt.datetime(2026,10,24,22,tzinfo=TZ))
        self.store=self.app.store;self.worker=Worker(self.store,Bridge(),self.app.records,lambda:{})
        self.aid=self.store.ensure_activity('A1')['id'];self.sid=self.worker.ensure_session(self.aid)
        self.manifest=prepare_learning(self.store,self.aid)['manifest']
    def tearDown(self):self.temp.cleanup()
    def reject(self,mode='initial'):
        row=guard.enqueue(self.store,self.store.activity(self.aid)['activity'],
            {'session_id':self.sid,'end_seq':9,'assistant_seq':8,'received_at':self.store.stamp()},
            'Isolated rejected fixture',self.manifest,self.app.records.context('routine:A1'),mode=mode)
        token=guard.record_attempt(self.store,row['id'])['attempt_token']
        guard.commit(self.store,row['id'],False,attempt_token=token)
        return row
    def test_rejected_is_consistent_in_detail_lists_bookmark_and_history(self):
        self.reject()
        with self.store.db(True) as db:db.execute('UPDATE activities SET bookmarked=1 WHERE id=?',(self.aid,))
        detail=self.app.activity(self.aid)
        self.assertEqual(detail['activity']['status'],'preparing')
        self.assertEqual(detail['activity']['display_status'],'learning_rejected')
        self.assertEqual(detail['processing']['status'],'rejected')
        for view in ('full','chat'):
            state=self.app.state(view=view)
            item=next(a for a in state['activities'] if a['id']==self.aid)
            archived=next(a for p in state['routine_projects'] for a in p['activities'] if a['id']==self.aid)
            self.assertEqual(item['display_status'],'learning_rejected')
            self.assertEqual(archived['display_status'],'learning_rejected')
        self.assertEqual(self.app.bookmarks()[0]['display_status'],'learning_rejected')
        gap=next(r for r in self.store.gaps() if r['activity_id']==self.aid)
        self.assertEqual(gap['status'],'preparing');self.assertEqual(gap['display_status'],'learning_rejected')
        with self.store.db() as db:self.assertEqual(db.execute('SELECT status FROM activities WHERE id=?',(self.aid,)).fetchone()[0],'preparing')
    def test_published_lesson_is_not_failed_when_followup_rejected(self):
        with self.store.db(True) as db:db.execute("UPDATE activities SET ready_at=?,status='awaiting_reply' WHERE id=?",(self.store.stamp(),self.aid))
        self.reject(mode='followup')
        detail=self.app.activity(self.aid)
        self.assertEqual(detail['processing']['mode'],'followup')
        self.assertEqual(detail['processing']['status'],'rejected')
        self.assertEqual(detail['activity']['display_status'],'awaiting_reply')
        archived=next(a for p in self.app.state(view='chat')['routine_projects'] for a in p['activities'] if a['id']==self.aid)
        self.assertEqual(archived['display_status'],'awaiting_reply')
    def test_failed_review_and_retry_use_truthful_status(self):
        row=self.reject();self.assertTrue(guard.retry(self.store,self.aid))
        self.assertEqual(self.app.activity(self.aid)['activity']['display_status'],'preparing')
        with self.store.db(True) as db:db.execute("UPDATE jobs SET status='failed' WHERE kind='learning_review' AND target=? AND version=?",(self.aid,row['job_version']))
        self.assertEqual(self.app.activity(self.aid)['activity']['display_status'],'learning_failed')
    def test_native_failure_does_not_claim_to_be_preparing(self):
        self.store.set_meta('learning_processing:'+self.aid,dumps({'session_id':self.sid,'status':'failed','message':'生成失败，原文已保留。'}))
        self.assertEqual(self.app.activity(self.aid)['activity']['display_status'],'learning_failed')
    def test_other_routines_keep_their_business_presentation(self):
        for routine in ('A2','A3','CHAT'):
            a=self.store.ensure_activity(routine)
            detail=self.app.activity(a['id'])['activity']
            self.assertEqual(detail['display_status'],detail['status'])
if __name__=='__main__':unittest.main()
