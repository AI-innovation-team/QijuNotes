"""Contracts joining project archives, source selection and the personal history."""
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from dsh_personal.server import Application


class ProductRevisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.app=Application(Path(self.tmp.name),clock=lambda:dt.datetime(2026,10,23,6,tzinfo=dt.timezone.utc))

    def material(self):
        return json.dumps({'id':'synthetic-conversation','messages':[
            {'id':'one','role':'user','content':'今天完成了合成材料的报告，明天计划开会。','created_at':'2026-10-19T22:32:00+08:00'},
            {'id':'two','role':'assistant','content':'可以考虑整理一个未来计划。','created_at':'2026-10-19T22:33:00+08:00'},
            {'id':'three','role':'user','content':'这是不想导入的另一条原话。'}]},ensure_ascii=False)

    def test_routine_archives_exceed_recent_limit_without_flattening(self):
        s=self.app.store
        for index in range(70):s.ensure_activity('A3',(dt.date(2026,10,23)-dt.timedelta(days=index)).isoformat())
        s.ensure_activity('CHAT');s.ensure_activity('A1');s.ensure_activity('A2')
        state=self.app.state()
        self.assertEqual(['CHAT'],[x['routine'] for x in state['recent_activities']])
        self.assertEqual(70,next(x['count'] for x in state['routine_projects'] if x['routine']=='A3'))
        self.assertEqual([],state['chronicle'])
        self.assertTrue(state['journal'])  # original automatic messages retained only in transcript

    def test_selection_is_bound_to_preview_and_unselected_text_not_persisted(self):
        params={'source':'kimi','content':self.material(),'filename':'selected.json'}
        first=self.app.action({'op':'import_preview',**params})
        selected=[first['items'][0]['message_id']]
        preview=self.app.action({'op':'import_preview',**params,'selected_message_ids':selected})
        with self.assertRaises(ValueError):self.app.action({'op':'import_apply',**params,'preview_token':preview['preview_token']})
        applied=self.app.action({'op':'import_apply',**params,'selected_message_ids':selected,'preview_token':preview['preview_token']})
        self.assertEqual(1,applied['counts']['new'])
        rows=self.app.records.imported_messages(limit=None)
        self.assertEqual(1,len(rows));self.assertEqual('user',rows[0]['role'])
        self.assertEqual('2026-10-19',rows[0]['original_date'])
        with self.app.store.db() as c:
            report=c.execute('SELECT report FROM records_import_batches').fetchone()[0]
            self.assertNotIn('不想导入',report);self.assertNotIn('items',json.loads(report))
        self.assertEqual('recorded',self.app.state()['chronicle'][0]['status'])

    def test_selection_invalid_ids_and_empty_rejected(self):
        for selected in ([],['missing'],[1]):
            with self.assertRaises(ValueError):self.app.records.import_preview('kimi',self.material(),selected_message_ids=selected)

    def test_history_edit_does_not_pollute_summary_or_model_context(self):
        a=self.app.store.ensure_activity('A3')['id']
        receipt=self.app.store.submit(a,'synthetic-revision-one','昨晚睡得一般，今天计划早点休息。')
        self.app.action({'op':'chronicle_revise','activity_id':a,'body':'本人记下这一晚的睡眠感受。','source_ids':[receipt['receipt']],'expected_version':0})
        summary=self.app.store.summarize_literal(a)
        self.assertTrue(summary)
        self.assertNotIn('chronicle_entry',self.app.store.activity(a)['summary']['fields'])
        payload=self.app.worker.contextual_input(a,'继续谈睡眠')
        self.assertNotIn('chronicle_entry',payload)
        self.assertIn('个人史秉笔史官',payload)

    def test_calendar_plans_are_not_completed_personal_history(self):
        event=self.app.action({'op':'calendar_save','submission_id':'synthetic-calendar-contract',
                              'title':'合成报告交流','start_at':'2026-10-24T10:00:00+08:00','end_at':'2026-10-24T11:00:00+08:00'})
        self.assertEqual([],self.app.history())
        self.app.action({'op':'calendar_status','event_id':event['id'],'expected_version':1,'status':'completed'})
        entries=self.app.history()
        self.assertEqual(1,len(entries));self.assertEqual('2026-10-23',entries[0]['business_date'])
        self.assertIn('确认完成日程',entries[0]['body'])
        self.assertEqual(event['id'],self.app.source('calendar:'+event['id'])['id'])

    def test_restore_isolation_blocks_new_mutations_and_history(self):
        self.app.store.set_meta('isolated','true')
        for op in ('chronicle_settings','chronicle_revise','calendar_save','calendar_status'):
            with self.assertRaises(PermissionError):self.app.action({'op':op})
        with self.assertRaises(PermissionError):self.app.history()
        self.assertEqual([],self.app.state()['activities'])
