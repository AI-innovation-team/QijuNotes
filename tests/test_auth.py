"""Private account persistence, revocation and attack boundaries with fake secrets."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from dsh_personal.auth import AuthError, AuthStorageError, AuthStore, RateLimitedError


class AuthTests(unittest.TestCase):
    USER = 'fixture-user'
    PASSWORD = 'fixture-password-never-real'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 10000.0
        self.auth = AuthStore(self.root / 'data', clock=lambda: self.now)

    def setup(self, remember=True):
        return self.auth.setup(self.USER, self.PASSWORD, remember=remember)

    def test_salted_password_and_cookie_are_not_stored_in_plaintext(self):
        session = self.setup()
        raw = self.auth.path.read_text()
        state = json.loads(raw)
        self.assertNotIn(self.PASSWORD, raw)
        self.assertNotIn(session['session_id'], raw)
        self.assertEqual('scrypt', state['credential']['algorithm'])
        self.assertEqual(0o600, self.auth.path.stat().st_mode & 0o777)
        self.assertEqual(0o600, self.auth.lock_path.stat().st_mode & 0o777)
        other = AuthStore(self.root / 'other')
        other.setup(self.USER, self.PASSWORD)
        self.assertNotEqual(state['credential']['salt'], json.loads(other.path.read_text())['credential']['salt'])
        self.assertNotEqual(state['credential']['digest'], json.loads(other.path.read_text())['credential']['digest'])

    def test_login_survives_new_store_and_a_new_python_process(self):
        self.setup()
        result = self.auth.login(self.USER, self.PASSWORD, peer='test-peer')
        fresh = AuthStore(self.auth.data, clock=lambda: self.now)
        self.assertTrue(fresh.valid_session(result['session_id']))
        # The fake session is passed through stdin, never command-line args.
        script = ('import sys,json;from dsh_personal.auth import AuthStore;'
                  'v=json.load(sys.stdin);print(AuthStore(v["data"],clock=lambda:10000).valid_session(v["sid"]))')
        completed = subprocess.run([sys.executable, '-c', script], input=json.dumps({
            'data': str(self.auth.data), 'sid': result['session_id']}), text=True, capture_output=True, check=True)
        self.assertEqual('True', completed.stdout.strip())
        self.assertEqual('', completed.stderr)

    def test_wrong_user_and_wrong_password_have_same_failure(self):
        self.setup()
        failures = []
        for username, password in [('unknown-user', self.PASSWORD), (self.USER, 'a-wrong-password')]:
            with self.assertRaises(AuthError) as caught:
                self.auth.login(username, password, peer='test-peer')
            failures.append(str(caught.exception))
        self.assertEqual(failures[0], failures[1])
        self.assertFalse(self.auth.status()['authenticated'])
        self.assertNotIn('username', self.auth.status())
        self.assertEqual(1, len(json.loads(self.auth.path.read_text())['sessions']))

    def test_expiry_and_logout_are_persistent(self):
        session = self.setup()
        self.assertEqual(30 * 86400, session['max_age'])
        self.auth.logout(session['session_id'])
        self.assertFalse(AuthStore(self.auth.data, clock=lambda: self.now).valid_session(session['session_id']))
        transient = self.auth.login(self.USER, self.PASSWORD, peer='test-peer', remember=False)
        self.assertIsNone(transient['max_age'])
        self.assertTrue(self.auth.valid_session(transient['session_id']))
        self.now += 12 * 3600
        self.assertFalse(self.auth.valid_session(transient['session_id']))
        self.assertFalse(self.auth.valid_session('not-a-session'))

    def test_change_password_revokes_all_previous_sessions(self):
        first = self.setup()
        second = self.auth.login(self.USER, self.PASSWORD, peer='test-peer')
        new_password = 'another-fixture-password'
        updated = self.auth.change_password(first['session_id'], self.PASSWORD, new_password)
        fresh = AuthStore(self.auth.data, clock=lambda: self.now)
        self.assertFalse(fresh.valid_session(first['session_id']))
        self.assertFalse(fresh.valid_session(second['session_id']))
        self.assertTrue(fresh.valid_session(updated['session_id']))
        with self.assertRaises(AuthError):
            fresh.login(self.USER, self.PASSWORD, peer='test-peer')
        self.assertTrue(fresh.login(self.USER, new_password, peer='test-peer')['session_id'])
        self.assertNotIn(new_password, fresh.path.read_text())

    def test_wrong_current_password_does_not_revoke_existing_sessions(self):
        first = self.setup()
        with self.assertRaises(AuthError):
            self.auth.change_password(first['session_id'], 'wrong-old-password', 'new-valid-password')
        self.assertTrue(self.auth.valid_session(first['session_id']))
        with self.assertRaises(AuthError):
            self.auth.change_password('invalid-session', self.PASSWORD, 'new-valid-password')

    def test_two_initializers_cannot_replace_each_other(self):
        def initialize(username):
            try:
                return AuthStore(self.auth.data, clock=lambda: self.now).setup(username, self.PASSWORD)
            except AuthError:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(initialize, ['first-account', 'second-account']))
        self.assertEqual(1, sum(result is not None for result in results))
        self.assertEqual(1, len(json.loads(self.auth.path.read_text())['sessions']))

    def test_rate_limit_survives_restart_and_expires(self):
        self.setup()
        for _ in range(10):
            with self.assertRaises(AuthError):
                self.auth.login(self.USER, '', peer='same-peer')
        fresh = AuthStore(self.auth.data, clock=lambda: self.now)
        with self.assertRaises(RateLimitedError) as caught:
            fresh.login(self.USER, self.PASSWORD, peer='same-peer')
        self.assertEqual(60, caught.exception.retry_after)
        self.now += 61
        self.assertTrue(fresh.login(self.USER, self.PASSWORD, peer='same-peer')['session_id'])

    def test_global_rate_limit_bounds_distinct_peer_attempts(self):
        self.setup()
        for peer in range(30):
            with self.assertRaises(AuthError):
                self.auth.login(self.USER, '', peer=str(peer))
        with self.assertRaises(RateLimitedError):
            self.auth.login(self.USER, self.PASSWORD, peer='new-peer')
        self.assertEqual(30, len(json.loads(self.auth.path.read_text())['attempts']))

    def test_invalid_setup_cannot_create_account(self):
        for username, password in [('x', self.PASSWORD), ('has space', self.PASSWORD),
                                   (self.USER, 'short'), (self.USER, 'x' * 257),
                                   ('a' * 65, self.PASSWORD), ('x\u202ey', self.PASSWORD),
                                   ('xx\ud800', self.PASSWORD), (self.USER, 'invalid-unicode-\ud800')]:
            with self.assertRaises(AuthError):
                self.auth.setup(username, password)
        self.assertFalse(self.auth.configured())
        self.assertFalse(self.auth.path.exists())

    def test_symlink_permissions_and_corrupt_file_fail_closed(self):
        self.setup()
        original = self.auth.path.read_bytes()
        outside = self.root / 'outside.json'
        outside.write_bytes(original)
        outside.chmod(0o600)
        self.auth.path.unlink()
        self.auth.path.symlink_to(outside)
        with self.assertRaises(AuthStorageError):
            self.auth.configured()
        with self.assertRaises(AuthStorageError):
            self.auth.reset_account(self.USER, self.PASSWORD)
        self.assertEqual(original, outside.read_bytes())
        self.auth.path.unlink()
        self.auth.path.write_bytes(original)
        self.auth.path.chmod(0o644)
        with self.assertRaises(AuthStorageError):
            self.auth.valid_session('a' * 43)
        self.auth.path.chmod(0o600)
        self.auth.path.write_text('{}')
        with self.assertRaises(AuthStorageError):
            self.auth.configured()

    def test_local_reset_revokes_sessions_and_changes_username(self):
        session = self.setup()
        result = self.auth.reset_account('new-account', 'new-fixture-password')
        self.assertEqual(1, result['sessions_revoked'])
        self.assertFalse(self.auth.valid_session(session['session_id']))
        with self.assertRaises(AuthError):
            self.auth.login(self.USER, self.PASSWORD, peer='test-peer')
        self.assertTrue(self.auth.login('new-account', 'new-fixture-password', peer='test-peer')['session_id'])

    def test_cli_refuses_noninteractive_password_input(self):
        command = [sys.executable, 'scripts/account.py', 'setup', '--data', str(self.root / 'cli-data')]
        result = subprocess.run(command, input=self.PASSWORD, capture_output=True, text=True)
        self.assertEqual(2, result.returncode)
        self.assertNotIn(self.PASSWORD, result.stdout + result.stderr)
        self.assertFalse((self.root / 'cli-data' / AuthStore.FILE_NAME).exists())


if __name__ == '__main__':
    unittest.main()
