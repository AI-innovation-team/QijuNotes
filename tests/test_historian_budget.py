import concurrent.futures
import datetime as dt
import tempfile
import threading
import unittest
from pathlib import Path

from dsh_personal.core import Store, TZ, BudgetRequestTooLarge
from dsh_personal.gateway import HISTORIAN_DAILY_HEADROOM


class HistorianBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = dt.datetime(2026, 10, 23, 15, tzinfo=TZ)
        self.store = Store(Path(self.tmp.name) / 'personal.sqlite', lambda: self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def historian(self, tokens):
        return self.store.reserve(tokens, minimum_daily_remaining=HISTORIAN_DAILY_HEADROOM)

    def rows(self):
        with self.store.db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM usage')]

    def test_concurrent_bulk_refinement_cannot_take_daily_routine_headroom(self):
        existing = self.store.reserve(1000)
        self.store.settle(existing, 1000)

        def request(_):
            try:
                return self.historian(1000)
            except RuntimeError:
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(request, range(40)))
        self.assertEqual(sum(bool(rid) for rid in results), 29)
        rows = self.rows()
        self.assertEqual(sum(r['actual'] if r['actual'] is not None else r['reserved'] for r in rows), 30000)
        self.assertEqual({r['kind'] for r in rows}, {'background'})
        # Routine work can use precisely the protected remainder, but cannot
        # exceed the original background cap once it has done so.
        self.store.reserve(30000)
        with self.assertRaises(RuntimeError):
            self.store.reserve(1)

    def test_request_larger_than_empty_period_is_permanent_not_daily_backoff(self):
        with self.assertRaises(BudgetRequestTooLarge):
            self.historian(30001)
        self.assertEqual(self.rows(), [])
        self.store.set_meta('monthly_tokens', 10000)
        with self.assertRaises(BudgetRequestTooLarge):
            self.historian(10001)
        self.assertEqual(self.rows(), [])

    def test_unknown_usage_and_existing_reservations_stay_counted(self):
        unknown = self.store.reserve(10000)
        self.store.settle(unknown, None, 'unconfirmed')
        self.historian(20000)
        before = len(self.rows())
        with self.assertRaisesRegex(RuntimeError, '每日活动'):
            self.historian(1)
        self.assertEqual(len(self.rows()), before)
        self.store.settle(unknown, 4000, 'later_confirmed')
        self.historian(6000)
        with self.assertRaises(RuntimeError):
            self.historian(1)

    def test_monthly_limit_still_applies_and_next_month_gets_its_own_budget(self):
        self.store.set_meta('monthly_tokens', 45000)
        prior = self.store.reserve(30000)
        self.store.settle(prior, 30000)
        self.now += dt.timedelta(days=1)
        with self.assertRaises(RuntimeError):
            self.historian(20000)
        self.historian(15000)
        self.now = dt.datetime(2026, 11, 1, 0, 0, tzinfo=TZ)
        current = self.historian(30000)
        row = next(r for r in self.rows() if r['id'] == current)
        self.assertEqual((row['day'], row['month']), ('2026-11-01', '2026-11'))

    def test_foreground_and_delegated_requests_are_not_reclassified_or_capped(self):
        self.historian(30000)
        self.store.reserve(30000)
        self.store.reserve(100000, 'foreground')
        self.store.reserve(100000, 'delegated')
        self.assertEqual([r['kind'] for r in self.rows()],
                         ['background', 'background', 'foreground', 'delegated'])
        with self.assertRaises(RuntimeError):
            self.historian(1)

    def test_daily_limit_configuration_is_not_raised_to_make_room(self):
        self.store.set_meta('daily_tokens', 20000)
        with self.assertRaises(RuntimeError):
            self.historian(1)
        self.assertEqual(self.rows(), [])
        self.store.reserve(20000)
        with self.assertRaises(RuntimeError):
            self.store.reserve(1)
        self.assertEqual(self.store.budget()['daily_limit'], 20000)

    def test_reservation_waiting_for_write_lock_uses_new_shanghai_date(self):
        self.now = dt.datetime(2026, 10, 31, 23, 59, 59, tzinfo=TZ)
        sampled = threading.Event()
        entered = threading.Event()

        def clock():
            sampled.set()
            return self.now

        self.store.clock = clock

        def reserve():
            entered.set()
            return self.historian(1000)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            with self.store.db(True):
                pending = pool.submit(reserve)
                self.assertTrue(entered.wait(1))
                self.assertFalse(sampled.wait(.1))
                self.now = dt.datetime(2026, 11, 1, 0, 0, 1, tzinfo=TZ)
            rid = pending.result(timeout=3)
        row = next(r for r in self.rows() if r['id'] == rid)
        self.assertEqual((row['day'], row['month']), ('2026-11-01', '2026-11'))
        self.assertEqual(dt.datetime.fromisoformat(row['created_at']).astimezone(TZ), self.now)

    def test_invalid_headroom_cannot_turn_into_an_extra_allowance(self):
        for invalid in (-1, True, 1.5, '30000'):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.store.reserve(1, minimum_daily_remaining=invalid)
        with self.assertRaises(ValueError):
            self.store.reserve(1, 'foreground', minimum_daily_remaining=30000)
        self.assertEqual(self.rows(), [])


if __name__ == '__main__':
    unittest.main()
