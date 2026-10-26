"""Explicit owner commands, real isolated persistence, no model or production IO."""
import concurrent.futures
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from dsh_personal.server import Application

from dsh_personal.core import Store, TZ
from dsh_personal.interaction import command, is_local_control
from dsh_personal.routine_settings import read_settings


class RoutineCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.clock=[dt.datetime(2026,10,26,19,0,tzinfo=TZ)]
        self.store=Store(Path(self.temp.name)/'test.sqlite',lambda:self.clock[0])

    def settings(self,routine):
        with self.store.db() as db:return read_settings(db,routine)

    def events(self):
        with self.store.db() as db:return [dict(row) for row in db.execute('SELECT * FROM schedule_events')]

    def schedule(self,routine):
        return next(s for s in self.store.schedules() if s['routine']==routine)

    def test_pure_control_classification_shares_handler_grammar(self):
        for text in ['每日一学经典主题改为 论语','查看打卡设置','每日反思方面改为 科研、健康',
                     '每日反思方面改为 科研、科研','每日反思改到 21:40','睡眠打卡时间改为 24:00',
                     '所有打卡时间改为 20:00','打卡改成每周二、四','睡眠暂停到 明天']:
            with self.subTest(text=text):
                self.assertTrue(is_local_control(text))
                self.assertIsNotNone(command(self.store,text))
        for text in ['今天我完成实验','每日反思方面改为 健康可以吗？','以后学习要深入浅出',
                     '“每日反思改到 20:00”','每日一学改成系统讲解政治经济学',
                     '学习改成每天先讲明白概念再讲例子']:
            with self.subTest(text=text):
                self.assertFalse(is_local_control(text,'A2'))
                self.assertIsNone(command(self.store,text))
        self.assertFalse(is_local_control('改到 20:00'))
        self.assertTrue(is_local_control('改到 20:00','A2'))
        self.assertTrue(is_local_control('跳过本次','A2'))
        self.assertFalse(is_local_control('跳过本次'))

    def test_read_shows_all_saved_content_and_actual_times_without_writing(self):
        reply=command(self.store,'查看打卡设置')
        self.assertIn('博士学位、科研创业、文化事业',reply['message'])
        self.assertIn('21:00',reply['message']);self.assertIn('21:30',reply['message'])
        self.assertIn('08:30',reply['message']);self.assertIn('Asia/Shanghai',reply['message'])
        self.assertEqual(self.settings('A1')['version'],0);self.assertEqual(self.events(),[])

    def test_questions_quotes_and_content_times_are_not_schedule_commands(self):
        before=self.store.schedules()
        for text in ['每日一学经典主题改为论语可以吗？','每日反思方面改为 科研、健康、家庭好吗',
                     '“每日反思改到 21:40”','他说：每日反思改到 21:40',
                     '每日反思改到 21:40 可以吗','每日一学改成系统讲解政治经济学',
                     '学习改成每天先讲明白概念再讲例子','今天睡眠打卡我在 08:00 起床',
                     '每日一学时间改为明天下午看看是否合适']:
            with self.subTest(text=text):self.assertIsNone(command(self.store,text))
        self.assertEqual(before,self.store.schedules());self.assertEqual(self.events(),[])
        self.assertEqual(self.settings('A1')['version'],0);self.assertEqual(self.settings('A2')['version'],0)

    def test_learning_fields_persist_independently_and_teaching_time_is_content(self):
        for number,(text,key,value) in enumerate([
            ('每日一学经典主题改为 论语','classic','论语'),
            ('每日一学历史主题改为 毛泽东诗词','history','毛泽东诗词'),
            ('每日一学现代概念主题改为 劳动价值论','modern','劳动价值论')]):
            reply=command(self.store,text,submission_id='topic-'+str(number))
            self.assertEqual(reply['settings']['topics'][key],value)
            self.assertTrue(reply['receipt'])
        reply=command(self.store,'每日一学教学要求改为 以 20:30 的日常场景举例，讲清概念',submission_id='teaching')
        self.assertIn('20:30',reply['settings']['teaching_request'])
        self.assertEqual(reply['settings']['topics'],{'classic':'论语','history':'毛泽东诗词','modern':'劳动价值论'})
        self.assertEqual(self.events(),[])
        restarted=Store(self.store.path,lambda:self.clock[0])
        with restarted.db() as db:self.assertEqual(read_settings(db,'A1')['settings'],reply['settings'])

    def test_restore_selection_preserves_teaching_request(self):
        command(self.store,'每日一学教学要求改为 每次先解释关键概念',submission_id='teach')
        command(self.store,'每日一学经典主题改为 论语',submission_id='classic')
        reply=command(self.store,'每日一学恢复自动选材',submission_id='restore')
        self.assertEqual(reply['settings']['topics'],{'classic':'','history':'','modern':''})
        self.assertEqual(reply['settings']['teaching_request'],'每次先解释关键概念')

    def test_reflection_dimensions_persist_and_keep_known_identity(self):
        reply=command(self.store,'每日反思方面改为 博士学位、健康、家庭',submission_id='dimensions')
        dimensions=reply['settings']['dimensions']
        self.assertEqual([d['label'] for d in dimensions],['博士学位','健康','家庭'])
        self.assertEqual(dimensions[0]['id'],'doctorate')
        self.assertEqual(dimensions[1]['keywords'],['健康'])
        ids={d['label']:d['id'] for d in dimensions}
        reordered=command(self.store,'每日反思方面改为 家庭、健康',submission_id='reorder')
        self.assertEqual([d['id'] for d in reordered['settings']['dimensions']],[ids['家庭'],ids['健康']])
        self.assertEqual(self.events(),[])

    def test_reflection_invalid_list_does_not_write(self):
        for text in ['每日反思方面改为 科研、科研','每日反思方面改为 、，',
                     '每日反思方面改为 '+ '、'.join('方面'+str(x) for x in range(13))]:
            self.assertIn('尚未修改',command(self.store,text)['message'])
        self.assertEqual(self.settings('A2')['version'],0)

    def test_same_settings_submission_replays_after_later_edits(self):
        first=command(self.store,'每日反思方面改为 科研、健康',submission_id='old')
        command(self.store,'每日反思方面改为 家庭',submission_id='new')
        again=command(self.store,'每日反思方面改为 科研、健康',submission_id='old')
        self.assertEqual(again['receipt'],first['receipt']);self.assertTrue(again['duplicate'])
        self.assertEqual([d['label'] for d in self.settings('A2')['settings']['dimensions']],['家庭'])
        with self.assertRaises(ValueError):command(self.store,'每日反思方面改为 休息',submission_id='old')

    def test_times_cover_all_routines_and_store_before_after(self):
        cases=[('每日一学时间改为 20:30','A1','21:00','20:30'),
               ('每日反思改到 21:40','A2','21:30','21:40'),
               ('睡眠打卡时间改为 08:00','A3','08:30','08:00')]
        for i,(text,routine,before,after) in enumerate(cases):
            reply=command(self.store,text,submission_id='time-'+str(i))
            self.assertEqual(reply['before']['time'],before);self.assertEqual(reply['schedule']['time'],after)
            self.assertIn(before+' → '+after,reply['message']);self.assertIn('Asia/Shanghai',reply['message'])
            self.assertEqual(reply['schedule']['weekdays'],list(range(7)))
            self.assertEqual(self.schedule(routine)['time'],after)
        events=self.events();self.assertEqual(len(events),3)
        self.assertEqual(json.loads(events[0]['before_json'])['time'],'21:00')
        self.assertEqual(json.loads(events[0]['after_json'])['time'],'20:30')
        self.assertEqual(events[0]['source'],cases[0][0])

    def test_time_change_preserves_frequency_and_pause(self):
        self.store.change_schedule('A2','21:30',[1,3],'2026-10-28','fixture owner instruction')
        reply=command(self.store,'每日反思时间改为 21:40',submission_id='preserve')
        self.assertEqual(reply['schedule']['weekdays'],[1,3])
        self.assertEqual(reply['schedule']['paused_until'],'2026-10-28')
        self.assertTrue(reply['schedule']['next_at'].startswith('2026-10-29T21:40'))

    def test_invalid_times_never_partially_apply(self):
        before=self.store.schedules()
        for value in ['24:00','08:60','8:00','08:0','08:00:30','08:00 和每日反思改到 20:00']:
            reply=command(self.store,'每日一学时间改为 '+value)
            self.assertIn('尚未修改',reply['message'])
        self.assertEqual(before,self.store.schedules());self.assertEqual(self.events(),[])

    def test_all_times_requires_individual_commands_without_partial_writes(self):
        reply=command(self.store,'所有打卡时间改为 20:00')
        self.assertIn('尚未修改',reply['message'])
        self.assertIn('每日一学时间改为 20:00',reply['message'])
        self.assertIn('每日反思时间改为 20:00',reply['message'])
        self.assertIn('睡眠打卡时间改为 20:00',reply['message'])
        self.assertEqual(self.events(),[])

    def test_existing_explicit_frequency_pause_restore_still_work(self):
        reply=command(self.store,'打卡改成每周二、四',submission_id='weekly')
        self.assertEqual(reply['schedule']['weekdays'],[1,3])
        reply=command(self.store,'睡眠暂停到 2026-10-28',submission_id='pause')
        self.assertEqual(reply['schedule']['paused_until'],'2026-10-28')
        reply=command(self.store,'睡眠恢复每天 08:10',submission_id='daily')
        self.assertEqual(reply['schedule']['weekdays'],list(range(7)))
        self.assertIsNone(reply['schedule']['paused_until']);self.assertEqual(reply['schedule']['time'],'08:10')

    def test_same_time_submission_replay_does_not_revert_newer_changes(self):
        first=command(self.store,'每日反思改到 21:40',submission_id='first')
        self.clock[0]+=dt.timedelta(seconds=1)
        command(self.store,'每日反思改到 每周二、四 22:00',submission_id='second')
        again=command(self.store,'每日反思改到 21:40',submission_id='first')
        self.assertEqual(again['schedule']['time'],first['schedule']['time'])
        self.assertEqual(self.schedule('A2')['time'],'22:00');self.assertEqual(len(self.events()),2)
        self.assertEqual(self.schedule('A2')['weekdays'],[1,3])
        self.assertEqual(again['before']['time'],'21:30')
        self.assertIn('重放原变更回执',again['message'])
        with self.assertRaises(ValueError):command(self.store,'每日反思改到 22:10',submission_id='first')

    def test_concurrent_natural_retries_have_one_durable_change(self):
        for text,submission in [('每日反思方面改为 健康、家庭','concurrent-settings'),
                                ('每日反思改到 20:40','concurrent-time')]:
            with concurrent.futures.ThreadPoolExecutor(8) as pool:
                replies=list(pool.map(lambda _:command(self.store,text,submission_id=submission),range(8)))
            self.assertEqual(len({r['receipt'] for r in replies}),1)
        self.assertEqual(self.settings('A2')['version'],1)
        self.assertEqual(len(self.events()),1)

    def test_crash_after_skip_then_retry_preserves_newer_continue(self):
        app=Application(Path(self.temp.name)/'app',lambda:self.clock[0]);store=app.store
        active=store.ensure_activity('A2')
        payload={'op':'message','activity_id':active['id'],'submission_id':'crash-original-skip','body':'跳过本次'}
        def crash_after_control(*args,**kwargs):
            command(*args,**kwargs)
            raise RuntimeError('synthetic crash after committed control')
        with patch('dsh_personal.interaction.command',crash_after_control):
            with self.assertRaises(RuntimeError):app.action(payload)
        self.assertEqual(store.activity(active['id'])['activity']['status'],'skipped')
        app=Application(app.data,lambda:self.clock[0]);store=app.store
        app.action({'op':'message','activity_id':active['id'],'submission_id':'crash-new-continue','body':'继续本次'})
        result=app.action(payload)
        self.assertEqual(store.activity(active['id'])['activity']['status'],'awaiting_reply')
        self.assertTrue(result['command']['activity_control']['duplicate'])
        self.assertIn('重放原操作回执',result['command']['message'])
        with store.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM meta WHERE key='activity_control_request:crash-original-skip'").fetchone()[0],1)
            row=json.loads(db.execute("SELECT value FROM meta WHERE key='activity_control_request:crash-original-skip'").fetchone()[0])
            self.assertNotIn('跳过本次',json.dumps(row,ensure_ascii=False))

    def test_activity_control_retries_reject_changed_payload_and_deleted_source(self):
        app=Application(Path(self.temp.name)/'app',lambda:self.clock[0]);store=app.store
        active=store.ensure_activity('A2');body='跳过本次';submission='control-delete-source'
        first=app.action({'op':'message','activity_id':active['id'],'submission_id':submission,'body':body})
        with self.assertRaises(ValueError):store.control(active['id'],'continue',submission_id=submission,source='继续本次')
        store.delete_message(first['receipt'],'isolated owner deletion')
        with self.assertRaises(ValueError):store.control(active['id'],'skip',submission_id=submission,source=body)

    def test_shorthand_time_requires_active_routine(self):
        self.assertIsNone(command(self.store,'改到 20:00'))
        active=self.store.ensure_activity('A2')
        reply=command(self.store,'改到 20:00',active['id'],submission_id='short')
        self.assertEqual(reply['schedule']['routine'],'A2')


if __name__=='__main__':unittest.main()
