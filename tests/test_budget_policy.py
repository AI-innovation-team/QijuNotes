import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from dsh_personal.core import Store


class BudgetPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / 'personal.sqlite'
        self.store = Store(self.path)

    def tearDown(self):
        self.temporary.cleanup()

    def test_explicit_opt_out_keeps_unknown_and_settled_usage_and_survives_reopen(self):
        first = self.store.reserve(60000)
        self.store.settle(first, None, 'unconfirmed')
        with self.assertRaises(RuntimeError):
            self.store.reserve(1)
        self.store.set_meta('background_limit_enforced', 'false')
        self.store = Store(self.path)
        second = self.store.reserve(2000000, minimum_daily_remaining=30000)
        self.store.settle(second, 1200000, 'upstream_response')
        budget = self.store.budget()
        self.assertFalse(budget['background_limit_enforced'])
        self.assertIsNone(budget['daily_limit'])
        self.assertIsNone(budget['monthly_limit'])
        self.assertEqual(budget['configured_daily_limit'], 60000)
        self.assertEqual(budget['configured_monthly_limit'], 1000000)
        self.assertEqual(len(budget['usage']), 1)
        self.assertEqual(budget['usage'][0]['settled'], 1200000)
        self.assertEqual(budget['usage'][0]['reserved'], 60000)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT state FROM usage WHERE id=?', (first,)).fetchone()[0], 'unknown')

    def test_reenabling_uses_original_limits_and_all_retained_usage(self):
        self.store.set_meta('daily_tokens', 1000)
        self.store.set_meta('background_limit_enforced', 'false')
        self.store.settle(self.store.reserve(1200), 1200)
        self.store.set_meta('background_limit_enforced', 'true')
        with self.assertRaises(RuntimeError):
            self.store.reserve(1)
        self.assertEqual(self.store.budget()['daily_limit'], 1000)

    def test_absent_or_malformed_policy_never_silently_disables_limits(self):
        self.store.reserve(60000)
        for value in (None, '', 'False', '0', 'invalid', 'null'):
            if value is not None:
                self.store.set_meta('background_limit_enforced', value)
            with self.subTest(value=value):
                self.assertTrue(self.store.budget()['background_limit_enforced'])
                with self.assertRaises(RuntimeError):
                    self.store.reserve(1)

    def test_concurrent_unlimited_requests_are_still_individually_recorded(self):
        self.store.set_meta('background_limit_enforced', 'false')
        def reserve(_):
            return self.store.reserve(100000, minimum_daily_remaining=30000)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            receipts = list(pool.map(reserve, range(12)))
        self.assertEqual(len(set(receipts)), 12)
        budget = self.store.budget()
        self.assertEqual(budget['usage'][0]['reserved'], 1200000)
        self.assertEqual(budget['usage'][0]['kind'], 'background')
        self.store.reserve(1, 'foreground')
        self.store.reserve(1, 'delegated')
        self.assertEqual({r['kind'] for r in self.store.budget()['usage']}, {'background', 'foreground', 'delegated'})
