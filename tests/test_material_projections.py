"""Source-derived history must stay dated, attributed and deletable."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from dsh_personal.core import Store, TZ, dumps
from dsh_personal.records import RecordsStore
from dsh_personal.chronicle import Chronicle
from dsh_personal.materials import revise_compiled, apply_plan
from types import SimpleNamespace
import hashlib


class MaterialProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.clock=lambda:dt.datetime(2026,10,23,12,tzinfo=TZ)
        self.store=Store(Path(self.temp.name)/'personal.sqlite',self.clock)
        self.records=RecordsStore(self.store.path,self.clock)
        self.records.import_apply('kimi',json.dumps({'id':'synthetic','messages':[
            {'id':'a','role':'user','content':'我完成了一个合成实验，并发现原先的方案需要修订。','created_at':None},
            {'id':'b','role':'source','content':'2026-10-12；此前来源整理，精确消息时间未知','created_at':None}]}))
        self.rows=self.records.imported_messages()
        self.user=next(x for x in self.rows if x['role']=='user')
        self.refs=['import:'+x['message_id'] for x in self.rows]
        self.date={'value':'2026-10-12','precision':'conversation_day','basis':'来源会话日期，非逐消息时间'}
        self.history=Chronicle(self.store,self.records)

    def profile(self,ident,content,scope,**kwargs):
        return self.records.add_profile(content,item_id=ident,scope=scope,source_refs=self.refs,**kwargs)

    def compiled(self):
        return self.profile('compiled',dumps({'format':'qiju-material-narrative-v1','title':'实验经历',
            'body':'{{user_title}}完成合成实验后，认为原方案仍需修订。','date':self.date}),'chronicle')

    def test_date_clue_filters_without_fabricating_original_timestamp(self):
        self.profile('date',dumps({'message_ids':[x['message_id'] for x in self.rows],'date':self.date}),'date_annotation')
        dated=self.records.imported_messages(day='2026-10-12',limit=1)
        self.assertEqual(len(dated),1);self.assertIsNone(dated[0]['original_at'])
        self.assertIsNone(dated[0]['original_date']);self.assertEqual(dated[0]['display_date'],'2026-10-12')
        self.assertEqual(self.records.imported_messages(day='unknown'),[])
        self.records.delete_imported(self.user['message_id'])
        remaining=self.records.imported_messages(day='unknown')
        self.assertEqual(len(remaining),1);self.assertIsNone(remaining[0]['date_evidence'])

    def test_compiled_narrative_suppresses_raw_projection_and_survives_user_edit(self):
        self.compiled();entries=self.history.entries(day='2026-10-12')
        self.assertEqual(len(entries),1);self.assertEqual(entries[0]['formation'],'agent_synthesis')
        self.assertIn('用户完成',entries[0]['body']);self.assertIsNone(self.history.automatic_snapshot(message_id=self.user['message_id']))
        revise_compiled(self.records,'compiled','用户后来更正了合成实验的记载。',1)
        self.assertEqual(self.history.entries()[0]['formation'],'manual_revision')
        with self.assertRaises(ValueError):revise_compiled(self.records,'compiled','过时覆盖',1)
        revise_compiled(self.records,'compiled','',2,True)
        self.assertEqual(self.history.entries(),[])

    def test_hidden_manual_entry_cannot_reappear_as_linked_compilation(self):
        manual=self.history.confirm_import_entry(self.user['message_id'],'用户完成合成实验。')
        self.profile('linked',dumps({'format':'qiju-material-narrative-v1','title':'重复事件',
            'body':'旧的自动正文','date':self.date,'linked_manual_profile_id':manual['item_id']}),'chronicle')
        self.assertEqual(len(self.history.entries()),1)
        self.history.confirm_import_entry(self.user['message_id'],'',expected_version=manual['version'],excluded=True)
        self.assertEqual(self.history.entries(),[])

    def test_compiled_context_keeps_other_day_visible_after_rewrite_edit_and_hide(self):
        self.profile('compiled',dumps({'format':'qiju-material-narrative-v1','title':'实验经历',
            'body':'旧稿','date':self.date}),'chronicle',user_input=dumps({'actor':'qiju_agent'}))
        self.records.import_apply('kimi',dumps({'id':'synthetic','messages':[
            {'id':'later','role':'user','content':'翌日参加了另一场报告会，并完成了会议笔记。',
             'created_at':'2026-10-13T10:00:00+08:00'}]}))
        later=next(row for row in self.records.imported_messages() if row['source_id']=='later')
        self.history.save_writing_style()
        snapshot=self.history.compiled_snapshot('compiled')
        self.assertIn('import:'+later['message_id'],snapshot['source_ids'])
        rewritten=self.history.commit_compiled_style(snapshot,'用户完成实验，发现旧方案仍有待修订。',
                                                     ['import:'+self.user['message_id']])
        self.assertEqual(rewritten['version'],2)
        self.assertIn('import:'+later['message_id'],rewritten['source_refs'])
        self.assertIsNone(self.history.compiled_snapshot('compiled'))
        def verify_later():
            self.assertTrue(self.history.automatic_snapshot(message_id=later['message_id']))
            self.assertTrue(any(row.get('import_id')==later['message_id']
                                for row in self.history.entries(day='2026-10-13')))
            self.assertIsNone(self.history.automatic_snapshot(message_id=self.user['message_id']))
        verify_later()
        revise_compiled(self.records,'compiled','用户更正了这条实验记录。',2)
        verify_later()
        revise_compiled(self.records,'compiled','',3,True)
        verify_later()

    def test_source_deletion_removes_compilation_and_activity_card_and_restore_cannot_revive(self):
        self.compiled();activity=self.store.ensure_activity('A3','2026-10-12')
        self.profile('sleep',dumps({'body':'合成睡眠记录','date_evidence':self.date}),'activity:'+activity['id'])
        snapshot=self.records.export_data()
        self.assertEqual(len(self.store.activity(activity['id'])['historical_records']),1)
        self.records.delete_imported(self.user['message_id'])
        self.assertEqual(self.history.entries(),[])
        self.assertEqual(self.store.activity(activity['id'])['historical_records'],[])
        latest=self.records.export_ledger()
        restored=RecordsStore(Path(self.temp.name)/'restore.sqlite',self.clock)
        restored.import_data(snapshot,latest_ledger=latest)
        self.assertEqual(restored.list_profile(include_inactive=True),[])

    def test_explicit_source_attested_preference_is_not_fake_human_confirmation(self):
        p=self.profile('preference','明确声明的合成偏好','global',category='preference',user_input=dumps({
            'actor':'qiju_agent','method':'material_compilation','context_eligible':True}))
        self.assertFalse(p['confirmed']);self.assertEqual(len(self.records.context()),1)
        self.records.disable_profile(p['item_id'],1,'用户停止使用该偏好')
        self.assertEqual(self.records.context(),[])

    def test_historical_source_card_never_creates_user_message_or_notification(self):
        activity=self.store.ensure_activity('A2','2026-10-12')
        self.profile('reflection',dumps({'body':'来源转录：当日曾拟定计划，未记完成。'}),'activity:'+activity['id'])
        view=self.store.activity(activity['id'])
        self.assertFalse(any(m['role']=='user' for m in view['messages']))
        with self.store.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM notifications').fetchone()[0],0)
        self.assertEqual(len(view['historical_records']),1)

    def test_plan_validates_sources_before_writing_and_preserves_user_revisions(self):
        source=self.user
        plan={'version':2,'modules':{'profiles':[{'id':'p','title':'合成偏好','content':'偏好有出处的讲解',
            'category':'preference','date':self.date,'context_eligible':True,'source_locations':[{
                'channel':source['channel'],'conversation_id':source['conversation_id'],'source_id':source['source_id'],
                'role':'user','content_sha256':hashlib.sha256(source['body'].encode()).hexdigest()}]}]}}
        app=SimpleNamespace(store=self.store,records=self.records)
        self.assertEqual(apply_plan(app,plan)['created'],0)
        result=apply_plan(app,plan,dry_run=False);ident=result['ids'][0]['id']
        self.records.revise_profile(ident,1,'用户修正',content='用户的新内容')
        self.assertEqual(apply_plan(app,plan,dry_run=False)['created'],0)
        self.assertEqual(self.records.get_profile(ident)['content'],'用户的新内容')
        plan['modules']['profiles'][0]['source_locations'][0]['role']='assistant'
        with self.assertRaises(ValueError):apply_plan(app,plan,dry_run=False)

    def test_quarantined_records_cannot_leak_through_compiled_history_or_activity_context(self):
        self.compiled();activity=self.store.ensure_activity('A3','2026-10-12')
        self.profile('sleep',dumps({'body':'隔离合成记录'}),'activity:'+activity['id'])
        self.profile('date',dumps({'message_ids':[self.user['message_id']],'date':self.date}),'date_annotation')
        snapshot=self.records.export_data()
        with self.store.db(True) as db:
            db.execute('DELETE FROM records_profile')
            db.execute('DELETE FROM records_import_messages')
        self.records.import_data(snapshot)
        self.assertTrue(self.records.is_quarantined())
        self.assertEqual(self.records.imported_messages(),[])
        self.assertEqual(self.records.date_annotations(),{})
        self.assertEqual(self.records.historical_records(activity['id']),[])
        self.assertEqual(self.store.activity(activity['id'])['historical_records'],[])
        self.assertEqual(self.history.entries(),[])
        from dsh_personal.server import Application
        app=Application(Path(self.temp.name),clock=self.clock)
        state=app.state()
        self.assertTrue(state['health']['isolated'])
        self.assertEqual(state.get('routine_projects',[]),[])
        with self.assertRaises(PermissionError):app.source('import:'+self.user['message_id'])


if __name__=='__main__':unittest.main()
