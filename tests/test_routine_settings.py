"""Cross-module settings, recovery and publication contracts; no real data."""
import copy
import datetime as dt
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dsh_personal.core import TZ
from dsh_personal.server import Application
from dsh_personal.routine_settings import DEFAULTS, read_settings, save_settings
from dsh_personal.learning import prepare_learning, source_identity_frozen
from dsh_personal.learning_schedule import tick
from dsh_personal.operations import recovery_ledger, _merge_core_ledger
from dsh_personal.deletion import apply_message_deletion
from dsh_personal import learning_guard as guard


class RoutineSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.now=dt.datetime(2026,10,26,20,tzinfo=TZ)
        self.app=Application(self.temp.name,lambda:self.now);self.s=self.app.store
    def tearDown(self):self.temp.cleanup()
    def setting(self,r='A1'):
        with self.s.db() as db:return read_settings(db,r)
    def save(self,r='A1',settings=None,version=0,request='settings-1'):
        return save_settings(self.s,r,settings or copy.deepcopy(DEFAULTS[r]),version,request,'isolated owner fixture')
    def send(self,aid,body,ident='message-001'):
        return self.app.action({'op':'message','activity_id':aid,'body':body,'submission_id':ident})

    def test_concurrent_cas_and_duplicate_old_request_never_rolls_back(self):
        first=self.save()
        settings=copy.deepcopy(DEFAULTS['A1']);settings['topics']['classic']='孟子'
        def change(i):
            try:return self.save(settings=settings,version=1,request='race-'+str(i))
            except ValueError:return None
        with ThreadPoolExecutor(2) as pool:out=list(pool.map(change,range(2)))
        self.assertEqual(sum(x is not None for x in out),1)
        self.assertEqual(self.save()['receipt'],first['receipt'])
        self.assertEqual(self.setting()['version'],2)
        self.assertEqual(self.setting()['settings']['topics']['classic'],'孟子')

    def test_validation_is_atomic_and_defaults_survive_restart(self):
        for settings in ({'dimensions':[]},{'dimensions':[{'label':'相同'},{'label':'相同'}]},
                         {'dimensions':[{'label':'a','keywords':['']}]}, {'dimensions':[{'label':'a','id':'../'}]}):
            with self.assertRaises(ValueError):self.save('A2',settings)
        self.assertEqual(self.setting('A2')['version'],0)
        self.save('A2',{'dimensions':[{'label':'健康','prompt':'感受如何？'}]})
        self.app=Application(self.temp.name,lambda:self.now);self.s=self.app.store
        self.assertEqual(self.setting('A2')['settings']['dimensions'][0]['label'],'健康')

    def test_custom_reflection_snapshot_and_current_completion_count(self):
        old=self.s.ensure_activity('A2','2026-10-25')['id']
        self.save('A2',{'dimensions':[{'label':'健康'},{'label':'家庭'}]})
        aid=self.s.ensure_activity('A2')['id']
        self.assertEqual(len(self.s.activity(old)['reflection_dimensions']),3)
        self.s.submit(aid,'answer-1','今天健康方面完成了散步')
        self.assertEqual(self.s.activity(aid)['activity']['status'],'partial')
        self.s.submit(aid,'answer-2','家庭方面今天一起吃饭')
        self.assertEqual(self.s.activity(aid)['activity']['status'],'basic_complete')
        self.s.submit(aid,'answer-3','继续深入谈健康的问题')
        self.assertEqual(len(self.s.activity(aid)['messages']),4)

    def test_settings_commands_are_not_reflection_answers_or_sleep_records(self):
        for routine,body in [('A2','每日反思方面改为 科研、健康'),('A3','睡眠打卡时间改为 09:00')]:
            aid=self.s.ensure_activity(routine)['id'];before=self.s.activity(aid)['activity']
            response=self.send(aid,body,routine+'-settings')
            self.assertIn('command',response)
            view=self.s.activity(aid)
            self.assertEqual(view['activity']['status'],before['status'])
            self.assertEqual(view['reflection'],[])
            self.assertEqual(self.s.summary_snapshot(aid)['messages'],[])
        aid=self.s.ensure_activity('A2')['id']
        self.send(aid,'跳过本次','skip-message')
        self.assertEqual(self.s.activity(aid)['activity']['status'],'skipped')

    def test_first_manual_settings_change_requeues_and_control_does_not_freeze(self):
        aid=self.s.ensure_activity('A1')['id'];old=prepare_learning(self.s,aid)
        with self.s.db(True) as db:db.execute("UPDATE jobs SET status='done'")
        self.send(aid,'每日一学经典主题改为 孟子')
        with self.s.db() as db:
            self.assertFalse(source_identity_frozen(db,aid))
            self.assertTrue(db.execute("SELECT 1 FROM jobs WHERE kind='learning' AND target=? AND status='pending'",(aid,)).fetchone())
        new=prepare_learning(self.s,aid)
        self.assertEqual(new['manifest']['cards'][0]['id'],'mencius-jinxin-3')
        self.assertNotEqual(old['manifest']['routine_settings_version'],new['manifest']['routine_settings_version'])

    def test_staged_old_settings_cannot_publish_at_activation_boundary(self):
        tick(self.s)
        with self.s.db(True) as db:
            aid=db.execute("SELECT id FROM activities WHERE routine='A1'").fetchone()[0]
            db.execute("UPDATE activities SET session_state='ready',session_id='fixture-session' WHERE id=?",(aid,))
        a=self.s.activity(aid)['activity'];manifest=prepare_learning(self.s,aid)['manifest']
        row=guard.enqueue(self.s,a,{'session_id':'fixture-session','end_seq':2,'assistant_seq':1},'isolated draft',manifest,[])
        token=guard.record_attempt(self.s,row['id'])['attempt_token']
        self.assertTrue(guard.commit(self.s,row['id'],True,attempt_token=token)['staged'])
        self.save()
        self.now=self.now.replace(hour=21)
        tick(self.s)
        self.assertIsNone(self.s.activity(aid)['activity']['ready_at'])
        with self.s.db() as db:
            self.assertFalse(db.execute("SELECT 1 FROM messages WHERE processing_state='learning_reviewed'").fetchone())

    def test_unavailable_topic_is_saved_with_honest_gap(self):
        value=copy.deepcopy(DEFAULTS['A1']);value['topics']['history']='不存在的测试史料'
        out=self.save(settings=value)
        self.assertEqual(out['missing_topics'],[{'category':'history','topic':'不存在的测试史料'}])
        self.assertEqual(self.app.state()['routine_settings']['A1']['version'],1)
        self.assertTrue(self.app.state()['learning_source_options'])

    def test_recovery_settings_time_and_older_ledger_do_not_rollback(self):
        self.save();older=recovery_ledger(self.app)
        self.now+=dt.timedelta(minutes=1)
        value=copy.deepcopy(DEFAULTS['A1']);value['teaching_request']='先举一个具体例子'
        self.save(settings=value,version=1,request='new')
        self.s.change_schedule('A1','20:30',list(range(7)),None,'isolated time',submission_id='time')
        newer=recovery_ledger(self.app)
        with tempfile.TemporaryDirectory() as target:
            app=Application(target,lambda:self.now)
            _merge_core_ledger(app.store,newer);_merge_core_ledger(app.store,older)
            with app.store.db() as db:self.assertEqual(read_settings(db,'A1')['version'],2)
            self.assertEqual(app.store.schedules()[0]['time'],'20:30')

    def test_deleting_settings_source_scrubs_copies_and_old_ledger_cannot_restore(self):
        aid=self.s.ensure_activity('CHAT')['id']
        result=self.send(aid,'每日反思方面改为 私密测试方面、健康')
        reflection=self.s.ensure_activity('A2')['id']
        old=recovery_ledger(self.app)
        with self.s.db(True) as db:apply_message_deletion(db,result['receipt'],self.s.stamp())
        with self.s.db() as db:
            self.assertNotIn('私密测试方面','\n'.join(r[0] for r in db.execute('SELECT value FROM meta')))
            self.assertNotIn('私密测试方面','\n'.join(r[0] for r in db.execute('SELECT body FROM messages')))
        _merge_core_ledger(self.s,old)
        self.assertEqual(self.setting('A2')['version'],2)
        self.assertNotIn('私密测试方面',json.dumps(self.setting('A2'),ensure_ascii=False))

    def test_settings_show_receipt_in_other_activity_follows_source_deletion(self):
        aid=self.s.ensure_activity('CHAT','2026-10-25')['id']
        result=self.send(aid,'每日反思方面改为 跨会话私密标记')
        other=self.s.ensure_activity('CHAT')['id']
        self.send(other,'查看打卡设置','display-settings')
        before=recovery_ledger(self.app)
        with self.s.db() as db:
            self.assertTrue(db.execute("SELECT 1 FROM messages WHERE activity_id=? AND role='assistant' AND body LIKE '%跨会话私密标记%'",(other,)).fetchone())
        with self.s.db(True) as db:apply_message_deletion(db,result['receipt'],self.s.stamp())
        _merge_core_ledger(self.s,before)
        with self.s.db() as db:
            self.assertFalse(db.execute("SELECT 1 FROM messages WHERE body LIKE '%跨会话私密标记%'").fetchone())

    def test_simultaneous_identical_message_command_has_single_receipt(self):
        aid=self.s.ensure_activity('CHAT')['id']
        with ThreadPoolExecutor(2) as pool:
            result=list(pool.map(lambda _:self.send(aid,'每日反思方面改为 健康、家庭','concurrent-settings'),range(2)))
        self.assertEqual({r['receipt'] for r in result},{result[0]['receipt']})
        self.assertEqual(self.setting('A2')['version'],1)
        with self.s.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE id LIKE 'routine-settings:%'").fetchone()[0],1)

    def test_invalid_timezone_or_seconds_schedule_not_saved(self):
        for time in ('9:00','09:00:01','09:00+04:00','24:00'):
            with self.assertRaises(ValueError):self.s.change_schedule('A1',time,list(range(7)),None,'test')
        self.assertEqual(self.s.schedules()[0]['time'],'21:00')


if __name__=='__main__':unittest.main()
