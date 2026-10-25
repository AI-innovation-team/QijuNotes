"""End-to-end deletion in isolated owned namespaces, with unrelated originals."""
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from dsh_personal.server import Application
from dsh_personal.deletion import compact_deleted_data
from dsh_personal.operations import export_bundle,restore_bundle,recovery_ledger
from dsh_personal.worker import Worker

class Bridge:
    def ensure_session(self,key):return str(uuid.uuid5(uuid.NAMESPACE_URL,key))

class DeletionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.app=Application(self.root/'data',lambda:dt.datetime(2026,10,22,12,tzinfo=dt.timezone.utc))
        self.s=self.app.store;self.a=self.s.ensure_activity('A3','2026-10-20')
        self.mid=self.s.submit(self.a['id'],'delete-fixture','DELETE_FIXTURE_abc')['receipt']
        self.keep=self.s.submit(self.a['id'],'keep-fixture','KEEP_USER_ORIGINAL_xyz')['receipt']
        self.sid=self.native(self.a['id'],'DELETE_FIXTURE_abc')
    def native(self,aid,body):
        sid=str(uuid.uuid4());folder=self.app.data/'dsh/sessions/--fixture--'/sid;folder.mkdir(parents=True)
        (folder/'session.v4.jsonl').write_text(json.dumps({'type':'session','version':4,'id':sid})+'\n'+json.dumps({'fixture':body})+'\n')
        with self.s.db(True) as c:c.execute("UPDATE activities SET session_id=?,session_state='ready' WHERE id=?",(sid,aid))
        return sid
    def assistant(self,aid,mid,body):
        with self.s.db(True) as c:c.execute("INSERT INTO messages VALUES(?,?,NULL,'assistant',?,?,'native',?,NULL,0)",(mid,aid,body,self.s.stamp(),mid))
    def delete(self,**kw):return self.app.action({'op':'delete_message','message_id':self.mid,**kw})
    def test_native_cascade_keeps_other_originals_and_continues_new_context(self):
        self.assistant(self.a['id'],'old-assistant','quotes DELETE_FIXTURE_abc')
        self.s.correct(self.a['id'],{'quality':'different independent correction','independent':'retain this correction'},'explicit correction')
        other=self.s.ensure_activity('A2','2026-10-19');other_sid=self.native(other['id'],'unrelated')
        before=(self.app.data/'dsh/sessions/--fixture--'/other_sid/'session.v4.jsonl').read_bytes()
        plan=self.app.action({'op':'deletion_plan','message_id':self.mid})
        result=self.delete(plan_token=plan['token']);self.assertFalse(result['native_cleanup_pending']);self.assertFalse(result['sqlite_cleanup_pending'])
        self.assertFalse((self.app.data/'dsh/sessions/--fixture--'/self.sid).exists())
        self.assertEqual((self.app.data/'dsh/sessions/--fixture--'/other_sid/'session.v4.jsonl').read_bytes(),before)
        view=self.s.activity(self.a['id']);self.assertIn(self.keep,{m['id'] for m in view['messages']});self.assertNotIn('old-assistant',{m['id'] for m in view['messages']})
        self.assertIn('retain this correction',json.dumps(view['corrections']));self.assertNotIn('DELETE_FIXTURE',json.dumps(view))
        w=Worker(self.s,Bridge(),self.app.records,lambda:{})
        new_sid=w.ensure_session(self.a['id']);self.assertNotEqual(new_sid,self.sid)
        w.sync_summary_refs(self.a['id'])
        new=self.s.submit(self.a['id'],'new-fixture','continue please')['receipt']
        self.assistant(self.a['id'],'new-assistant','new answer')
        text=w.contextual_input(self.a['id'],'continue please',new)
        self.assertIn('KEEP_USER_ORIGINAL_xyz',text);self.assertNotIn('DELETE_FIXTURE',text)
        self.delete();self.assertIn('new-assistant',{m['id'] for m in self.s.activity(self.a['id'])['messages']})
        # A current backup already containing the deletion must not delete new work.
        restore_bundle(export_bundle(self.app),self.root/'current-restore',recovery_ledger(self.app))
        restored=Application(self.root/'current-restore')
        self.assertIn('new-assistant',{m['id'] for m in restored.store.activity(self.a['id'])['messages']})
    def test_short_original_does_not_rewrite_independent_user_fields(self):
        with self.s.db(True) as c:c.execute('UPDATE messages SET body=? WHERE id=?',('好',self.mid))
        self.s.correct(self.a['id'],{'quality':'身体还好，但昨晚睡眠较差'},'好好核对')
        t=self.s.create_task('把课题做好',source_activity_id=self.a['id'])
        self.delete()
        self.assertIn('身体还好',json.dumps(self.s.activity(self.a['id'])['corrections'],ensure_ascii=False))
        self.assertEqual(self.s.task(t['id'])['goal'],'把课题做好')
        with self.s.db() as c:
            self.assertEqual(c.execute('SELECT processing_state FROM messages WHERE id=?',(self.keep,)).fetchone()[0],'deletion_hold')
        self.s.submit(self.a['id'],'continue-stable-activity','新的研究记录')
        Worker(self.s,Bridge(),self.app.records,lambda:{}).sync_summary_refs(self.a['id'])
        self.app.records.add_profile('new independent confirmed fact',source_refs=['activity:'+self.a['id']])
    def test_stale_plan_refused_before_deleting(self):
        plan=self.app.action({'op':'deletion_plan','message_id':self.mid})
        self.s.submit(self.a['id'],'later-fixture','new independent data')
        with self.assertRaisesRegex(ValueError,'重新核对'):self.delete(plan_token=plan['token'])
        self.assertIn(self.mid,{m['id'] for m in self.s.activity(self.a['id'])['messages']})
    def test_profile_context_cross_activity_and_delegation_follow_deletion(self):
        profile=self.app.records.add_profile('profile derived from original',source_refs=['message:'+self.mid],confirmed=True,user_input='fixture confirmed')
        other=self.s.ensure_activity('A2','2026-10-19');sid=self.native(other['id'],'profile derived from original')
        self.s.submit(other['id'],'other-user','independent retained')
        self.assistant(other['id'],'related-assistant','derived profile inference')
        w=Worker(self.s,Bridge(),self.app.records,lambda:{})
        w.contextual_input(other['id'],'independent retained')
        plan=self.app.action({'op':'deletion_plan','message_id':self.mid})
        self.assertIn(sid,plan['native_session_ids']);self.assertIn('profile:'+profile['item_id'],plan['source_refs'])
        result=self.delete(plan_token=plan['token']);self.assertFalse(result['native_cleanup_pending'])
        self.assertFalse(self.app.records.context())
        self.assertNotIn('related-assistant',{m['id'] for m in self.s.activity(other['id'])['messages']})
        self.assertIn('independent retained',json.dumps(self.s.activity(other['id'])))
    def test_busy_reader_exposes_pending_then_compacts(self):
        reader=sqlite3.connect(self.s.path);self.addCleanup(reader.close)
        reader.execute('BEGIN');reader.execute('SELECT * FROM messages').fetchall()
        result=self.delete();self.assertTrue(result['sqlite_cleanup_pending'])
        reader.close();self.assertTrue(compact_deleted_data(self.s));self.assertFalse(self.delete()['sqlite_cleanup_pending'])
        self.assertNotIn(b'DELETE_FIXTURE_abc',self.s.path.read_bytes())
    def test_unknown_native_layout_isolated_and_export_refused(self):
        (self.app.data/'dsh/sessions/--fixture--'/self.sid/'unknown-copy.txt').write_text('DELETE_FIXTURE_abc')
        result=self.delete();self.assertTrue(result['native_cleanup_pending'])
        self.assertEqual(self.s.activity(self.a['id'])['activity']['session_state'],'deletion_pending')
        with self.assertRaises(RuntimeError):export_bundle(self.app)
        (self.app.data/'dsh/sessions/--fixture--'/self.sid/'unknown-copy.txt').unlink()
        self.assertFalse(self.delete()['native_cleanup_pending'])

if __name__=='__main__':unittest.main()
