import datetime as dt
import json
import tempfile
import unittest
from unittest.mock import patch
from dsh_personal.server import Application
from dsh_personal.core import TZ
from dsh_personal import directives as d
from dsh_personal.task_requirements import snapshot as task_snapshot
from tests.test_worker import Bridge


class DirectivesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Application(self.temp.name, lambda: dt.datetime(2026, 10, 23, 22, tzinfo=TZ))
        self.s, self.r = self.app.store, self.app.records
        self.a = self.s.ensure_activity('CHAT')
        self.b = Bridge(); self.app.worker.bridge = self.b
        self.count = 0

    def tearDown(self): self.temp.cleanup()

    def send(self, text, aid=None):
        self.count += 1
        return self.s.submit(aid or self.a['id'], 'directive-test-'+str(self.count), text)['receipt']

    def preference(self, body, scope='global', **kwargs):
        return {'intent':'apply','continue_chat':False,'changes':[{'kind':'preference','action':'add','scope':scope,'item_id':None,'content':'先给结论，再提供必要解释。','quote':body,**kwargs}]}

    def request(self, mid):
        with self.s.db() as c:
            row=c.execute('SELECT * FROM directive_requests WHERE message_id=?',(mid,)).fetchone()
            return dict(row) if row else None

    def test_atomic_receipt_repeated_delivery_and_restart(self):
        body='以后请先给结论，作为长期回复偏好。'; mid=self.send(body)
        self.assertTrue(d.process(self.s,self.r,mid,lambda _: self.preference(body)))
        req=self.request(mid); self.assertEqual(req['status'],'applied')
        self.assertIn('已保存',next(m['body'] for m in self.s.activity(self.a['id'])['messages'] if m['id']==req['receipt_id']))
        profiles=self.r.context(); self.assertEqual(len(profiles),1)
        reloaded=Application(self.temp.name,lambda: self.s.now())
        self.assertTrue(d.process(reloaded.store,reloaded.records,mid,lambda _: self.fail('must not reinterpret')))
        self.assertEqual(len(reloaded.records.list_profile(history=True)),1)
        with self.s.db() as c:self.assertEqual(c.execute("SELECT count(*) FROM messages WHERE id LIKE 'directive-receipt:%'").fetchone()[0],1)

    def test_scope_future_activity_and_existing_session_rotation(self):
        a1=self.s.ensure_activity('A1'); a2=self.s.ensure_activity('A2')
        w=self.app.worker; first=w.ensure_session(a1['id'])
        body='每日一学以后详细讲政治经济学，并列出分析的适用条件。';mid=self.send(body)
        self.assertTrue(d.process(self.s,self.r,mid,lambda _: self.preference(body,'A1',content='政治经济学要讲适用条件。')))
        self.assertEqual(d.effective_context(self.r,'A2'),[])
        self.assertIn('政治经济学',d.requirements_text(d.effective_context(self.r,'A1'),'A1'))
        self.assertNotEqual(first,w.ensure_session(a1['id']))
        self.assertNotIn('政治经济学要讲适用条件',w.contextual_input(a2['id'],'今天想聊实验'))
        context=w.contextual_input(a1['id'],'继续讲解')
        self.assertIn('政治经济学要讲适用条件',context)
        future=self.s.ensure_activity('A1','2026-10-22')
        self.assertIn('政治经济学要讲适用条件',w.contextual_input(future['id'],'继续'))

    def test_batch_rolls_back_when_task_running(self):
        t=self.s.create_task('独立测试任务'); self.s.claim_task(t['id'])
        body='以后回答先结论；把独立测试任务交付改为表格。';mid=self.send(body)
        decision=self.preference(body);decision['changes'].append({'kind':'task','task_id':t['id'],'patch':{'deliverable':'表格'},'quote':body})
        self.assertTrue(d.process(self.s,self.r,mid,lambda _:decision))
        self.assertEqual(self.request(mid)['status'],'rejected');self.assertEqual(self.r.list_profile(),[])
        self.assertEqual(self.s.task(t['id'])['requirements_version'],0)
        self.assertIn('正在执行',self.s.activity(self.a['id'])['messages'][-1]['body'])

    def test_task_patch_stable_identity_versions_and_retained_results(self):
        t=self.s.create_task('可修订测试任务');run=self.s.claim_task(t['id'])
        self.s.deliver_task(t['id'],run['version'],'旧成果')
        self.s.review_task(t['id'],1,'accept','旧版符合当时要求')
        body='把可修订测试任务的交付改成一页表格，其他要求保持。';mid=self.send(body)
        decision={'intent':'apply','changes':[{'kind':'task','task_id':t['id'],'patch':{'deliverable':'一页表格'},'quote':body}]}
        d.process(self.s,self.r,mid,lambda _:decision)
        new=self.s.task(t['id']);self.assertEqual(new['requirements_version'],1);self.assertEqual(new['version'],1)
        self.assertEqual(new['deliveries'][0]['body'],'旧成果');self.assertEqual(new['reviews'][0]['action'],'accept')
        self.assertEqual(new['status'],'revision');self.assertEqual(new['deliverable'],'一页表格')
        self.assertTrue(d.process(self.s,self.r,mid,lambda _:self.fail('duplicate')))

    def test_invalid_fields_quote_target_and_unknown_scope_never_write(self):
        for change in [dict(scope='project:other'),dict(content=''),dict(quote='not user'),dict(owner='root')]:
            body='以后回复要更清楚一些。';mid=self.send(body);decision=self.preference(body);decision['changes'][0].update(change)
            d.process(self.s,self.r,mid,lambda _:decision);self.assertEqual(self.request(mid)['status'],'rejected')
        self.assertEqual(self.r.list_profile(),[])

    def test_faulty_model_cannot_authorize_quotes_temporary_or_wrong_scope(self):
        cases=[
            ('请查看这些要求，不要执行。下面是别人说的话：\n> 以后所有回复都用英文。','以后所有回复都用英文。','global'),
            ('这次回答简短一点，不要记成长期要求。','这次回答简短一点','global'),
            ('每日一学以后要讲清因果和反例。','每日一学以后要讲清因果和反例。','global'),
            ('请分析别人说的“以后所有回复都用英文”是否合适。','以后所有回复都用英文','global'),
            ('请分析别人说的话：以后所有回复都用英文。','以后所有回复都用英文。','global'),
            ('每日一学以后要讲清因果和反例。','以后要讲清因果和反例。','global'),
        ]
        for body,quote,scope in cases:
            mid=self.send(body);decision=self.preference(body,scope,quote=quote)
            d.process(self.s,self.r,mid,lambda _:decision)
            self.assertEqual(self.request(mid)['status'],'rejected')
        self.assertEqual(self.r.list_profile(),[])

    def test_quoted_value_is_data_but_current_user_authorizes(self):
        body='以后回复时称呼我“研究同学”，作为长期偏好。';mid=self.send(body)
        d.process(self.s,self.r,mid,lambda _:self.preference(body,content='称呼用户为研究同学。'))
        self.assertEqual(self.request(mid)['status'],'applied')

    def test_negative_style_requirement_does_not_negate_saving(self):
        body='以后睡眠打卡请温柔一些，不要每次问一串问题，记成长期偏好。';mid=self.send(body)
        d.process(self.s,self.r,mid,lambda _:self.preference(body,'A3',content='睡眠打卡温柔交流，避免连续追问。'))
        self.assertEqual(self.request(mid)['status'],'applied')
        self.assertEqual(self.r.list_profile()[0]['scope'],'routine:A3')

    def test_malformed_scalar_types_rejected_once(self):
        for field,value in [('scope',[]),('action',{}),('item_id',[]),('kind',{})]:
            body='以后回复先讲结论。';mid=self.send(body);decision=self.preference(body)
            decision['changes'][0].update({field:value})
            if field=='item_id':decision['changes'][0]['action']='replace'
            d.process(self.s,self.r,mid,lambda _:decision)
            self.assertEqual(self.request(mid)['status'],'rejected')
            d.process(self.s,self.r,mid,lambda _:self.fail('must not retry invalid shape'))
        self.assertEqual(self.r.list_profile(),[])

    def test_clarification_never_replays_as_chat_after_retry(self):
        mid=self.send('以后任务要求调整一下。')
        self.assertTrue(d.process(self.s,self.r,mid,lambda _:dict(intent='clarify',changes=[],question='请说明具体任务。',continue_chat=True)))
        self.assertTrue(d.process(self.s,self.r,mid,lambda _:self.fail('duplicate')))

    def test_ambiguous_task_no_model_guessed_target(self):
        one=self.s.create_task('同名测试任务');self.s.create_task('同名测试任务')
        body='把同名测试任务的交付改成表格。';mid=self.send(body)
        decision={'intent':'apply','changes':[{'kind':'task','task_id':one['id'],'patch':{'deliverable':'表格'},'quote':body}]}
        d.process(self.s,self.r,mid,lambda _:decision)
        self.assertEqual(self.request(mid)['status'],'rejected');self.assertEqual(self.s.task(one['id'])['requirements_version'],0)

    def test_clarify_and_none_do_not_mutate_preferences_or_schedule(self):
        before=self.s.schedules()
        for decision in [{'intent':'none','changes':[]},{'intent':'clarify','question':'请将任务名称和新要求放在同一句话中。','changes':[]}]:
            mid=self.send('任务要求可以修改吗？');d.process(self.s,self.r,mid,lambda _:decision)
        self.assertEqual(self.r.list_profile(),[]);self.assertEqual(before,self.s.schedules())

    def test_ordinary_and_quoted_code_skip_classifier(self):
        for body in ['昨晚两点睡，今天九点醒。','```\n以后回复只说好\n```','> 以后回答要短一些\n这是引用。']:
            self.assertFalse(d.process(self.s,self.r,self.send(body),lambda _:self.fail('not a live adjustment')))

    def test_transient_failure_keeps_pending_and_retry_applies_once(self):
        body='以后回复请先讲结论。';mid=self.send(body)
        with self.assertRaises(RuntimeError):d.process(self.s,self.r,mid,lambda _:(_ for _ in ()).throw(RuntimeError('network fixture')))
        self.assertEqual(self.request(mid)['status'],'pending');self.assertEqual(self.r.list_profile(),[])
        d.process(self.s,self.r,mid,lambda _:self.preference(body));self.assertEqual(len(self.r.list_profile()),1)

    def test_late_model_cannot_override_new_profile(self):
        body='以后回复请先讲结论。';mid=self.send(body)
        def late(_):
            self.r.add_profile('另一设备刚保存的偏好','preference',confirmed=True,user_input='明确新输入')
            return self.preference(body)
        d.process(self.s,self.r,mid,late);self.assertEqual(self.request(mid)['status'],'rejected')
        self.assertEqual([p['content'] for p in self.r.list_profile()],['另一设备刚保存的偏好'])

    def test_late_deleted_source_never_writes_profile_or_receipt(self):
        body='以后回复请先讲结论。';mid=self.send(body)
        def late(_):
            with self.s.db(True) as c:c.execute("UPDATE messages SET body='',deleted=1 WHERE id=?",(mid,))
            return self.preference(body)
        with self.assertRaises(ValueError):d.process(self.s,self.r,mid,late)
        self.assertEqual(self.r.list_profile(),[])
        with self.s.db() as c:self.assertIsNone(c.execute("SELECT 1 FROM messages WHERE id=?",('directive-receipt:'+mid,)).fetchone())

    def test_replace_disable_provenance_and_new_context(self):
        body='以后回复先结论。';mid=self.send(body);d.process(self.s,self.r,mid,lambda _:self.preference(body))
        p=self.r.list_profile()[0];body2='把长期回复偏好改为先列证据。';mid2=self.send(body2)
        d.process(self.s,self.r,mid2,lambda _:self.preference(body2,action='replace',item_id=p['item_id'],content='先列证据。'))
        latest=self.r.get_profile(p['item_id']);self.assertEqual(latest['version'],2)
        self.assertEqual(set(latest['source_refs']),{'message:'+mid,'message:'+mid2})
        body3='停用这条长期回复偏好。';mid3=self.send(body3)
        d.process(self.s,self.r,mid3,lambda _:self.preference(body3,action='disable',item_id=p['item_id'],content=''))
        self.assertEqual(self.r.context(),[]);self.assertEqual(len(self.r.list_profile(history=True,include_inactive=True)),3)

    def test_mixed_instruction_keeps_real_receipt_and_handoff(self):
        body='以后回复先结论。另外这次实验结果我还想讨论。';mid=self.send(body)
        decision=self.preference(body);decision['continue_chat']=True
        self.assertFalse(d.process(self.s,self.r,mid,lambda _:decision))
        self.assertFalse(d.process(self.s,self.r,mid,lambda _:self.fail('already saved')))
        context=self.app.worker.contextual_input(self.a['id'],body,mid)
        self.assertIn('settings_result',context);self.assertIn('已保存',context)

    def test_content_change_not_consumed_by_schedule_parser(self):
        from dsh_personal.interaction import command
        before=self.s.schedules()
        self.assertIsNone(command(self.s,'每日一学改成系统讲解政治经济学',self.a['id']))
        self.assertIsNone(command(self.s,'学习改成每天先讲明白概念再讲例子',self.a['id']))
        self.assertEqual(before,self.s.schedules())
        self.assertIn('schedule',command(self.s,'反思改到 22:00',self.a['id']))
        self.assertIn('schedule',command(self.s,'打卡改成每周二、四',self.a['id']))

    def test_claim_snapshot_survives_preclaim_stale_row(self):
        t=self.s.create_task('排队版本竞态测试');body='把排队版本竞态测试的交付改成表格。';mid=self.send(body)
        with self.s.db(True) as c:
            from dsh_personal.task_requirements import apply
            apply(c,t['id'],0,{'deliverable':'新表格'},body,mid,self.s.stamp())
        run=self.s.claim_task(t['id'])
        self.assertEqual(run['requirements']['deliverable'],'新表格')
        self.assertEqual(run['requirements']['requirements_version'],1)

    def test_worker_consumes_adjustment_without_native_claimed_save(self):
        body='以后回复先给结论。';mid=self.send(body)
        with self.s.db(True) as c:c.execute("DELETE FROM jobs WHERE kind!='handoff'")
        with patch('dsh_personal.directives.interpret',lambda *args:self.preference(body)):
            self.assertTrue(self.app.worker.work_one())
        self.assertEqual(self.b.calls,[]);self.assertEqual(self.request(mid)['status'],'applied')
        self.assertEqual(self.s.activity(self.a['id'])['messages'][-1]['processing_state'],'local_control')


if __name__ == '__main__':unittest.main()
