"""Scheduled lesson behavior, with isolated data and no model/network calls."""
import datetime as dt
import json
import tempfile
import unittest
from unittest.mock import patch

from dsh_personal import learning_guard as guard, learning_schedule as schedule
from dsh_personal.core import TZ
from dsh_personal.learning import prepare_learning
from dsh_personal.operations import _markdown_from
from dsh_personal.server import Application
from dsh_personal.worker import Worker


class Bridge:
    def __init__(self):
        self.sessions = []

    def ensure_session(self, identity):
        self.sessions.append(identity)
        return 'schedule-fixture:' + identity


class LearningScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = dt.datetime(2026, 10, 24, 12, tzinfo=TZ)
        self.app = Application(self.temp.name, lambda: self.now)
        self.store = self.app.store
        self.bridge = Bridge()
        self.worker = Worker(self.store, self.bridge, self.app.records, lambda: {})

    def tearDown(self):
        self.temp.cleanup()

    def rows(self, table, where='', args=()):
        with self.store.db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM ' + table + (' WHERE ' + where if where else ''), args)]

    def prepare(self, day=None):
        schedule.tick(self.store)
        rows = self.rows('activities', "routine='A1' AND business_date=?", (day or self.store.today(),))
        self.assertEqual(len(rows), 1)
        return rows[0]

    def candidate(self, activity, *, approved=True):
        sid = self.worker.ensure_session(activity['id'])
        activity = self.store.activity(activity['id'])['activity']
        manifest = prepare_learning(self.store, activity['id'])['manifest']
        body = '已核对的独立测试课程正文。\n' + '\n'.join(
            c['source_url'] for c in manifest['cards']) + '\n' + manifest['world_fun_fact']['source_url']
        row = guard.enqueue(self.store, activity,
                            {'session_id': sid, 'end_seq': 9, 'assistant_seq': 8,
                             'received_at': self.store.stamp()}, body, manifest,
                            self.app.records.context('routine:A1'))
        if approved:
            token = guard.record_attempt(self.store, row['id'])['attempt_token']
            result = guard.commit(self.store, row['id'], True, attempt_token=token)
            self.assertTrue(result['staged'])
            self.assertFalse(result['published'])
        return row, body

    def published(self, aid):
        return self.rows('messages', "activity_id=? AND processing_state='learning_reviewed' AND deleted=0", (aid,))

    def at_due(self):
        self.now = self.now.replace(hour=21, minute=0, second=0, microsecond=0)

    def test_approved_preparation_is_private_across_public_views_and_delivery_records(self):
        activity = self.prepare(); aid = activity['id']
        row, body = self.candidate(activity)
        with self.store.db(True) as db:
            db.execute('UPDATE activities SET bookmarked=1 WHERE id=?', (aid,))
        for view in ('full', 'chat'):
            state = self.app.state(view=view)
            self.assertNotIn(aid, [a['id'] for a in state['activities']])
            self.assertNotIn(aid, [a['id'] for p in state['routine_projects'] for a in p['activities']])
            self.assertNotIn(body, json.dumps(state))
        self.assertNotIn(aid, [a['id'] for a in self.app.bookmarks()])
        self.assertNotIn(aid, [a['activity_id'] for a in self.store.gaps()])
        for read in (lambda: self.app.activity(aid), lambda: self.app.source('activity:' + aid),
                     lambda: self.app.source('message:' + row['message_id'])):
            with self.assertRaises(KeyError):
                read()
        self.assertNotIn(body, json.dumps(self.store.journal()))
        self.assertNotIn(body, _markdown_from(self.store.path, self.app.records))
        self.assertEqual(self.published(aid), [])
        self.assertEqual(self.rows('notifications', 'activity_id=?', (aid,)), [])
        current = self.store.activity(aid)['activity']
        self.assertIsNone(current['ready_at']); self.assertIsNone(current['triggered_at'])
        self.assertIsNone(guard.snapshot(self.store, row['id'])['published_hash'])
        self.assertIsNone(json.loads(self.store.meta('learning_selection:' + aid))['delivery'])

    def test_due_release_is_network_free_atomic_and_idempotent(self):
        activity = self.prepare(); aid = activity['id']
        row, body = self.candidate(activity)
        generated_at = row['received_at']
        self.at_due()
        with (patch('urllib.request.urlopen', side_effect=AssertionError('release must not use network')),
              patch.object(Worker, 'learning_model', side_effect=AssertionError('release must not use model'))):
            self.store.tick(); self.store.tick()
        messages = self.published(aid)
        self.assertEqual(len(messages), 1); self.assertEqual(messages[0]['body'], body)
        self.assertEqual(messages[0]['received_at'], self.store.stamp())
        self.assertNotEqual(messages[0]['received_at'], generated_at)
        current = self.app.activity(aid)['activity']
        self.assertEqual(current['ready_at'], self.store.stamp())
        self.assertEqual(current['triggered_at'], self.store.stamp())
        self.assertEqual(current['business_date'], '2026-10-24')
        self.assertEqual(len(self.rows('notifications', "activity_id=? AND kind='first'", (aid,))), 1)
        self.assertEqual(json.loads(self.store.meta('learning_selection:' + aid))['delivery']['received_at'], self.store.stamp())
        self.assertIsNotNone(guard.snapshot(self.store, row['id'])['published_hash'])

    def test_next_day_enters_preparation_only_within_24_hours(self):
        self.prepare()
        self.assertEqual(len(self.rows('activities', "routine='A1'")), 1)
        self.now = self.now.replace(hour=21)
        schedule.tick(self.store); schedule.tick(self.store)
        future = self.rows('activities', "routine='A1' AND business_date='2026-10-25'")
        self.assertEqual(len(future), 1)
        with self.store.db() as db:
            self.assertTrue(schedule.is_hidden(db, future[0]['id']))
        self.assertEqual(len(self.rows('jobs', "target=? AND kind='learning'", (future[0]['id'],))), 1)

    def test_release_failure_rolls_back_visibility_message_and_notification_together(self):
        activity = self.prepare(); aid = activity['id']; row, _ = self.candidate(activity)
        publish = guard._publish

        def interrupted(*args, **kwargs):
            publish(*args, **kwargs)
            raise RuntimeError('fixture interruption before activation transaction commit')

        self.at_due()
        with patch.object(guard, '_publish', side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                schedule.tick(self.store)
        self.assertEqual(self.published(aid), [])
        self.assertEqual(self.rows('notifications', 'activity_id=?', (aid,)), [])
        self.assertIsNone(self.rows('learning_releases', 'activity_id=?', (aid,))[0]['activated_at'])
        self.assertIsNone(guard.snapshot(self.store, row['id'])['published_hash'])
        self.assertIsNone(json.loads(self.store.meta('learning_selection:' + aid))['delivery'])
        self.assertIsNone(self.store.activity(aid)['activity']['ready_at'])
        schedule.tick(self.store)
        self.assertEqual(len(self.published(aid)), 1)

    def test_regular_restart_retains_prepared_body_and_recovery_activation_is_silent(self):
        activity = self.prepare(); aid = activity['id']
        _, body = self.candidate(activity)
        self.app = Application(self.temp.name, lambda: self.now); self.store = self.app.store
        schedule.tick(self.store, recovery=True)
        self.assertEqual(self.published(aid), [])
        self.at_due(); self.now += dt.timedelta(seconds=10)
        self.store.tick(recovery=True); self.store.tick()
        self.assertEqual([m['body'] for m in self.published(aid)], [body])
        self.assertEqual(self.rows('notifications', 'activity_id=?', (aid,)), [])
        self.assertEqual(self.app.activity(aid)['activity']['notification_state'], 'recovery_silent')

    def test_pause_holds_preparation_and_resume_restores_same_day_without_new_activity(self):
        activity = self.prepare(); aid = activity['id']
        self.candidate(activity)
        self.store.change_schedule('A1', '21:00', list(range(7)), '2026-10-24', '本人暂停今日')
        schedule.tick(self.store)
        self.assertTrue(self.rows('jobs', "target=? AND status='schedule_hold'", (aid,)))
        self.now = self.now.replace(hour=20)
        schedule.tick(self.store)
        self.assertEqual(self.published(aid), [])
        self.store.change_schedule('A1', '21:00', list(range(7)), None, '本人恢复今日')
        schedule.tick(self.store)
        self.assertEqual(self.rows('jobs', "target=? AND status='schedule_hold'", (aid,)), [])
        self.at_due(); self.store.tick()
        self.assertEqual(len(self.published(aid)), 1)
        self.assertEqual(len(self.rows('activities', "routine='A1' AND business_date='2026-10-24'")), 1)

    def test_schedule_moved_later_releases_at_new_time(self):
        activity = self.prepare(); aid = activity['id']; self.candidate(activity)
        self.store.change_schedule('A1', '22:00', list(range(7)), None, '本人延后')
        self.at_due(); self.store.tick()
        self.assertEqual(self.published(aid), [])
        self.assertIsNone(self.rows('learning_releases', 'activity_id=?', (aid,))[0]['activated_at'])
        self.now = self.now.replace(hour=22); self.store.tick()
        self.assertEqual(len(self.published(aid)), 1)
        self.assertEqual(self.app.activity(aid)['activity']['scheduled_at'], self.now.isoformat())

    def test_schedule_moved_earlier_releases_at_new_future_time(self):
        activity = self.prepare(); aid = activity['id']; self.candidate(activity)
        self.store.change_schedule('A1', '18:00', list(range(7)), None, '本人提前')
        self.now = self.now.replace(hour=17, minute=59); self.store.tick()
        self.assertEqual(self.published(aid), [])
        self.now = self.now.replace(hour=18, minute=0); self.store.tick()
        self.assertEqual(len(self.published(aid)), 1)

    def test_changed_weekday_or_past_slot_does_not_publish_a_cancelled_slot(self):
        activity = self.prepare(); aid = activity['id']; self.candidate(activity)
        weekdays = [d for d in range(7) if d != self.now.weekday()]
        self.store.change_schedule('A1', '21:00', weekdays, None, '本人排除今日')
        self.at_due(); self.store.tick()
        self.assertEqual(self.published(aid), [])
        # Changing to an already elapsed slot resumes at its next future date.
        self.now += dt.timedelta(minutes=10)
        self.store.change_schedule('A1', '20:00', list(range(7)), None, '本人改为20点')
        self.store.tick()
        self.assertEqual(self.published(aid), [])
        self.assertIsNone(self.rows('learning_releases', 'activity_id=?', (aid,))[0]['activated_at'])

    def test_midnight_slot_is_prepared_previous_day_and_uses_shanghai_business_date(self):
        self.store.change_schedule('A1', '00:00', list(range(7)), None, '本人改为零点')
        self.now = self.now.replace(hour=23, minute=30)
        activity = self.prepare('2026-10-25'); aid = activity['id']; self.candidate(activity)
        self.assertEqual(self.published(aid), [])
        self.now = dt.datetime(2026, 10, 25, 0, tzinfo=TZ); self.store.tick()
        current = self.app.activity(aid)['activity']
        self.assertEqual(current['business_date'], '2026-10-25')
        self.assertEqual(current['ready_at'], '2026-10-24T16:00:00+00:00')
        self.assertEqual(len(self.published(aid)), 1)

    def test_unreviewed_lesson_is_delayed_at_due_and_published_only_after_review(self):
        activity = self.prepare(); aid = activity['id']
        row, body = self.candidate(activity, approved=False)
        self.at_due(); self.store.tick()
        self.assertEqual(self.published(aid), [])
        self.assertIsNone(self.app.activity(aid)['activity']['ready_at'])
        self.assertEqual(schedule.processing(self.store, aid)['status'], 'delayed')
        self.now += dt.timedelta(minutes=5)
        token = guard.record_attempt(self.store, row['id'])['attempt_token']
        result = guard.commit(self.store, row['id'], True, attempt_token=token)
        self.assertTrue(result['published']); self.assertFalse(result['staged'])
        self.assertEqual(self.published(aid)[0]['body'], body)
        self.assertEqual(self.published(aid)[0]['received_at'], self.store.stamp())
        self.assertIsNone(schedule.processing(self.store, aid))

    def test_deleted_prepared_derivatives_never_reappear_at_due(self):
        activity = self.prepare(); aid = activity['id']; row, _ = self.candidate(activity)
        with self.store.db(True) as db:
            guard.scrub(db, activity_ids=[aid], stamp=self.store.stamp())
            db.execute('INSERT INTO tombstones VALUES(?,?,?,?,?)',
                       ('fixture-deletion', 'activity_derivatives', aid, None, self.store.stamp()))
        self.at_due(); self.store.tick(); self.store.tick()
        self.assertEqual(self.published(aid), [])
        self.assertEqual(guard.snapshot(self.store, row['id'])['status'], 'deleted')
        self.assertIsNone(self.rows('learning_releases', 'activity_id=?', (aid,))[0]['activated_at'])
        self.assertEqual(self.rows('notifications', 'activity_id=?', (aid,)), [])

    def test_isolation_and_record_quarantine_prevent_creation_or_release(self):
        self.store.set_meta('isolated', 'true'); self.store.tick()
        self.assertEqual(self.rows('learning_releases'), [])
        self.store.set_meta('isolated', 'false')
        activity = self.prepare(); aid = activity['id']; self.candidate(activity)
        self.at_due(); self.store.set_meta('isolated', 'true'); self.store.tick()
        self.assertEqual(self.published(aid), [])
        self.store.set_meta('isolated', 'false')
        with self.store.db(True) as db:
            db.execute("INSERT OR REPLACE INTO records_meta VALUES('quarantine','1')")
        self.store.tick()
        self.assertEqual(self.published(aid), [])
        self.assertIsNone(self.rows('learning_releases', 'activity_id=?', (aid,))[0]['activated_at'])

    def test_recovered_private_approval_stays_on_hold(self):
        activity = self.prepare(); aid = activity['id']; row, _ = self.candidate(activity)
        with self.store.db(True) as db:
            guard.recover(db, stamp=self.store.stamp())
        self.assertEqual(guard.snapshot(self.store, row['id'])['status'], 'recovery_hold')
        self.at_due(); self.store.tick(recovery=True)
        self.assertEqual(self.published(aid), [])
        self.assertIsNone(self.app.activity(aid)['activity']['ready_at'])

    def test_manual_early_learning_releases_once_without_evening_notification(self):
        activity = self.prepare(); aid = activity['id']; _, body = self.candidate(activity)
        self.now += dt.timedelta(minutes=3)
        opened = self.store.ensure_activity('A1')
        self.assertEqual(opened['id'], aid)
        self.assertEqual(opened['origin'], 'manual')
        self.assertEqual(self.published(aid)[0]['received_at'], self.store.stamp())
        self.at_due(); self.store.tick()
        self.assertEqual([m['body'] for m in self.published(aid)], [body])
        self.assertEqual(self.rows('notifications', 'activity_id=?', (aid,)), [])

    def test_manual_open_during_pause_resumes_schedule_held_preparation(self):
        activity = self.prepare(); aid = activity['id']
        self.store.change_schedule('A1', '21:00', list(range(7)), '2026-10-24', '本人暂停定时提醒')
        schedule.tick(self.store)
        self.assertEqual({r['status'] for r in self.rows('jobs', 'target=?', (aid,))}, {'schedule_hold'})
        opened = self.store.ensure_activity('A1')
        self.store.tick()
        self.assertEqual(opened['id'], aid)
        self.assertEqual({r['status'] for r in self.rows('jobs', 'target=?', (aid,))}, {'pending'})
        self.assertEqual(self.rows('notifications', 'activity_id=?', (aid,)), [])
        self.assertEqual(next(s for s in self.store.schedules() if s['routine'] == 'A1')['paused_until'], '2026-10-24')

    def test_manual_open_immediately_after_preference_change_regenerates_with_truthful_receipt(self):
        activity = self.prepare(); aid = activity['id']; old, _ = self.candidate(activity)
        self.app.records.add_profile('每天增加一个概念辨析。', category='preference', scope='routine:A1',
                                     confirmed=True, user_input='本人要求')
        # No intervening scheduler tick: manual activation must reconcile inputs.
        self.store.ensure_activity('A1'); self.store.tick()
        self.assertEqual(len(self.rows('jobs', "target=? AND kind='learning'", (aid,))), 2)
        self.assertEqual(guard.snapshot(self.store, old['id'])['status'], 'cancelled')
        self.assertNotIn('已经核对并发布', self.app.action({'op': 'retry', 'activity_id': aid})['message'])
        self.assertIsNone(self.app.activity(aid)['activity']['ready_at'])
        self.assertNotEqual(self.worker.ensure_session(aid), old['session_id'])
        new, body = self.candidate(activity, approved=False)
        token = guard.record_attempt(self.store, new['id'])['attempt_token']
        self.assertTrue(guard.commit(self.store, new['id'], True, attempt_token=token)['published'])
        self.assertEqual([m['body'] for m in self.published(aid)], [body])
        self.assertEqual(self.rows('notifications', 'activity_id=?', (aid,)), [])

    def test_transport_does_not_send_hidden_lesson_with_stale_ready_flag_and_notice(self):
        activity = self.prepare(); aid = activity['id']
        with self.store.db(True) as db:
            db.execute('UPDATE activities SET ready_at=? WHERE id=?', (self.store.stamp(), aid))
            db.execute("INSERT INTO notifications VALUES(?,?, 'first','pending',?,0,NULL,NULL,?)",
                       ('fixture-hidden-notice', aid, self.store.stamp(), self.store.stamp()))
        visible = self.store.ensure_activity('A3', origin='scheduled')
        self.now += dt.timedelta(seconds=11)
        self.worker.config = lambda: {'notification': {'url': 'http://fixture.invalid'}, 'public_url': 'http://fixture.invalid'}
        with patch('dsh_personal.notifications.publish_notification', return_value='fixture-accepted') as send:
            self.worker.notifications()
        self.assertEqual(send.call_count, 1)
        self.assertTrue(send.call_args.args[1]['url'].endswith(visible['id']))
        self.assertEqual(self.rows('notifications', 'activity_id=?', (aid,))[0]['state'], 'pending')
        self.assertEqual(self.rows('notifications', 'activity_id=?', (visible['id'],))[0]['state'], 'accepted')

    def test_manual_history_is_preserved_and_future_public_request_stays_rejected(self):
        old = self.store.ensure_activity('A1', '2026-10-22')
        old_messages = self.store.activity(old['id'])['messages']
        self.prepare()
        with self.assertRaises(ValueError):
            self.app.action({'op': 'ensure_activity', 'routine': 'A1', 'date': '2026-10-25'})
        self.at_due(); self.store.tick()
        self.assertEqual(self.store.activity(old['id'])['messages'], old_messages)
        self.assertEqual(self.rows('notifications', 'activity_id=?', (old['id'],)), [])
        self.assertEqual(self.store.ensure_activity('A1', '2026-10-22')['id'], old['id'])

    def test_changed_requirements_queue_one_new_generation_rotate_session_and_block_stale_draft(self):
        old = self.store.ensure_activity('A3', '2026-10-23')
        receipt = self.store.submit(old['id'], 'preserve-original-sleep', '昨晚睡得还好。')
        prior = self.rows('messages', 'id=?', (receipt['receipt'],))[0]
        activity = self.prepare(); aid = activity['id']; row, _ = self.candidate(activity)
        self.app.records.add_profile('每日一学应增加概念辨析。', category='preference',
                                     scope='routine:A1', confirmed=True, user_input='本人要求')
        schedule.tick(self.store); schedule.tick(self.store)
        self.assertEqual(len(self.rows('jobs', "target=? AND kind='learning'", (aid,))), 2)
        self.assertNotEqual(self.worker.ensure_session(aid), row['session_id'])
        self.at_due(); self.store.tick()
        self.assertEqual(self.published(aid), [])
        self.assertIsNone(self.app.activity(aid)['activity']['ready_at'])
        self.assertEqual(self.rows('messages', 'id=?', (receipt['receipt'],))[0], prior)


if __name__ == '__main__':
    unittest.main()
