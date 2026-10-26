"""Structured settings cannot be faked by a model's generic preference receipt."""
import datetime as dt
import json
import tempfile
import unittest
from dsh_personal.server import Application
from dsh_personal.core import TZ
from dsh_personal import directives
from dsh_personal.routine_settings import read_settings


class RoutineDirectiveBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.app=Application(self.temp.name,lambda:dt.datetime(2026,10,26,19,tzinfo=TZ))
        self.store=self.app.store;self.activity=self.store.ensure_activity('CHAT');self.counter=0

    def submit(self,body):
        self.counter+=1
        return self.store.submit(self.activity['id'],'directive-boundary-'+str(self.counter),body)['receipt']

    def request(self,mid):
        with self.store.db() as db:return dict(db.execute('SELECT * FROM directive_requests WHERE message_id=?',(mid,)).fetchone())

    def preference(self,body,scope,content=None):
        return {'intent':'apply','changes':[{'kind':'preference','action':'add','scope':scope,'item_id':None,'content':content or body,'quote':body}]}

    def test_explicit_structured_intent_gets_truthful_receipt_without_model(self):
        for body in ['以后每日一学聚焦劳动价值论。','请把每日一学的主题换成论语。',
                     '以后反思只谈健康。','我希望增加每日反思的健康方面。']:
            with self.subTest(body=body):
                mid=self.submit(body)
                self.assertTrue(directives.process(self.store,self.app.records,mid,lambda _:self.fail('no model needed')))
                request=self.request(mid);self.assertEqual(request['status'],'clarify')
                messages=self.store.activity(self.activity['id'])['messages']
                receipt=next(m for m in messages if m['id']==request['receipt_id'])
                self.assertIn('尚未',receipt['body']);self.assertNotIn('已保存',receipt['body'])
                self.assertTrue(directives.process(self.store,self.app.records,mid,lambda _:self.fail('durable retry')))
        self.assertEqual(self.app.records.list_profile(),[])
        with self.store.db() as db:
            self.assertEqual(read_settings(db,'A1')['version'],0)
            self.assertEqual(read_settings(db,'A2')['version'],0)

    def test_teaching_style_and_depth_remain_supported_preferences(self):
        for body,scope in [('每日一学以后详细讲政治经济学，并列出分析的适用条件。','A1'),
                           ('每日一学每个主题都要讲透，多用实例。','A1'),
                           ('以后反思请像参谋一样分析取舍。','A2')]:
            with self.subTest(body=body):
                mid=self.submit(body)
                self.assertTrue(directives.process(self.store,self.app.records,mid,lambda _:self.preference(body,scope)))
                self.assertEqual(self.request(mid)['status'],'applied')
        self.assertEqual(len(self.app.records.list_profile()),3)

    def test_quoted_and_question_requests_do_not_mutate_settings(self):
        for body in ['他说：“以后每日一学聚焦劳动价值论。”','每日反思只谈健康可以吗？',
                     '如果我说以后每日一学聚焦劳动价值论，会怎么样？']:
            with self.subTest(body=body):
                mid=self.submit(body)
                directives.process(self.store,self.app.records,mid,lambda _:{'intent':'none','changes':[]})
                self.assertEqual(self.request(mid)['status'],'none')
        self.assertEqual(self.app.records.list_profile(),[])

    def test_malformed_model_cannot_apply_structured_question_as_preference(self):
        body='每日一学的主题可以改为劳动价值论吗？';mid=self.submit(body)
        directives.process(self.store,self.app.records,mid,lambda _:self.preference(body,'A1'))
        self.assertEqual(self.request(mid)['status'],'clarify')
        self.assertEqual(self.app.records.list_profile(),[])

    def test_commit_boundary_prevents_structured_apply_with_other_changes(self):
        body='以后每日一学聚焦劳动价值论；以后回复先结论。';mid=self.submit(body)
        with self.store.db(True) as db:
            db.execute("INSERT INTO directive_requests VALUES(?,?,'pending',NULL,NULL,?,?)",(mid,self.activity['id'],self.store.stamp(),self.store.stamp()))
        snap=directives.snapshot(self.store,mid)
        decision=self.preference('以后每日一学聚焦劳动价值论','A1')
        decision['changes'].append(self.preference('以后回复先结论','global')['changes'][0])
        directives.commit(self.store,self.app.records,snap,decision)
        self.assertEqual(self.request(mid)['status'],'clarify')
        self.assertEqual(self.app.records.list_profile(),[])


if __name__=='__main__':unittest.main()
