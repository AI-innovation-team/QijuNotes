"""Prepared lessons stay internal across real HTTP, exports and recovery."""
import datetime as dt
from hashlib import sha256
import http.client
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.parse
import zipfile

from dsh_personal.core import TZ
from dsh_personal.learning_schedule import initialize, is_hidden
from dsh_personal.operations import (_database_json, export_bundle, markdown_export,
    recovery_ledger, restore_bundle)
from dsh_personal.server import Application, create_server


class LearningScheduleVisibilityTests(unittest.TestCase):
    PRIVATE='INTERNAL-UNPUBLISHED-LESSON-ONLY'
    VISIBLE='PUBLIC-EXISTING-CONVERSATION'

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.now=dt.datetime(2026,10,24,10,tzinfo=TZ)
        self.app=Application(self.root/'instance',clock=lambda:self.now)
        self.store=self.app.store
        self.hidden=self.store.ensure_activity('A1')
        self.visible=self.store.ensure_activity('CHAT')
        self.asset='hidden-asset'
        with self.store.db(True) as db:
            initialize(db)
            db.execute('INSERT INTO learning_releases VALUES(?,?,NULL,?,?,NULL)',
                       (self.hidden['id'],'2026-10-24T21:00:00+08:00','schedule-fixture',self.store.stamp()))
            db.execute('UPDATE activities SET bookmarked=1,session_id=? WHERE id=?',('hidden-native',self.hidden['id']))
            db.execute('UPDATE messages SET body=? WHERE activity_id=?',(self.PRIVATE,self.hidden['id']))
            self.mid=db.execute('SELECT id FROM messages WHERE activity_id=?',(self.hidden['id'],)).fetchone()[0]
            db.execute('UPDATE messages SET body=? WHERE activity_id=?',(self.VISIBLE,self.visible['id']))
            db.execute('INSERT INTO attachments VALUES(?,?,NULL,?,?,?,?,?,?)',
                       (self.asset,self.hidden['id'],self.mid,'hidden.txt','attachments/'+self.asset,sha256(self.PRIVATE.encode()).hexdigest(),len(self.PRIVATE),self.store.stamp()))
        folder=self.app.data/'attachments';folder.mkdir()
        (folder/self.asset).write_text(self.PRIVATE)
        from dsh_personal.learning import prepare_learning
        manifest=prepare_learning(self.store,self.hidden['id'])['manifest']
        manifest['fixture_private_note']=self.PRIVATE
        self.store.set_meta('learning_selection:'+self.hidden['id'],json.dumps(manifest))
        self.profile=self.app.records.add_profile(self.PRIVATE,'background',source_refs=['activity:'+self.hidden['id']])
        self.app.records.put_growth('doctorate','action',self.PRIVATE,source_refs=['message:'+self.mid])

    def server(self):
        server=create_server(self.app,port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def stop():
            self.app.sessions.clear();server.shutdown();server.server_close();thread.join(timeout=2)
        self.addCleanup(stop)
        self.app.sessions['test-fixture-session']=time.time()+60
        return server.server_address[1]

    def request(self,port,path,payload=None):
        conn=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
        headers={'Cookie':'dsh_session=test-fixture-session'}
        if payload is not None:headers.update({'X-DSH-Client':'web','Content-Type':'application/json'})
        try:
            conn.request('GET' if payload is None else 'POST',path,None if payload is None else json.dumps(payload),headers)
            response=conn.getresponse();body=response.read()
            return response.status,body
        finally:conn.close()

    def test_state_sidebar_bookmarks_history_and_provenance_are_hidden(self):
        for view in ('full','chat'):
            state=self.app.state(view=view)
            text=json.dumps(state)
            self.assertNotIn(self.hidden['id'],text)
            self.assertNotIn(self.PRIVATE,text)
            self.assertIn(self.visible['id'],text)
        self.assertEqual(self.app.bookmarks(),[])
        for ref in ('activity:'+self.hidden['id'],'message:'+self.mid,'profile:'+self.profile['item_id']):
            with self.subTest(ref=ref),self.assertRaises(KeyError):self.app.source(ref)

    def test_real_get_and_write_endpoints_do_not_accept_guessed_ids(self):
        port=self.server()
        for path in ('/api/activity?id='+self.hidden['id'],'/api/source?ref='+urllib.parse.quote('message:'+self.mid),'/api/attachment?id='+self.asset):
            with self.subTest(path=path):
                code,body=self.request(port,path)
                self.assertEqual(code,400)
                self.assertNotIn(self.PRIVATE.encode(),body)
        commands=[
            {'op':'message','activity_id':self.hidden['id'],'submission_id':'fixture-submission','body':'guessed input'},
            {'op':'activity_control','activity_id':self.hidden['id'],'action':'bookmark','value':False},
            {'op':'correction','activity_id':self.hidden['id'],'fields':{'test':'guessed'}},
            {'op':'retry','activity_id':self.hidden['id']},
            {'op':'presented','activity_id':self.hidden['id'],'lease':'unknown','visible':True},
            {'op':'delete_message','message_id':self.mid},
            {'op':'deletion_plan','message_id':self.mid},
            {'op':'profile_save','content':'guessed','source_refs':['message:'+self.mid]},
            {'op':'profile_save','content':'guessed','source_refs':'activity:'+self.hidden['id']},
            {'op':'profile_disable','item_id':self.profile['item_id']},
            {'op':'task_create','source_activity_id':self.hidden['id']},
            {'op':'attachment_upload','activity_id':self.hidden['id']},
        ]
        for payload in commands:
            with self.subTest(op=payload['op']):self.assertEqual(self.request(port,'/api/action',payload)[0],400)
        with self.store.db() as db:
            self.assertFalse(db.execute('SELECT 1 FROM messages WHERE submission_id=?',('fixture-submission',)).fetchone())
            self.assertEqual(db.execute('SELECT deleted FROM messages WHERE id=?',(self.mid,)).fetchone()[0],0)

    def test_manual_open_today_is_the_explicit_visibility_transition(self):
        with self.store.db() as db:self.assertTrue(is_hidden(db,self.hidden['id']))
        result=self.app.action({'op':'ensure_activity','routine':'A1','date':'2026-10-24'})
        self.assertEqual(result['activity']['id'],self.hidden['id'])
        self.assertEqual(self.app.activity(self.hidden['id'])['activity']['id'],self.hidden['id'])
        with self.assertRaises(ValueError):self.app.action({'op':'ensure_activity','routine':'A1','date':'2026-10-25'})

    def test_cancelled_or_paused_preparation_remains_hidden(self):
        with self.store.db(True) as db:
            db.execute("UPDATE learning_releases SET reason='schedule_changed' WHERE activity_id=?",(self.hidden['id'],))
        self.assertNotIn(self.hidden['id'],json.dumps(self.app.state(view='chat')))
        with self.assertRaises(KeyError):self.app.activity(self.hidden['id'])

    def test_batch_retry_cannot_resume_private_preparation(self):
        with self.store.db(True) as db:db.execute("UPDATE jobs SET status='failed' WHERE target=?",(self.hidden['id'],))
        self.app.action({'op':'retry'})
        with self.store.db() as db:
            self.assertEqual({r[0] for r in db.execute('SELECT status FROM jobs WHERE target=?',(self.hidden['id'],))},{'failed'})

    def test_sse_does_not_announce_unreleased_lesson_even_if_a_stale_notice_exists(self):
        with self.store.db(True) as db:
            db.execute('UPDATE activities SET ready_at=? WHERE id=?',(self.store.stamp(),self.hidden['id']))
            for aid in (self.hidden['id'],self.visible['id']):
                db.execute("INSERT INTO notifications VALUES(?,?, 'first','pending',?,0,NULL,NULL,?)",(aid,aid,self.store.stamp(),self.store.stamp()))
        port=self.server()
        conn=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
        try:
            conn.request('GET','/api/events',headers={'Cookie':'dsh_session=test-fixture-session'})
            response=conn.getresponse();self.assertEqual(response.status,200)
            lines=[]
            for _ in range(20):
                line=response.fp.readline();lines.append(line)
                if line.startswith(b': connected'):break
            data=b''.join(lines)
            self.assertNotIn(self.hidden['id'].encode(),data)
            self.assertIn(self.visible['id'].encode(),data)
        finally:
            self.app.sessions.clear();conn.close()

    def test_readable_exports_hide_preparation_complete_migration_preserves_it(self):
        encoded=json.dumps(_database_json(self.store.path))
        self.assertNotIn(self.PRIVATE,encoded)
        self.assertNotIn(self.hidden['id'],encoded)
        self.assertIn(self.VISIBLE,encoded)
        self.assertNotIn(self.PRIVATE,markdown_export(self.app))
        content=export_bundle(self.app)
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for name in ('data.json','records.json','diary.md'):
                with self.subTest(name=name):self.assertNotIn(self.PRIVATE.encode(),archive.read(name))
            self.assertIn(self.PRIVATE.encode(),archive.read('personal.sqlite'))
            self.assertIn('private complete migration',json.loads(archive.read('manifest.json'))['privacy'])
        ledger=recovery_ledger(self.app)
        self.assertEqual(ledger['core']['learning_releases'][0]['activated_at'],None)

    def test_restore_newer_release_metadata_cannot_publish_an_older_private_snapshot(self):
        content=export_bundle(self.app)
        with self.store.db(True) as db:
            db.execute('UPDATE learning_releases SET activated_at=? WHERE activity_id=?',(self.store.stamp(),self.hidden['id']))
        latest=recovery_ledger(self.app)
        target=self.root/'restored'
        restore_bundle(content,target,latest)
        with sqlite3.connect(target/'personal.sqlite') as db:
            self.assertIsNone(db.execute('SELECT activated_at FROM learning_releases WHERE activity_id=?',(self.hidden['id'],)).fetchone()[0])
            self.assertEqual(db.execute('SELECT reason FROM learning_releases WHERE activity_id=?',(self.hidden['id'],)).fetchone()[0],'recovery_hold')
        for name in ('data.json','records.json','diary.md'):
            with self.subTest(name=name):self.assertNotIn(self.PRIVATE,(target/name).read_text())

    def test_visible_snapshot_keeps_its_actual_activation_during_restore(self):
        with self.store.db(True) as db:db.execute('UPDATE learning_releases SET activated_at=?',(self.store.stamp(),))
        content=export_bundle(self.app)
        target=self.root/'visible-restore'
        restore_bundle(content,target)
        with sqlite3.connect(target/'personal.sqlite') as db:
            self.assertEqual(db.execute('SELECT activated_at FROM learning_releases').fetchone()[0],self.store.stamp())

    def test_older_database_without_release_table_stays_compatible(self):
        with self.store.db(True) as db:
            db.execute('DROP TABLE learning_releases')
            self.assertFalse(is_hidden(db,self.hidden['id']))
        self.app.require_visible('activity:'+self.hidden['id'])
        self.assertIn(self.PRIVATE,json.dumps(_database_json(self.store.path)))


if __name__=='__main__':unittest.main()
