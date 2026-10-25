"""Single-user password authentication and revocable, restart-safe browser sessions.

The web boundary owns same-origin and loopback checks for first-time setup. This
module never prints credentials, stores clear-text passwords or bearer cookies,
or mutates the application's conversation database.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading
import time
import unicodedata


class AuthError(ValueError):
    """Safe, user-facing authentication failure."""


class RateLimitedError(AuthError):
    def __init__(self, retry_after=60):
        self.retry_after = max(1, int(retry_after))
        super().__init__('尝试过多，请稍后再试。')


class AuthStorageError(RuntimeError):
    """Fail closed if a private authentication file is invalid or inaccessible."""


class AuthStore:
    FILE_NAME = 'account-auth.json'
    LOCK_NAME = 'account-auth.lock'
    REMEMBER_SECONDS = 30 * 86400
    SESSION_SECONDS = 12 * 3600
    MAX_SESSIONS = 32
    MAX_FILE_BYTES = 131072
    SCRYPT_N = 32768
    SCRYPT_R = 8
    SCRYPT_P = 1

    def __init__(self, data, clock=None):
        self.data = Path(data).expanduser().resolve()
        self.path = self.data / self.FILE_NAME
        self.lock_path = self.data / self.LOCK_NAME
        self.clock = clock or time.time
        self._thread_lock = threading.RLock()

    @staticmethod
    def validate_username(username):
        if not isinstance(username, str):
            raise AuthError('请输入账号。')
        username = unicodedata.normalize('NFC', username.strip())
        try:
            encoded = username.encode()
        except UnicodeEncodeError:
            raise AuthError('账号包含无效字符。') from None
        if (not 2 <= len(username) <= 64 or len(encoded) > 256
                or any(c.isspace() or unicodedata.category(c).startswith('C') for c in username)):
            raise AuthError('账号需为2至64个字符，不含空格或控制字符。')
        return username

    @staticmethod
    def validate_password(password):
        if not isinstance(password, str):
            raise AuthError('请输入密码。')
        try:
            encoded = password.encode()
        except UnicodeEncodeError:
            raise AuthError('密码包含无效字符。') from None
        if (not 10 <= len(password) <= 256 or len(encoded) > 1024 or '\x00' in password):
            raise AuthError('密码需为10至256个字符，可以使用便于记忆的长词组。')
        return password

    @classmethod
    def _derive(cls, password, salt):
        return hashlib.scrypt(password.encode(), salt=salt, n=cls.SCRYPT_N,
                              r=cls.SCRYPT_R, p=cls.SCRYPT_P, dklen=32,
                              maxmem=64 * 1024 * 1024)

    @classmethod
    def _credential(cls, username, password):
        salt = secrets.token_bytes(16)
        return {'username': username, 'algorithm': 'scrypt', 'n': cls.SCRYPT_N,
                'r': cls.SCRYPT_R, 'p': cls.SCRYPT_P, 'salt': salt.hex(),
                'digest': cls._derive(password, salt).hex()}

    @staticmethod
    def _session_key(sid):
        if not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', sid):
            return None
        return hashlib.sha256(sid.encode()).hexdigest()

    @contextmanager
    def _locked(self, exclusive=False):
        # The data directory is already project-owned; standalone CLI setup may
        # create it, but never weakens an existing directory's permissions.
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._thread_lock:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    os.close(fd)
                    raise AuthStorageError('账号文件权限异常，请在本机检查。')
                with os.fdopen(fd, 'a+b') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                    try:
                        yield
                    finally:
                        fcntl.flock(lock, fcntl.LOCK_UN)
            except OSError as exc:
                raise AuthStorageError('账号存储暂不可用，请在本机检查。') from exc

    def _read(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise AuthStorageError('账号存储暂不可用，请在本机检查。') from exc
        try:
            with os.fdopen(fd, 'rb') as handle:
                info = os.fstat(handle.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o077 or info.st_size > self.MAX_FILE_BYTES):
                    raise ValueError('invalid private file')
                raw = handle.read(self.MAX_FILE_BYTES + 1)
            if len(raw) > self.MAX_FILE_BYTES:
                raise ValueError('oversized private file')
            state = json.loads(raw)
            credential = state['credential']
            if (state['version'] != 1 or not isinstance(credential, dict)
                    or credential['algorithm'] != 'scrypt'
                    or (credential['n'], credential['r'], credential['p']) != (self.SCRYPT_N, self.SCRYPT_R, self.SCRYPT_P)
                    or len(bytes.fromhex(credential['salt'])) != 16
                    or len(bytes.fromhex(credential['digest'])) != 32
                    or self.validate_username(credential['username']) != credential['username']
                    or not isinstance(state['sessions'], dict) or len(state['sessions']) > self.MAX_SESSIONS
                    or not isinstance(state['attempts'], list) or len(state['attempts']) > 30):
                raise ValueError('invalid private state')
            for key, session in state['sessions'].items():
                if (not re.fullmatch(r'[0-9a-f]{64}', key)
                        or not isinstance(session, dict)
                        or not all(isinstance(session[k], (int, float)) and math.isfinite(session[k])
                                   for k in ['created_at', 'expires_at'])):
                    raise ValueError('invalid session')
            for attempt in state['attempts']:
                if (not isinstance(attempt, dict) or not re.fullmatch(r'[0-9a-f]{64}', attempt['peer'])
                        or not isinstance(attempt['at'], (int, float)) or not math.isfinite(attempt['at'])):
                    raise ValueError('invalid attempt')
            return state
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError) as exc:
            raise AuthStorageError('账号存储无效；已停止认证，请在本机恢复或重设。') from exc

    def _write(self, state):
        raw = (json.dumps(state, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
        if len(raw) > self.MAX_FILE_BYTES:
            raise AuthStorageError('账号存储超出安全大小。')
        fd, temporary = tempfile.mkstemp(prefix='.account-auth.', dir=self.data)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.data, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def configured(self):
        with self._locked():
            return self._read() is not None

    def status(self, sid=None):
        with self._locked():
            state = self._read()
            authenticated = bool(state and self._valid(state, sid))
            result = {'configured': state is not None, 'authenticated': authenticated}
            if authenticated:
                result['username'] = state['credential']['username']
            return result

    def _valid(self, state, sid):
        key = self._session_key(sid)
        session = state['sessions'].get(key) if key else None
        return bool(session and session['created_at'] <= self.clock() < session['expires_at'])

    def valid_session(self, sid):
        with self._locked():
            state = self._read()
            return bool(state and self._valid(state, sid))

    def _new_session(self, state, remember):
        now = self.clock()
        state['sessions'] = {k: v for k, v in state['sessions'].items() if v['expires_at'] > now}
        while len(state['sessions']) >= self.MAX_SESSIONS:
            oldest = min(state['sessions'], key=lambda key: state['sessions'][key]['created_at'])
            del state['sessions'][oldest]
        sid = secrets.token_urlsafe(32)
        ttl = self.REMEMBER_SECONDS if remember else self.SESSION_SECONDS
        state['sessions'][self._session_key(sid)] = {'created_at': now, 'expires_at': now + ttl}
        return {'session_id': sid, 'max_age': ttl if remember else None, 'expires_at': now + ttl}

    def setup(self, username, password, remember=True):
        username = self.validate_username(username)
        password = self.validate_password(password)
        with self._locked(exclusive=True):
            if self._read() is not None:
                raise AuthError('账号已设置，请使用账号密码登录。')
            state = {'version': 1, 'credential': self._credential(username, password),
                     'sessions': {}, 'attempts': []}
            session = self._new_session(state, bool(remember))
            self._write(state)
            return session

    def _rate_limit(self, state, peer):
        now = self.clock()
        peer = hashlib.sha256(str(peer).encode()).hexdigest()
        attempts = [a for a in state['attempts'] if a['at'] > now - 60]
        same_peer = [a for a in attempts if a['peer'] == peer]
        if len(same_peer) >= 10 or len(attempts) >= 30:
            limiting = same_peer if len(same_peer) >= 10 else attempts
            raise RateLimitedError(math.ceil(min(a['at'] for a in limiting) + 60 - now))
        state['attempts'] = attempts + [{'at': now, 'peer': peer}]

    def _matches(self, state, username, password):
        credential = state['credential']
        digest = self._derive(password, bytes.fromhex(credential['salt']))
        return (hmac.compare_digest(digest, bytes.fromhex(credential['digest']))
                and hmac.compare_digest(username.encode(), credential['username'].encode()))

    def login(self, username, password, peer, remember=True):
        with self._locked(exclusive=True):
            state = self._read()
            if state is None:
                raise AuthError('请先在这台电脑上设置账号。')
            self._rate_limit(state, peer)
            try:
                username = self.validate_username(username)
                password = self.validate_password(password)
                matched = self._matches(state, username, password)
            except AuthError:
                matched = False
            if not matched:
                self._write(state)
                raise AuthError('账号或密码不正确。')
            session = self._new_session(state, bool(remember))
            self._write(state)
            return session

    def logout(self, sid):
        with self._locked(exclusive=True):
            state = self._read()
            if state is None:
                return
            key = self._session_key(sid)
            if key in state['sessions']:
                del state['sessions'][key]
                self._write(state)

    def change_password(self, sid, current_password, new_password, remember=True, peer='password-change'):
        new_password = self.validate_password(new_password)
        with self._locked(exclusive=True):
            state = self._read()
            if not state or not self._valid(state, sid):
                raise AuthError('请重新登录后修改密码。')
            self._rate_limit(state, peer)
            try:
                current_password = self.validate_password(current_password)
                matched = self._matches(state, state['credential']['username'], current_password)
            except AuthError:
                matched = False
            if not matched:
                self._write(state)
                raise AuthError('当前密码不正确。')
            state['credential'] = self._credential(state['credential']['username'], new_password)
            state['sessions'] = {}
            session = self._new_session(state, bool(remember))
            self._write(state)
            return session

    def reset_account(self, username, password):
        """Local administrative CLI only: reset password and revoke every cookie."""
        username = self.validate_username(username)
        password = self.validate_password(password)
        with self._locked(exclusive=True):
            # Even local recovery refuses symlink/unreadable/corrupt state rather
            # than silently replacing something it cannot safely inspect.
            prior = self._read()
            state = {'version': 1, 'credential': self._credential(username, password),
                     'sessions': {}, 'attempts': []}
            self._write(state)
            return {'username': username, 'sessions_revoked': len(prior['sessions']) if prior else 0}
