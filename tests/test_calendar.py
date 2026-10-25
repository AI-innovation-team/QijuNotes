"""Calendar concurrency and reminder safety, with only isolated synthetic data."""
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from dsh_personal.calendar import Calendar
from dsh_personal.core import Store, UTC


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dsh-calendar-test-')
        self.addCleanup(self.temp.cleanup)
        self.now = dt.datetime(2026, 10, 23, 4, tzinfo=UTC)
        self.path = Path(self.temp.name) / 'personal.sqlite'
        self.store = Store(self.path, clock=lambda: self.now)
        self.calendar = Calendar(self.store)
        self.config = {'notification': {'url': 'https://notification.invalid'},
                       'public_url': 'https://diary.invalid'}

    def values(self, **changes):
        values = dict(title='合成日程', start_at='2026-10-23T13:00:00+08:00',
                      end_at='2026-10-23T14:00:00+08:00', submission_id='calendar-test-1')
        values.update(changes)
        return values

    def create(self, **changes):
        return self.calendar.save(**self.values(**changes))

    def counts(self):
        with self.store.db() as db:
            return tuple(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
                         for table in ('calendar_events', 'calendar_requests', 'calendar_changes'))

    def test_week_range_crosses_month_and_includes_overlap_at_edges(self):
        crossing=self.create(start_at='2026-10-25T23:30:00+08:00',end_at='2026-10-26T00:30:00+08:00')
        boundary=self.create(submission_id='boundary',start_at='2026-11-02T00:00:00+08:00',end_at='2026-11-02T01:00:00+08:00')
        last=self.create(submission_id='last',start_at='2026-11-01T23:00:00+08:00',end_at='2026-11-02T00:30:00+08:00')
        events=self.calendar.events(start='2026-10-26',end='2026-11-01')
        self.assertEqual({e['id'] for e in events},{crossing['id'],last['id']})
        self.assertNotIn(boundary['id'],{e['id'] for e in events})
        for start,end in [('2026-11-01','2026-10-26'),('2026-10-26',None),('2026-10-26','2028-01-01')]:
            with self.assertRaises(ValueError):self.calendar.events(start=start,end=end)

    def test_creation_does_not_modify_daily_routines_or_claim_completion(self):
        schedules = self.store.schedules()
        event = self.create()
        self.assertEqual(event['status'], 'planned')
        self.assertEqual(event['notification_state'], 'off')
        self.assertEqual(self.calendar.completed_entries(), [])
        self.assertEqual(self.store.schedules(), schedules)
        self.now += dt.timedelta(days=2)
        self.calendar.reminders(self.config, publisher=lambda *_: self.fail('No reminder requested'))
        self.assertEqual(self.calendar.get(event['id'])['status'], 'planned')
        self.assertEqual(self.calendar.completed_entries(), [])

    def test_timezone_normalization_and_invalid_time_or_reminder_rejected(self):
        event = self.create(start_at='2026-10-23T01:00:00-04:00',
                            end_at='2026-10-23T06:00:00Z')
        self.assertEqual(event['start_at'], '2026-10-23T05:00:00+00:00')
        for field, value in [('start_at','2026-10-23T13:00:00'), ('end_at','bad'),
                             ('reminder_minutes',True), ('reminder_minutes',-1),
                             ('reminder_minutes',10081), ('all_day','true'),
                             ('title','  ')]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.create(submission_id='invalid-'+field, **{field:value})
        with self.assertRaises(ValueError):
            self.create(submission_id='invalid-end', end_at='2026-10-23T13:00:00+08:00')
        self.assertEqual(self.counts(), (1,1,1))

    def test_month_overlap_uses_shanghai_boundaries_and_excludes_touching_edges(self):
        self.create(title='跨月', start_at='2026-10-31T23:30:00+08:00',
                    end_at='2026-11-01T00:30:00+08:00')
        touching = self.create(title='止于边界', submission_id='touching',
                    start_at='2026-10-31T22:00:00+08:00', end_at='2026-11-01T00:00:00+08:00')
        december = self.create(title='十二月', submission_id='december',
                    start_at='2026-12-01T00:00:00+08:00', end_at='2026-12-01T01:00:00+08:00')
        self.assertEqual({e['title'] for e in self.calendar.events('2026-11')}, {'跨月'})
        self.assertIn(touching['id'], {e['id'] for e in self.calendar.events('2026-10')})
        self.assertIn(december['id'], {e['id'] for e in self.calendar.events('2026-12')})
        for month in ('2026-13', '2026-1', '2026-11-02', 'invalid'):
            with self.subTest(month=month), self.assertRaises(ValueError):
                self.calendar.events(month)

    def test_leap_month_and_all_day_intervals(self):
        event = self.create(all_day=True, start_at='2028-02-29T00:00:00+08:00',
                            end_at='2028-03-01T00:00:00+08:00')
        self.assertTrue(event['all_day'])
        self.assertEqual([e['id'] for e in self.calendar.events('2028-02')], [event['id']])
        self.assertEqual(self.calendar.events('2028-03'), [])

    def test_create_lost_receipt_retry_after_reminder_time_returns_original(self):
        values = self.values(reminder_minutes=30)
        original = self.calendar.save(**values)
        self.now = dt.datetime(2026,10,23,5,30,tzinfo=UTC)
        restarted = Calendar(Store(self.path, clock=lambda:self.now))
        duplicate = restarted.save(**values)
        self.assertEqual(duplicate['id'], original['id'])
        self.assertEqual(duplicate['version'], 1)
        self.assertEqual(self.counts(), (1,1,1))
        with self.assertRaisesRegex(ValueError, '同一提交编号'):
            restarted.save(**{**values,'title':'改写重试'})

    def test_concurrent_create_same_submission_yields_one_event_and_change(self):
        start = threading.Barrier(6)
        def create():
            start.wait(timeout=5)
            return self.calendar.save(**self.values())
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _:create(), range(6)))
        self.assertEqual(len({r['id'] for r in results}), 1)
        self.assertEqual(self.counts(), (1,1,1))

    def test_concurrent_reminder_claim_sends_once_and_uses_private_body(self):
        event = self.create(title='私密事件标题', notes='私密事件内容', reminder_minutes=30)
        self.now += dt.timedelta(minutes=31)
        entered, release = threading.Event(), threading.Event()
        calls = []
        def publish(config,payload):
            calls.append(payload)
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return 'accepted-test-receipt'
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.calendar.reminders,self.config,publish)
            self.assertTrue(entered.wait(timeout=5))
            second = pool.submit(self.calendar.reminders,self.config,publish)
            second.result(timeout=5)
            release.set()
            first.result(timeout=5)
        self.assertEqual(len(calls),1)
        self.assertNotIn('私密事件', calls[0]['title']+calls[0]['message'])
        self.assertEqual(calls[0]['url'], 'https://diary.invalid/#calendar/'+event['id'])
        self.assertEqual(calls[0]['idempotency_key'],event['id']+'-1')
        self.assertEqual(self.calendar.get(event['id'])['notification_state'],'accepted')
        self.calendar.reminders(self.config,publisher=lambda *_:self.fail('Accepted reminder replayed'))

    def test_unknown_publish_result_is_not_replayed_or_exposed(self):
        event = self.create(reminder_minutes=30)
        self.now += dt.timedelta(minutes=31)
        calls=[]
        def failed(*_):
            calls.append(1)
            raise TimeoutError('synthetic secret must not leak')
        self.calendar.reminders(self.config,failed)
        saved=self.calendar.get(event['id'])
        self.assertEqual(saved['notification_state'],'unknown')
        self.assertNotIn('synthetic secret',saved['notification_receipt'])
        restarted=Calendar(Store(self.path,clock=lambda:self.now))
        restarted.recover_uncertain()
        restarted.reminders(self.config,failed)
        self.assertEqual(calls,[1])

    def test_crash_during_send_recovers_to_unknown_without_resend(self):
        event=self.create(reminder_minutes=30)
        with self.store.db(True) as db:
            db.execute("UPDATE calendar_events SET notification_state='sending' WHERE id=?",(event['id'],))
        self.now += dt.timedelta(minutes=31)
        restarted=Calendar(Store(self.path,clock=lambda:self.now))
        restarted.recover_uncertain()
        self.assertEqual(restarted.get(event['id'])['notification_state'],'unknown')
        restarted.reminders(self.config,publisher=lambda *_:self.fail('Unknown attempt replayed'))

    def test_expired_and_unconfigured_reminders_do_not_claim_delivery_or_completion(self):
        expired=self.create(reminder_minutes=30)
        self.now=dt.datetime(2026,10,23,6,1,tzinfo=UTC)
        self.calendar.reminders(self.config,publisher=lambda *_:self.fail('Expired event replayed'))
        self.assertEqual(self.calendar.get(expired['id'])['notification_state'],'expired')
        page=self.create(submission_id='page-only',start_at='2026-10-23T16:00:00+08:00',
                         end_at='2026-10-23T17:00:00+08:00',reminder_minutes=30)
        self.now=dt.datetime(2026,10,23,7,31,tzinfo=UTC)
        self.calendar.reminders({},publisher=lambda *_:self.fail('No notification config'))
        self.assertEqual(self.calendar.get(page['id'])['notification_state'],'page_only')
        self.calendar.reminders(self.config,publisher=lambda *_:self.fail('Old page-only reminder replayed'))
        self.assertEqual(self.calendar.completed_entries(),[])

    def test_edit_requires_current_version_and_keeps_change_history(self):
        original=self.create(reminder_minutes=30)
        for invalid in (None, True, '1', -1):
            with self.subTest(expected_version=invalid), self.assertRaises(ValueError):
                self.calendar.save(**self.values(event_id=original['id'],expected_version=invalid))
            with self.subTest(status_version=invalid), self.assertRaises(ValueError):
                self.calendar.change_status(original['id'],invalid,'cancelled')
        edited=self.calendar.save(**self.values(event_id=original['id'],expected_version=1,
                                               title='修订日程',reminder_minutes=None))
        self.assertEqual(edited['version'],2)
        self.assertEqual(edited['notification_state'],'off')
        with self.assertRaisesRegex(ValueError,'已更新'):
            self.calendar.save(**self.values(event_id=original['id'],expected_version=1,title='旧页覆盖'))
        with self.assertRaises(ValueError):
            self.calendar.change_status(original['id'],1,'cancelled')
        self.assertEqual(self.counts(),(1,1,2))
        with self.store.db() as db:
            self.assertEqual([r['action'] for r in db.execute('SELECT action FROM calendar_changes ORDER BY version')],['create','edit'])

    def test_concurrent_edit_and_cancel_allow_only_one_cas_transition(self):
        event=self.create()
        start=threading.Barrier(2)
        def edit():
            start.wait(timeout=5)
            return self.calendar.save(**self.values(event_id=event['id'],expected_version=1,title='并发修改'))
        def cancel():
            start.wait(timeout=5)
            return self.calendar.change_status(event['id'],1,'cancelled')
        outcomes=[]
        with ThreadPoolExecutor(max_workers=2) as pool:
            for future in (pool.submit(edit),pool.submit(cancel)):
                try:outcomes.append(future.result(timeout=5))
                except ValueError:outcomes.append(None)
        self.assertEqual(sum(item is not None for item in outcomes),1)
        self.assertEqual(self.calendar.get(event['id'])['version'],2)
        self.assertEqual(self.counts(),(1,1,2))

    def test_edit_and_cancel_blocked_while_notification_claim_is_sending(self):
        event=self.create(reminder_minutes=30)
        with self.store.db(True) as db:
            db.execute("UPDATE calendar_events SET notification_state='sending' WHERE id=?",(event['id'],))
        with self.assertRaisesRegex(ValueError,'正在发送'):
            self.calendar.save(**self.values(event_id=event['id'],expected_version=1,title='发送中修改'))
        with self.assertRaisesRegex(ValueError,'正在发送'):
            self.calendar.change_status(event['id'],1,'cancelled')
        self.assertEqual(self.counts(),(1,1,1))

    def test_explicit_completion_and_cancellation_are_versioned_and_retryable(self):
        completed=self.create()
        self.now=dt.datetime(2026,10,23,16,1,tzinfo=UTC)
        result=self.calendar.change_status(completed['id'],1,'completed')
        retry=self.calendar.change_status(completed['id'],1,'completed')
        self.assertEqual(result,retry)
        entries=self.calendar.completed_entries()
        self.assertEqual(len(entries),1)
        self.assertEqual(entries[0]['business_date'],'2026-10-24')
        self.assertEqual(entries[0]['source_refs'],['calendar:'+completed['id']])
        self.assertEqual(entries[0]['recorded_at'],result['completed_at'])
        cancelled=self.create(submission_id='cancel-me')
        self.calendar.change_status(cancelled['id'],1,'cancelled')
        self.assertEqual(self.calendar.change_status(cancelled['id'],1,'cancelled')['status'],'cancelled')
        self.assertEqual(len(self.calendar.completed_entries()),1)
        with self.assertRaises(ValueError):self.calendar.change_status(cancelled['id'],2,'completed')
        with self.assertRaises(ValueError):self.calendar.change_status(completed['id'],2,'planned')
        with self.assertRaises(ValueError):self.calendar.save(**self.values(event_id=completed['id'],expected_version=2))

    def test_creation_retry_after_cancellation_does_not_resurrect_event(self):
        event=self.create(reminder_minutes=30)
        self.calendar.change_status(event['id'],1,'cancelled')
        self.now += dt.timedelta(days=1)
        retry=self.create(reminder_minutes=30)
        self.assertEqual(retry['id'],event['id'])
        self.assertEqual(retry['status'],'cancelled')
        self.assertEqual(retry['version'],2)
        self.assertEqual(self.counts(),(1,1,2))

    def test_old_snapshot_latest_ledger_preserves_status_receipts_title_and_no_replay(self):
        from dsh_personal.chronicle import Chronicle
        from dsh_personal.records import RecordsStore
        from dsh_personal.operations import export_bundle, recovery_ledger, restore_bundle, _merge_core_ledger
        root=Path(self.temp.name)
        config={'public_url':'http://127.0.0.1:18780','model':{},'notification':{},'backup':{}}
        (root/'config.json').write_text(json.dumps(config))
        records=RecordsStore(self.path,clock=lambda:self.now)
        app=SimpleNamespace(data=root,store=self.store,records=records,config=lambda:config)
        chronicle=Chronicle(self.store,records)
        chronicle.save_settings('旧称谓')
        cancel=self.create(submission_id='ledger-cancel',reminder_minutes=30)
        complete=self.create(submission_id='ledger-complete',reminder_minutes=30)
        unknown=self.create(submission_id='ledger-unknown',reminder_minutes=30)
        accepted=self.create(submission_id='ledger-accepted',reminder_minutes=30)
        pending=self.create(submission_id='ledger-pending',reminder_minutes=30,
                            start_at='2026-10-23T15:00:00+08:00',end_at='2026-10-23T16:00:00+08:00')
        old_ledger=recovery_ledger(app)
        snapshot=export_bundle(app)
        self.now += dt.timedelta(minutes=31)
        self.calendar.change_status(cancel['id'],1,'cancelled')
        self.calendar.change_status(complete['id'],1,'completed')
        chronicle.save_settings('新称谓')
        def publish(_,payload):
            if payload['idempotency_key'].startswith(unknown['id']):
                raise TimeoutError('Synthetic ambiguous send')
            return 'durable-provider-receipt'
        self.calendar.reminders(self.config,publish)
        latest=recovery_ledger(app)
        self.assertEqual(len(latest['core']['calendar_events']),5)
        destination=root/'restored-isolated'
        result=restore_bundle(snapshot,destination,latest_ledger=latest)
        self.assertTrue(result['isolated'])
        restored_store=Store(destination/'personal.sqlite',clock=lambda:self.now)
        restored=Calendar(restored_store)
        expected={cancel['id']:('cancelled','cancelled'),complete['id']:('completed','cancelled'),
                  unknown['id']:('planned','unknown'),accepted['id']:('planned','accepted'),
                  pending['id']:('planned','recovery_unknown')}
        for ident,states in expected.items():
            item=restored.get(ident)
            self.assertEqual((item['status'],item['notification_state']),states)
        self.assertEqual(restored.get(accepted['id'])['notification_receipt'],'durable-provider-receipt')
        restored_records=RecordsStore(destination/'personal.sqlite',clock=lambda:self.now)
        self.assertEqual(Chronicle(restored_store,restored_records).settings()['user_title'],'新称谓')
        restored.reminders(self.config,publisher=lambda *_:self.fail('Restored action replayed'))
        # Reapplying an older pending ledger must preserve both the terminal
        # state and the evidence proving its already attempted external effect.
        _merge_core_ledger(restored_store,old_ledger)
        self.assertEqual(restored.get(accepted['id'])['notification_state'],'accepted')
        self.assertEqual(restored.get(accepted['id'])['notification_receipt'],'durable-provider-receipt')
        self.assertEqual(restored.get(unknown['id'])['notification_state'],'unknown')
        self.assertTrue(restored.get(unknown['id'])['notification_receipt'])
        self.assertEqual(restored.get(cancel['id'])['status'],'cancelled')
        self.assertEqual(restored.get(complete['id'])['status'],'completed')
        restored.reminders(self.config,publisher=lambda *_:self.fail('Merged old action replayed'))


if __name__=='__main__':
    unittest.main()
