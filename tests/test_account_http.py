"""Real loopback HTTP boundaries for account setup, migration and durable cookies."""
import http.client
from http import cookies
import json
from pathlib import Path
import tempfile
import threading
import unittest

from dsh_personal.server import Application, create_server


class AccountHTTPTests(unittest.TestCase):
    USER = 'http-fixture-user'
    PASSWORD = 'http-fixture-password-only'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / 'data'
        self.start()
        self.addCleanup(self.stop)

    def start(self):
        self.app = Application(self.data)
        self.server = create_server(self.app, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.host = '127.0.0.1:' + str(self.port)
        self.origin = 'http://' + self.host

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, payload=None, cookie=None, overrides=None):
        headers = {'Host': self.host}
        if payload is not None:
            headers.update({'X-DSH-Client': 'web', 'Content-Type': 'application/json', 'Origin': self.origin})
        if cookie:
            headers['Cookie'] = cookie
        for key, value in (overrides or {}).items():
            if value is None:
                headers.pop(key, None)
            else:
                headers[key] = value
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            connection.request('GET' if payload is None else 'POST', path,
                               None if payload is None else json.dumps(payload).encode(), headers)
            response = connection.getresponse()
            raw = response.read()
            content = json.loads(raw) if response.getheader('Content-Type', '').startswith('application/json') else raw
            return response.status, content, dict(response.getheaders())
        finally:
            connection.close()

    def setup_account(self, **params):
        status, body, headers = self.request('/api/auth/setup', {'username': self.USER, 'password': self.PASSWORD, **params})
        self.assertEqual(200, status, body)
        return headers['Set-Cookie'].split(';', 1)[0], headers

    def test_first_setup_is_local_and_returns_http_only_persistent_cookie(self):
        status, body, _ = self.request('/api/auth/status')
        self.assertEqual(200, status)
        self.assertEqual({'configured': False, 'authenticated': False, 'setup_available': True}, body)
        cookie, headers = self.setup_account()
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        self.assertIn('Max-Age=2592000', headers['Set-Cookie'])
        self.assertNotIn(self.PASSWORD, str(headers))
        status, body, _ = self.request('/api/auth/status', cookie=cookie)
        self.assertEqual(200, status)
        self.assertEqual(self.USER, body['username'])
        self.assertTrue(body['authenticated'])
        self.assertFalse(body['setup_available'])
        self.assertEqual(200, self.request('/api/state', cookie=cookie)[0])

    def test_setup_rejects_remote_host_proxies_and_absent_origin(self):
        payload = {'username': self.USER, 'password': self.PASSWORD}
        variants = [
            {'Host': '100.64.0.2:' + str(self.port), 'Origin': 'http://100.64.0.2:' + str(self.port)},
            {'Forwarded': 'for=100.64.0.2'}, {'X-Forwarded-For': '100.64.0.2'},
            {'X-Forwarded-Host': 'localhost'}, {'Origin': None},
        ]
        for headers in variants:
            with self.subTest(headers=list(headers)):
                status, _, _ = self.request('/api/auth/setup', payload, overrides=headers)
                self.assertEqual(403, status)
                self.assertFalse(self.app.auth.configured())
        _, status, _ = self.request('/api/auth/status', overrides={'Host': '100.64.0.2:' + str(self.port)})
        self.assertFalse(status['setup_available'])

    def test_setup_rejects_malformed_or_different_scheme_origin(self):
        payload = {'username': self.USER, 'password': self.PASSWORD}
        for origin in ['ftp://' + self.host, 'https://' + self.host, self.origin + '/path',
                       self.origin + '?query=1', self.origin + '#fragment', 'null']:
            with self.subTest(origin=origin):
                self.assertEqual(403, self.request('/api/auth/setup', payload, overrides={'Origin': origin})[0])
                self.assertFalse(self.app.auth.configured())

    def test_setup_cannot_replace_account_and_old_key_or_memory_session_is_disabled(self):
        status, _, headers = self.request('/api/login', {'token': self.app.access_token})
        self.assertEqual(200, status)
        legacy_cookie = headers['Set-Cookie'].split(';', 1)[0]
        self.assertEqual(200, self.request('/api/state', cookie=legacy_cookie)[0])
        cookie, _ = self.setup_account()
        self.assertEqual(401, self.request('/api/state', cookie=legacy_cookie)[0])
        self.assertEqual(401, self.request('/api/login', {'token': self.app.access_token})[0])
        self.assertEqual(401, self.request('/api/auth/setup', {'username': 'replacement-user', 'password': self.PASSWORD})[0])
        self.assertEqual(200, self.request('/api/state', cookie=cookie)[0])

    def test_password_login_and_cookie_survive_new_application(self):
        self.setup_account()
        status, body, headers = self.request('/api/login', {'username': self.USER, 'password': self.PASSWORD})
        self.assertEqual(200, status, body)
        cookie = headers['Set-Cookie'].split(';', 1)[0]
        self.stop()
        self.start()
        self.assertEqual({}, self.app.sessions)
        self.assertEqual(200, self.request('/api/state', cookie=cookie)[0])
        self.assertEqual(401, self.request('/api/state')[0])
        _, status, _ = self.request('/api/auth/status')
        self.assertTrue(status['configured'])
        self.assertFalse(status['authenticated'])
        self.assertNotIn('username', status)

    def test_logout_revokes_cookie_in_other_application(self):
        cookie, _ = self.setup_account()
        status, _, headers = self.request('/api/logout', {}, cookie=cookie)
        self.assertEqual(200, status)
        self.assertIn('Max-Age=0', headers['Set-Cookie'])
        self.stop()
        self.start()
        self.assertEqual(401, self.request('/api/state', cookie=cookie)[0])

    def test_password_change_revokes_all_old_cookies(self):
        first, _ = self.setup_account()
        status, _, headers = self.request('/api/login', {'username': self.USER, 'password': self.PASSWORD})
        self.assertEqual(200, status)
        second = headers['Set-Cookie'].split(';', 1)[0]
        new_password = 'new-http-fixture-password'
        status, body, headers = self.request('/api/auth/password', {
            'current_password': self.PASSWORD, 'new_password': new_password}, cookie=first)
        self.assertEqual(200, status, body)
        current = headers['Set-Cookie'].split(';', 1)[0]
        self.assertEqual(401, self.request('/api/state', cookie=first)[0])
        self.assertEqual(401, self.request('/api/state', cookie=second)[0])
        self.assertEqual(200, self.request('/api/state', cookie=current)[0])
        self.assertEqual(401, self.request('/api/login', {'username': self.USER, 'password': self.PASSWORD})[0])

    def test_remember_false_uses_browser_session_cookie(self):
        _, headers = self.setup_account(remember=False)
        self.assertNotIn('Max-Age', headers['Set-Cookie'])
        self.assertNotIn('Expires', headers['Set-Cookie'])

    def test_mutations_reject_cross_origin_or_missing_client_header(self):
        cookie, _ = self.setup_account()
        for headers in [{'Origin': 'https://unrelated.invalid'}, {'X-DSH-Client': None}]:
            self.assertEqual(403, self.request('/api/logout', {}, cookie=cookie, overrides=headers)[0])
            self.assertEqual(200, self.request('/api/state', cookie=cookie)[0])

    def test_private_auth_files_are_not_static_routes(self):
        cookie, _ = self.setup_account()
        for path in ['/account-auth.json', '/account-auth.lock', '/.account-auth.example',
                     '/data/account-auth.json', '/../account-auth.json']:
            status, body, _ = self.request(path, cookie=cookie)
            self.assertEqual(404, status)
            self.assertNotIn(self.PASSWORD, str(body))

    def test_invalid_auth_storage_fails_closed_without_old_token_fallback(self):
        cookie, _ = self.setup_account()
        self.app.auth.path.chmod(0o644)
        self.assertEqual(503, self.request('/api/auth/status')[0])
        self.assertEqual(401, self.request('/api/state', cookie=cookie)[0])
        self.assertEqual(503, self.request('/api/login', {'token': self.app.access_token})[0])
        self.assertEqual(503, self.request('/api/login', {'username': self.USER, 'password': self.PASSWORD})[0])


if __name__ == '__main__':
    unittest.main()
