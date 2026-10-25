"""Pinned DSH 0.2.1-rc.2 Web RPC adapter. No direct writes to DSH storage."""
from __future__ import annotations

import fcntl
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import threading
import urllib.error
import urllib.request
import uuid


class DSHError(RuntimeError):
    def __init__(self, message: str, code: str = 'dsh/unavailable'):
        super().__init__(message)
        self.code = code


class DSHBridge:
    def __init__(self, base_url='http://127.0.0.1:18781', dsh_home=None, *, auth_file=None, timeout=20):
        self.base_url = base_url.rstrip('/')
        self.dsh_home = Path(dsh_home or os.environ.get('DSH_HOME', Path.home() / '.local/share/dsh-personal/dsh'))
        self.auth_file = Path(auth_file or self.dsh_home / 'bridge-auth.json')
        self.timeout = timeout
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self._lock = threading.RLock()
        self._authenticated = False

    def _authenticate(self):
        try:
            auth = json.loads(self.auth_file.read_text())
            # Launch URL contains a one-process bearer secret, never returned to callers.
            url = auth['launch_url']
            if not url.startswith(self.base_url + '/?token='):
                raise DSHError('DSH认证地址与配置端口不匹配', 'dsh/auth-mismatch')
            with self.opener.open(url, timeout=self.timeout) as response:
                response.read()
            self._authenticated = True
        except (OSError, ValueError, KeyError, urllib.error.URLError) as exc:
            raise DSHError('DSH宿主尚未就绪或本地认证不可用', 'dsh/auth-unavailable') from exc

    def rpc(self, method, args):
        """Invoke actual Connection client-request envelope, preserving stable domain errors."""
        with self._lock:
            if not self._authenticated:
                self._authenticate()
            body = json.dumps({'type': 'client-request', 'rpcId': str(uuid.uuid4()),
                               'method': method, 'payload': {'args': args}}, ensure_ascii=False).encode()
            req = urllib.request.Request(self.base_url + '/api/' + method, body,
                                         {'Content-Type': 'application/json'}, method='POST')
            try:
                with self.opener.open(req, timeout=self.timeout) as response:
                    result = json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    self._authenticated = False
                    raise DSHError('DSH认证过期，请稍后重试', 'dsh/auth-expired') from exc
                raise DSHError(f'DSH HTTP请求失败 ({exc.code})', 'dsh/http-error') from exc
            except (OSError, ValueError, urllib.error.URLError) as exc:
                raise DSHError('DSH响应未确认；应使用原提交ID核对后重试') from exc
            envelope = result.get('result', {})
            if envelope.get('ok') is True:
                return envelope.get('value')
            error = envelope.get('error', {})
            raise DSHError(error.get('message', 'DSH未返回合法RPC回执'),
                           error.get('code', 'dsh/invalid-envelope'))

    @staticmethod
    def stable_session_id(activity_id):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, 'dsh-personal/activity/' + str(activity_id)))

    def ensure_session(self, activity_id, cwd=None):
        sid = self.stable_session_id(activity_id)
        lock_dir = self.dsh_home / 'bridge-locks'
        lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = lock_dir / (hashlib.sha256(str(activity_id).encode()).hexdigest() + '.lock')
        with lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self.projections(sid)
            if state is not None:
                return sid
            result = self.rpc('session/create', {'request': {'sessionId': sid,
                'cwd': str(Path(cwd or self.dsh_home.parent / 'workspace').resolve()), 'agentPreset': 'personal'}})
            if result.get('sessionId') != sid:
                # A concurrent non-bridge writer or mismatched storage policy must not silently fork.
                raise DSHError('DSH未返回请求的稳定会话身份，需核对', 'dsh/session-conflict')
            return sid

    def submit(self, session_id, submission_id, text, *, kind="background", model=None):
        with self._lock:
            if not str(text).strip():
                raise ValueError('消息不能为空')
            existing = self.reconcile(session_id, submission_id)
            if existing['accepted']:
                return {'accepted': True, 'native_request_id': submission_id, 'reconciled': True}
            model_id = model or {'background': 'configured-model', 'foreground': 'foreground-model',
                                 'delegated': 'delegated-model'}.get(kind)
            if model_id not in ('configured-model', 'foreground-model', 'delegated-model'):
                raise ValueError('未知用量分类')
            self.rpc('session/selectModel', {'request': {'sessionId': session_id,
                     'provider': 'dsh-personal', 'model': model_id}})
            result = self.rpc('session/prompt', {'request': {'sessionId': session_id,
                'requestId': submission_id, 'mode': 'queue', 'clientTimeZone': 'Asia/Shanghai',
                'content': [{'type': 'text', 'text': text}]}})
            if result.get('accepted') is not True:
                raise DSHError('DSH没有返回入站回执', 'dsh/receipt-missing')
            return {'accepted': True, 'native_request_id': submission_id}

    def projections(self, session_id):
        return self.rpc('session/projections', {'request': {'sessionId': session_id}})

    def events(self, session_id, after_seq=None):
        state = self.projections(session_id)
        if state is None:
            return []
        if state.get('kind') != 'sequenced':
            raise DSHError('DSH会话需要迁移，禁止猜测事件游标', 'dsh/migration-required')
        cursor = state['asOfSeq']
        if cursor < 0 or (after_seq is not None and cursor <= after_seq):
            return []
        events, before = [], None
        while True:
            request = {'address': {'kind': 'session', 'sessionId': session_id},
                       'throughSeq': cursor, 'maxMessages': 500}
            if before is not None:
                request['beforeSeq'] = before
            page = self.rpc('session/page', {'request': request})
            current = [r['event'] for r in page['records'] if r.get('type') == 'event']
            events[0:0] = current
            if not page['hasMore'] or (after_seq is not None and current and current[0]['seq'] <= after_seq):
                break
            if not current or (before is not None and current[0]['seq'] >= before):
                raise DSHError('DSH分页游标未前进', 'dsh/pagination-invalid')
            before = current[0]['seq']
        return [event for event in events if after_seq is None or event['seq'] > after_seq]

    def reconcile(self, session_id, submission_id):
        for event in self.events(session_id):
            data = event.get('data', {})
            if event['type'] == 'user/message' and data.get('source', {}).get('rpcId') == submission_id:
                return {'accepted': True, 'state': 'transcript', 'seq': event['seq'], 'message_id': data.get('id')}
            # Durable inbox accepted messages survive provider failures before transcript admission.
            if event['type'] == 'agent/inbox/spliced':
                for message in data.get('inserted', []):
                    if message.get('source', {}).get('rpcId') == submission_id:
                        return {'accepted': True, 'state': 'inbox', 'seq': event['seq'], 'message_id': message.get('id')}
        return {'accepted': False, 'state': 'not-observed'}

    def resume_processing(self, session_id, retry_id, source_request_id):
        """Explicit producer-attributed control event; original user messages remain unchanged."""
        with self._lock:
            if not self._authenticated:
                self._authenticate()
            data = json.dumps({'sessionId': session_id, 'retryId': retry_id,
                               'sourceRequestId': source_request_id}).encode()
            req = urllib.request.Request(self.base_url + '/api/personal.retry', data,
                                         {'Content-Type': 'application/json'}, method='POST')
            try:
                with self.opener.open(req, timeout=self.timeout) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                try: message = json.loads(exc.read()).get('error', '重试请求被拒绝')
                except ValueError: message = '重试请求被拒绝'
                raise DSHError(message, 'dsh/retry-rejected') from exc

    def cancel_processing(self, session_id):
        return self.rpc('session/cancel', {'request': {'sessionId': session_id}})

    def pending_questions(self, session_id):
        state = self.projections(session_id)
        return [] if state is None else state.get('values', {}).get('userQuestions', {}).get('active', [])

    def answer_pending(self, session_id, call_id, answers):
        """Answer a specifically selected native question batch, without a second ordinary prompt.

        answers is [{id: question_id, selected: [...], custom: optional_text}, ...].
        Every question in the selected batch must be explicitly answered or skipped by the user.
        """
        if not isinstance(answers, list) or not answers:
            raise ValueError('请提供明确的问题答复批次')
        with self._lock:
            for event in self.events(session_id):
                messages = ([event['data']] if event['type'] == 'user/message' else
                            event['data'].get('inserted', []) if event['type'] == 'agent/inbox/spliced' else [])
                for message in messages:
                    source = message.get('source', {})
                    if source.get('kind') != 'user-question-reply' or source.get('callId') != call_id:
                        continue
                    for part in message.get('content', []):
                        if part.get('type') != 'text':
                            continue
                        try: previous = json.loads(part['text']).get('answers')
                        except (ValueError, KeyError): continue
                        if previous != answers:
                            raise DSHError('该问题已有不同答复；请在原会话继续补充或更正', 'dsh/answer-conflict')
                    return {'accepted': True, 'call_id': call_id, 'reconciled': True}
            active = next((q for q in self.pending_questions(session_id) if q['callId'] == call_id), None)
            if active is None:
                raise DSHError('未找到仍可回答的问题', 'dsh/question-not-found')
            if {q['id'] for q in active['questions']} != {a.get('id') for a in answers}:
                raise ValueError('请明确处理该批次中的每个问题，不自动跳过未答项')
            if active['state'] != 'continued':
                raise DSHError('问题仍在当前计算中，稍后可回答', 'dsh/question-not-continued')
            self.rpc('session/selectModel', {'request': {'sessionId': session_id,
                     'provider': 'dsh-personal', 'model': 'foreground-model'}})
            accepted = self.rpc('userQuestions/answer', {'agentId': session_id,
                                'callId': call_id, 'answer': {'answers': answers}})
            if accepted is not True:
                raise DSHError('问题答复未取得原生回执，请核对后重试', 'dsh/answer-unconfirmed')
            return {'accepted': True, 'call_id': call_id}
