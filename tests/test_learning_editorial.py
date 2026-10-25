"""Editorial recovery must preserve old text and reject stale source snapshots."""
import datetime as dt
import unittest

from dsh_personal import learning_guard as guard, learning_editorial as editor
from dsh_personal.learning_content import (build_packet,audit_payload,reference_audit_payload,
    premise_payload,COMBINED_REFERENCE_SYSTEM,AUDIT_REFERENCE_SYSTEM,PREMISE_REFERENCE_SYSTEM)
from tests import test_learning_worker_guard as fixtures


class LearningEditorialTests(unittest.TestCase):
    def setUp(self):
        self.h=fixtures.LearningWorkerGuardTests();self.h.setUp();self.s=self.h.store

    def tearDown(self):
        self.h.tearDown()

    def reviews(self,body,mode='lesson',users=None):
        payload=audit_payload(build_packet(self.h.manifest,users or []),body,mode)
        stages=[('full',COMBINED_REFERENCE_SYSTEM if mode=='followup' else AUDIT_REFERENCE_SYSTEM,payload)]
        focused=premise_payload(payload) if mode=='lesson' else None
        if focused:stages.append(('premise',PREMISE_REFERENCE_SYSTEM,focused))
        return [dict(stage=stage,policy_sha256=editor.sha(system),result=fixtures.LearningWorkerGuardTests.audit(reference_audit_payload(p)))
                for stage,system,p in stages]

    def original(self):
        candidate=self.h.initial_analysis('可以比较两项规则的条件。')
        self.h.worker.learning_model=lambda system,payload,repair=False:fixtures.LearningWorkerGuardTests.audit(payload)
        self.assertTrue(self.h.worker.review_learning(self.h.aid,candidate['job_version'])['published'])
        with self.s.db() as db:
            message=dict(db.execute('SELECT * FROM messages WHERE id=?',(candidate['message_id'],)).fetchone())
            preferences_hash=guard._preference_hash(guard._profiles(db,self.s.today()))
        body=self.h.lesson_text('先列出比较的条件，再讨论规则的差异。')
        return message,body,self.reviews(body),preferences_hash

    def publish(self,original,body,reviews,prefs):
        return editor.publish_lesson_correction(self.s,original['id'],body,reviews,
            expected_original_sha256=editor.sha(original['body']),expected_preferences_hash=prefs)

    def test_append_version_preserves_original_and_is_idempotent(self):
        original,body,reviews,prefs=self.original()
        self.h.now+=dt.timedelta(minutes=10)
        row=self.publish(original,body,reviews,prefs)
        self.assertEqual(row['version'],2)
        self.assertEqual(self.publish(original,body,reviews,prefs),row)
        messages=self.s.activity(self.h.aid)['messages']
        self.assertEqual(next(m for m in messages if m['id']==original['id']),original)
        self.assertEqual(len([m for m in messages if m['processing_state']=='learning_edited']),1)
        self.assertGreater(row['created_at'],original['received_at'])

    def test_changed_preferences_block_completed_audit(self):
        original,body,reviews,prefs=self.original()
        self.h.app.records.add_profile('学习内容请更深入。',category='preference',scope='routine:A1',confirmed=True,user_input='学习内容请更深入。')
        with self.assertRaises(ValueError):self.publish(original,body,reviews,prefs)

    def test_records_quarantine_and_deleted_original_block_correction(self):
        original,body,reviews,prefs=self.original()
        with self.s.db(True) as db:db.execute("INSERT OR REPLACE INTO records_meta VALUES('quarantine','1')")
        with self.assertRaises(ValueError):self.publish(original,body,reviews,prefs)
        with self.s.db(True) as db:
            db.execute("DELETE FROM records_meta WHERE key='quarantine'")
            db.execute("UPDATE messages SET deleted=1 WHERE id=?",(original['id'],))
        with self.assertRaises(ValueError):self.publish(original,body,reviews,prefs)

    def test_tampered_source_or_audit_cannot_publish(self):
        original,body,reviews,prefs=self.original()
        reviews[0]['result']['draft_sha256']='0'*64
        with self.assertRaises(ValueError):self.publish(original,body,reviews,prefs)

    def test_deletion_scrubs_revision_body_and_private_audit(self):
        original,body,reviews,prefs=self.original()
        row=self.publish(original,body,reviews,prefs)
        from dsh_personal.deletion import apply_message_deletion
        with self.s.db(True) as db:apply_message_deletion(db,original['id'],self.s.stamp())
        self.assertIsNone(self.s.meta('learning_editorial:'+original['id']))
        self.assertIsNone(self.s.meta('learning_editorial_audit:'+row['message_id']))
        self.assertFalse(any(m['id']==row['message_id'] for m in self.s.activity(self.h.aid)['messages']))

    def test_exact_followup_candidate_does_not_queue_newer_rejection(self):
        old,mid=self.h.followup('收到，可以继续追问。')
        token=guard.record_attempt(self.s,old['id'])['attempt_token']
        guard.commit(self.s,old['id'],False,attempt_token=token)
        newer=guard.enqueue(self.s,self.h.activity(),{'session_id':self.h.sid,'end_seq':33,'assistant_seq':32},
            '新的未发布候选。',self.h.manifest,[],user_message_ids=[mid],mode='followup')
        token=guard.record_attempt(self.s,newer['id'])['attempt_token']
        guard.commit(self.s,newer['id'],False,attempt_token=token)
        before=guard.snapshot(self.s,newer['id'])
        with self.s.db() as db:users=[dict(db.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone())]
        body='### 理论分析\n\n假设甲乙采用不同规则，可以先逐项比较条件。'
        result=editor.publish_followup(self.s,old['id'],body,self.reviews(body,'followup',users),expected_body_sha256=editor.sha(old['draft']))
        self.assertTrue(result['published'])
        self.assertEqual(guard.snapshot(self.s,newer['id']),before)
        self.assertTrue(any(a['result'].get('previous_body')==old['draft'] for a in guard.snapshot(self.s,old['id'])['audit']))
        with self.assertRaises(ValueError):editor.publish_followup(self.s,old['id'],body,self.reviews(body,'followup',users),expected_body_sha256=editor.sha(old['draft']))


if __name__=='__main__':unittest.main()
