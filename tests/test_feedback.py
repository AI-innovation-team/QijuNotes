import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from dsh_personal.core import Store, TZ
from dsh_personal import feedback


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name)/'personal.sqlite', lambda: dt.datetime(2026,10,25,12,tzinfo=TZ))
        self.aid = self.store.ensure_activity('CHAT')['id']

    def tearDown(self):
        self.tmp.cleanup()

    def submit(self, body='反馈：每日一学回复太慢，而且仍有防御套话。'):
        return self.store.submit(self.aid,'stable-feedback-input',body)

    def test_immediate_receipt_precedes_model_and_duplicate_is_idempotent(self):
        result = self.submit()
        self.assertTrue(self.submit()['duplicate'])
        rows = feedback.view(self.store)['feedback']
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['message_id'],result['receipt'])
        self.assertEqual(rows[0]['status'],'recorded')
        messages = self.store.activity(self.aid)['messages']
        receipts = [m for m in messages if m['id'].startswith('feedback-receipt:')]
        self.assertEqual(len(receipts),1)
        self.assertEqual(receipts[0]['processing_state'],'local_control')
        self.assertIn('待排查',receipts[0]['body'])
        self.assertNotIn('已修复',receipts[0]['body'])

    def test_quoted_feedback_and_academic_critique_do_not_create_reports(self):
        for text in ['> 反馈：网站太慢','“反馈：网站太慢”是什么意思？','负反馈为什么会导致神经活动减弱？','批评形而上学有哪些方法？']:
            self.assertFalse(feedback.is_feedback(text),text)
        self.assertTrue(feedback.is_feedback('每日一学怎么不回我的话？'))

    def test_source_deletion_scrubs_feedback_and_cannot_resolve(self):
        result = self.submit()
        row = feedback.view(self.store)['feedback'][0]
        from dsh_personal.deletion import apply_message_deletion
        with self.store.db(True) as db:
            apply_message_deletion(db,result['receipt'],self.store.stamp())
        self.assertEqual(feedback.view(self.store),{'feedback':[]})
        self.assertNotIn('防御套话',self.store.meta(feedback.PREFIX+row['id']))
        with self.assertRaises(ValueError):
            feedback.resolve(self.store,row['id'],'已验证',['fixture'])

    def test_restore_with_missing_source_scrubs_even_without_live_cascade(self):
        self.submit()
        row = feedback.view(self.store)['feedback'][0]
        with self.store.db(True) as db:
            db.execute('DELETE FROM messages WHERE id=?',(row['message_id'],))
            feedback.scrub(db,self.store.stamp())
        self.assertEqual(json.loads(self.store.meta(feedback.PREFIX+row['id']))['status'],'deleted')

    def test_resolve_requires_evidence_and_is_not_a_public_action(self):
        self.submit()
        row = feedback.view(self.store)['feedback'][0]
        with self.assertRaises(ValueError):
            feedback.resolve(self.store,row['id'],'模型说它修好了',[])
        with self.assertRaises(ValueError):
            feedback.resolve(self.store,row['id'],'模型说它修好了','fixture')
        with self.assertRaises(ValueError):
            feedback.resolve(self.store,row['id'],'模型说它修好了',['   '])
        result = feedback.resolve(self.store,row['id'],'实际用例已核对',['isolated test fixture'])
        self.assertEqual(result['status'],'resolved')
        self.assertEqual(feedback.view(self.store)['feedback'][0]['resolution'],'实际用例已核对')
        self.store.set_meta('isolated','true')
        self.assertEqual(feedback.view(self.store),{'feedback':[]})

    def test_deleting_unrelated_message_keeps_original_feedback_and_receipt(self):
        self.submit()
        row=feedback.view(self.store)['feedback'][0]
        other=self.store.submit(self.aid,'another-unrelated-input','另一个随手想法。')
        from dsh_personal.deletion import apply_message_deletion
        with self.store.db(True) as db:
            apply_message_deletion(db,other['receipt'],self.store.stamp())
        self.assertEqual(feedback.view(self.store)['feedback'][0]['id'],row['id'])
        self.assertTrue(any(m['id']==row['receipt_id'] for m in self.store.activity(self.aid)['messages']))

    def test_newer_resolution_ledger_merges_into_old_snapshot(self):
        import sqlite3
        self.submit()
        row=feedback.view(self.store)['feedback'][0]
        with self.store.db() as db,sqlite3.connect(Path(self.tmp.name)/'old.sqlite') as target:
            db.backup(target)
        feedback.resolve(self.store,row['id'],'已有实际回读',['release/readback'])
        with self.store.db() as db:
            entries=feedback.ledger(db)
        self.assertNotIn('body',entries[0])
        old=Store(Path(self.tmp.name)/'old.sqlite')
        with old.db(True) as db:
            feedback.recover(db,entries,old.stamp())
        recovered=feedback.view(old)['feedback'][0]
        self.assertEqual(recovered['status'],'resolved')
        self.assertEqual(recovered['evidence'],['release/readback'])

    def test_equal_timestamp_old_ledger_never_reverses_resolution(self):
        self.submit()
        row=feedback.view(self.store)['feedback'][0]
        with self.store.db() as db:
            entries=feedback.ledger(db)
        feedback.resolve(self.store,row['id'],'确切验证通过',['test-case'])
        with self.store.db(True) as db:
            feedback.recover(db,entries,self.store.stamp())
        self.assertEqual(feedback.view(self.store)['feedback'][0]['status'],'resolved')


if __name__ == '__main__':
    unittest.main()
