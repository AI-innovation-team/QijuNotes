"""Notification readiness through authenticated HTTP, with isolated fixtures only."""
import http.cookiejar
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request

from dsh_personal.diagnostics import notification_configuration_id
from dsh_personal.server import Application, create_server


class NotificationHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = Application(self.root / 'data')
        self.server = create_server(self.app, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        request = urllib.request.Request(self.base + '/api/login', json.dumps({'token': self.app.access_token}).encode(),
            {'Content-Type': 'application/json', 'X-DSH-Client': 'web', 'Origin': self.base})
        with self.opener.open(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
        self.token = self.root / 'PRIVATE-CREDENTIAL-PATH'
        self.token_value = 'SYNTHETIC-PRIVATE-NOTIFICATION-TOKEN'
        self.verified_at = '2026-10-23T10:30:00+08:00'

    def shutdown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def configure(self, proof=False):
        self.token.write_text(self.token_value)
        self.token.chmod(0o600)
        conf = self.app.config()
        conf.update(public_url='http://127.0.0.1:19980', notification={
            'backend': 'ntfy', 'url': 'http://127.0.0.1:19983',
            'topic': 'isolated-test', 'token_file': str(self.token)})
        self.app.config_path.write_text(json.dumps(conf))
        if proof:
            evidence = {'type': 'real_device_user_report', 'locked_screen_received': True,
                'correct_activity_opened': True, 'verified_at': self.verified_at,
                'configuration_id': notification_configuration_id(conf),
                'receipt': {'backend': 'ntfy', 'http_status': 200, 'state': 'accepted',
                    'message_id': 'PRIVATE-RECEIPT-ID', 'sequence_id': 'PRIVATE-SEQUENCE-ID',
                    'raw_body': 'PRIVATE-RECEIPT-RAW-BODY'},
                'untrusted_detail': 'PRIVATE-REPORT-DETAIL'}
            self.assertIsNotNone(evidence['configuration_id'])
            path = self.app.data / 'notification-verification.json'
            path.write_text(json.dumps(evidence))
            path.chmod(0o600)
        return conf

    def status(self):
        with self.opener.open(self.base + '/api/state', timeout=10) as response:
            self.assertEqual(response.status, 200)
            raw = response.read().decode()
        for private in [self.token_value, str(self.token), str(self.root), self.app.access_token,
                        self.app.gateway_key, 'PRIVATE-RECEIPT-ID', 'PRIVATE-SEQUENCE-ID',
                        'PRIVATE-RECEIPT-RAW-BODY', 'PRIVATE-REPORT-DETAIL']:
            self.assertNotIn(private, raw)
        return json.loads(raw)['health']['notification']

    def assert_pending(self):
        text = self.status()
        self.assertIn('已配置', text)
        self.assertIn('待真机锁屏接收及活动跳转验证', text)
        self.assertNotIn('已通过', text)
        self.assertNotIn(self.verified_at, text)

    def test_without_channel_reports_unconfigured(self):
        text = self.status()
        self.assertIn('通知通道未配置', text)
        self.assertIn('仅页面可见', text)
        self.assertNotIn('已通过', text)

    def test_configured_channel_without_proof_waits_for_device(self):
        self.configure()
        self.assert_pending()

    def test_matching_proof_exposes_only_verification_time_and_limits(self):
        self.configure(proof=True)
        text = self.status()
        self.assertIn('已通过一次真机锁屏接收及正确活动跳转', text)
        self.assertIn(self.verified_at, text)
        self.assertIn('不代表长期后台稳定或全体验验收', text)
        self.assertIn('配置或凭据变化后需重验', text)

    def test_public_url_change_invalidates_proof_on_next_http_request(self):
        conf = self.configure(proof=True)
        self.assertIn('已通过', self.status())
        conf['public_url'] = 'http://127.0.0.1:19981'
        self.app.config_path.write_text(json.dumps(conf))
        self.assert_pending()

    def test_credential_rotation_invalidates_proof_on_next_http_request(self):
        self.configure(proof=True)
        self.assertIn('已通过', self.status())
        self.token.write_text(self.token_value + '-ROTATED')
        self.assert_pending()


if __name__ == '__main__':
    unittest.main()
