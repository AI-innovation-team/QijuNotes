"""Isolated listener/native readiness races; no live native or model calls."""
import datetime as dt
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.request

from dsh_personal.core import TZ
from dsh_personal.server import Application, ROOT, create_server, run_servers
from dsh_personal.worker import Worker
from runtime.dsh_bridge import DSHError


class ListenerReadinessTests(unittest.TestCase):
    def fixture(self):
        events = []
        worker = SimpleNamespace(start=Mock(side_effect=lambda: events.append('worker-start')),
                                 stop=threading.Event())
        app = SimpleNamespace(store=Mock(), config_path='fixture-config', gateway_key='fixture-key', worker=worker)

        def listener(name):
            return SimpleNamespace(
                serve_forever=Mock(side_effect=lambda: events.append(name + '-serve')),
                shutdown=Mock(side_effect=lambda: events.append(name + '-shutdown')),
                server_close=Mock(side_effect=lambda: events.append(name + '-close')))

        local, private, gateway = (listener(name) for name in ('local', 'private', 'gateway'))

        def bind(_app, host, _port):
            events.append(host + '-bind')
            return local if host == 'local' else private

        gateway_factory = Mock()
        gateway_factory.return_value.serve.side_effect = lambda _port: (events.append('gateway-bind'), gateway)[1]
        thread_factory = Mock()
        thread_factory.return_value.start.side_effect = lambda: events.append('private-thread-start')
        return app, events, local, private, gateway, bind, gateway_factory, thread_factory

    def test_every_listener_binds_before_worker_starts_and_cleanup_closes_all(self):
        app, events, local, private, gateway, bind, gw, threads = self.fixture()
        with patch('dsh_personal.server.create_server', side_effect=bind), \
             patch('dsh_personal.server.Gateway', gw), patch('dsh_personal.server.threading.Thread', threads), \
             patch('builtins.print'):
            run_servers(app, 'local', 1, 'private', 2)
        self.assertEqual(events[:5], ['local-bind', 'private-bind', 'gateway-bind', 'private-thread-start', 'worker-start'])
        self.assertTrue(app.worker.stop.is_set())
        for server in (local, private, gateway):
            server.server_close.assert_called_once()
        private.shutdown.assert_called_once()
        gateway.shutdown.assert_called_once()

    def test_private_bind_failure_never_starts_worker_or_gateway_and_closes_local(self):
        app, events, local, private, gateway, bind, gw, threads = self.fixture()

        def unavailable(_app, host, port):
            if host == 'private':
                raise OSError('synthetic private address not ready')
            return bind(_app, host, port)

        with patch('dsh_personal.server.create_server', side_effect=unavailable), \
             patch('dsh_personal.server.Gateway', gw), patch('dsh_personal.server.threading.Thread', threads):
            with self.assertRaises(OSError):
                run_servers(app, 'local', 1, 'private', 2)
        app.worker.start.assert_not_called()
        gw.assert_not_called()
        threads.assert_not_called()
        local.server_close.assert_called_once()
        local.serve_forever.assert_not_called()

    def test_gateway_bind_failure_closes_both_web_listeners_without_work(self):
        app, events, local, private, gateway, bind, gw, threads = self.fixture()
        gw.return_value.serve.side_effect = OSError('synthetic gateway bind conflict')
        with patch('dsh_personal.server.create_server', side_effect=bind), \
             patch('dsh_personal.server.Gateway', gw), patch('dsh_personal.server.threading.Thread', threads):
            with self.assertRaises(OSError):
                run_servers(app, 'local', 1, 'private', 2)
        app.worker.start.assert_not_called()
        threads.assert_not_called()
        local.server_close.assert_called_once()
        private.server_close.assert_called_once()

    def test_worker_start_failure_stops_partial_worker_and_closes_gateway(self):
        app, events, local, private, gateway, bind, gw, threads = self.fixture()
        app.worker.start.side_effect = RuntimeError('synthetic startup error')
        with patch('dsh_personal.server.create_server', side_effect=bind), \
             patch('dsh_personal.server.Gateway', gw), patch('dsh_personal.server.threading.Thread', threads):
            with self.assertRaises(RuntimeError):
                run_servers(app, 'local', 1, 'private', 2)
        self.assertTrue(app.worker.stop.is_set())
        gateway.shutdown.assert_called_once()
        gateway.server_close.assert_called_once()
        private.shutdown.assert_called_once()
        local.server_close.assert_called_once()

    def test_no_worker_mode_remains_available(self):
        app, events, local, private, gateway, bind, gw, threads = self.fixture()
        with patch('dsh_personal.server.create_server', side_effect=bind), \
             patch('dsh_personal.server.Gateway', gw), patch('builtins.print'):
            run_servers(app, 'local', 1, gateway_port=2, no_worker=True)
        app.worker.start.assert_not_called()
        self.assertFalse(app.worker.stop.is_set())
        local.serve_forever.assert_called_once()
        gateway.shutdown.assert_called_once()


class NativeFixture:
    def __init__(self):
        self.ready = False
        self.probes = 0
        self.session_calls = []
        self.submissions = []
        self.on_session = None
        self.probe_error = None

    def rpc(self, method, payload):
        assert method == 'session/list' and payload == {'_request': {}}
        self.probes += 1
        if self.probe_error:
            raise self.probe_error
        if not self.ready:
            raise DSHError('fixture native host not ready', 'dsh/auth-unavailable')
        return []

    def ensure_session(self, activity):
        self.session_calls.append(activity)
        if self.on_session:
            self.on_session()
        return 'fixture-session-' + activity

    def submit(self, sid, request_id, text, **kwargs):
        self.submissions.append((sid, request_id))
        return {'accepted': True}


class NativeReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='startup-fixture-')
        self.now = dt.datetime(2026, 10, 23, 12, 0, tzinfo=TZ)
        self.app = Application(self.temp.name, lambda: self.now)
        self.store = self.app.store
        self.bridge = NativeFixture()
        self.worker = Worker(self.store, self.bridge, self.app.records, lambda: {})
        self.activity = self.store.ensure_activity('A2', '2026-10-22')['id']
        with self.store.db(True) as db:
            db.execute('DELETE FROM jobs')
            self.store._enqueue(db, 'session', self.activity, 1)
        self.monotonic = 100.0
        self.clock_patch = patch('dsh_personal.worker.time.monotonic', side_effect=lambda: self.monotonic)
        self.clock_patch.start()

    def tearDown(self):
        self.clock_patch.stop()
        self.temp.cleanup()

    def advance(self, seconds=60):
        self.now += dt.timedelta(seconds=seconds)
        self.monotonic += seconds

    def job(self):
        with self.store.db() as db:
            return dict(db.execute("SELECT * FROM jobs WHERE kind='session'").fetchone())

    def test_native_not_ready_keeps_job_unclaimed_then_processes_once_after_recovery(self):
        for _ in range(5):
            self.assertFalse(self.worker.work_one())
            self.assertEqual((self.job()['status'], self.job()['attempts']), ('pending', 0))
            self.advance()
        self.assertEqual(self.bridge.session_calls, [])
        self.bridge.ready = True
        self.assertTrue(self.worker.work_one())
        self.assertEqual((self.job()['status'], self.job()['attempts']), ('done', 1))
        self.assertFalse(self.worker.work_one())
        self.assertEqual(self.bridge.session_calls, [self.activity])

    def test_unready_probe_is_bounded_and_local_summary_still_runs(self):
        with self.store.db(True) as db:
            self.store._enqueue(db, 'summary', self.activity, 1)
        with patch.object(self.store, 'summarize_literal') as summary, \
             patch.object(self.worker, 'sync_summary_refs'):
            self.assertTrue(self.worker.work_one())
            summary.assert_called_once_with(self.activity)
        self.assertEqual(self.job()['attempts'], 0)
        for _ in range(10):
            self.assertFalse(self.worker.work_one())
        self.assertEqual(self.bridge.probes, 1)

    def test_native_goes_down_after_probe_requeues_same_job_without_business_attempt(self):
        self.bridge.ready = True

        def disconnect():
            self.bridge.ready = False
            raise DSHError('fixture transport loss')

        self.bridge.on_session = disconnect
        job_id = self.job()['id']
        self.assertTrue(self.worker.work_one())
        self.assertEqual((self.job()['id'], self.job()['status'], self.job()['attempts']), (job_id, 'pending', 0))
        self.assertIsNone(self.job()['lease_until'])
        self.bridge.on_session = None
        self.bridge.ready = True
        self.advance()
        self.assertTrue(self.worker.work_one())
        self.assertEqual((self.job()['id'], self.job()['status'], self.job()['attempts']), (job_id, 'done', 1))
        self.assertFalse(self.worker.work_one())

    def check_bounded_failure(self, error):
        self.bridge.ready = True

        def fail():
            raise error

        self.bridge.on_session = fail
        for attempt in range(1, 4):
            self.assertTrue(self.worker.work_one())
            self.assertEqual(self.job()['attempts'], attempt)
            self.advance(120)
        self.assertEqual(self.job()['status'], 'failed')
        self.assertFalse(self.worker.work_one())
        self.assertEqual(len(self.bridge.session_calls), 3)

    def test_real_business_error_retains_three_attempt_limit(self):
        self.check_bounded_failure(ValueError('fixture invalid task data'))

    def test_ambiguous_native_error_with_healthy_host_is_not_retried_forever(self):
        self.check_bounded_failure(DSHError('fixture malformed business reply'))

    def test_nontransient_probe_error_is_not_hidden_as_an_unlimited_wait(self):
        self.bridge.probe_error = DSHError('fixture auth configuration mismatch', 'dsh/auth-mismatch')
        for attempt in range(1, 4):
            self.assertTrue(self.worker.work_one())
            self.assertEqual(self.job()['attempts'], attempt)
            self.advance(120)
        self.assertEqual(self.job()['status'], 'failed')
        self.assertFalse(self.worker.work_one())
        self.assertEqual(self.bridge.session_calls, [])

    def test_delegated_task_stays_queued_without_a_run_until_native_ready(self):
        self.worker.config = lambda: {'model': {'name': 'fixture'}}
        task_id = self.store.create_task('固定技术测试委托')['id']
        for _ in range(4):
            self.worker.dispatch_tasks()
            task = self.store.task(task_id)
            self.assertEqual((task['status'], task['version']), ('queued', 0))
            self.assertEqual(task['deliveries'], [])
            self.advance()
        self.bridge.ready = True
        self.worker.dispatch_tasks()
        task = self.store.task(task_id)
        self.assertEqual((task['status'], task['version']), ('running', 1))
        self.assertEqual(len(task['deliveries']), 1)
        self.assertEqual(len(self.bridge.submissions), 1)
        self.worker.dispatch_tasks()
        self.assertEqual(len(self.bridge.submissions), 1)

    def test_failed_listener_bind_does_not_claim_existing_durable_jobs(self):
        original = self.job()
        local = Mock()
        with patch('dsh_personal.server.create_server', side_effect=[local, OSError('fixture private bind failure')]), \
             patch('dsh_personal.server.Gateway') as gateway, patch.object(self.app.worker, 'start') as start:
            with self.assertRaises(OSError):
                run_servers(self.app, 'local', 1, 'private', 2)
        self.assertEqual(self.job(), original)
        start.assert_not_called()
        gateway.assert_not_called()
        local.server_close.assert_called_once()


class GenericGuideTests(unittest.TestCase):
    def test_new_material_guides_serve_only_the_fixed_generic_files(self):
        guides = {
            '/guide/chatgpt-material-instructions': 'chatgpt-material-instructions.md',
            '/guide/kimi-material-instructions': 'kimi-material-instructions.md',
            '/guide/material-batch-example': 'personal-material-batch-example.json',
        }
        with tempfile.TemporaryDirectory(prefix='guide-fixture-') as temp:
            app = Application(temp)
            server = create_server(app, '127.0.0.1', 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for route, filename in guides.items():
                    with self.subTest(route=route), urllib.request.urlopen(
                            'http://127.0.0.1:' + str(server.server_port) + route) as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.headers.get_content_type(), 'text/plain')
                        self.assertEqual(response.read(), (ROOT / 'docs' / 'templates' / filename).read_bytes())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)


if __name__ == '__main__':
    unittest.main()
