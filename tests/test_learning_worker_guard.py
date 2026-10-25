"""Publication boundary tests: isolated database and native/model doubles only."""
import datetime as dt
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch

from dsh_personal import learning_guard as guard
from dsh_personal.core import TZ, dumps
from dsh_personal.learning import prepare_learning
from dsh_personal.server import Application
from dsh_personal.worker import Worker


class Bridge:
    def __init__(self):
        self.event_list = []
        self.questions = []
        self.submissions = []

    def ensure_session(self, aid):
        return 'isolated-' + aid

    def events(self, sid):
        return self.event_list

    def pending_questions(self, sid):
        return self.questions

    def submit(self, *args, **kwargs):
        self.submissions.append((args, kwargs))


class LearningWorkerGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = dt.datetime(2026, 10, 24, 12, tzinfo=TZ)
        self.app = Application(self.tmp.name, lambda: self.now)
        self.store = self.app.store
        self.bridge = Bridge()
        self.worker = Worker(self.store, self.bridge, self.app.records, lambda: {})
        self.aid = self.store.ensure_activity('A1')['id']
        self.sid = self.worker.ensure_session(self.aid)
        self.manifest = prepare_learning(self.store, self.aid)['manifest']
        with self.store.db(True) as db:
            db.execute("UPDATE jobs SET status='done'")
        self.initial_messages = self.store.activity(self.aid)['messages']

    def tearDown(self):
        self.tmp.cleanup()

    def activity(self):
        return self.store.activity(self.aid)['activity']

    def candidates(self):
        with self.store.db() as db:
            return [guard.snapshot(self.store, row[0]) for row in db.execute('SELECT id FROM learning_reviews ORDER BY rowid')]

    def public(self):
        return self.store.activity(self.aid)['messages']

    def native_turn(self, text='PRIVATE_DRAFT', *, start=10, turn=1, request=None, reason='completed'):
        return [
            {'seq': start, 'type': 'turn/start', 'data': {'turn': turn}},
            {'seq': start + 1, 'type': 'user/message', 'data': {'source': {'rpcId': request or 'learning-' + self.aid}}},
            {'seq': start + 2, 'type': 'assistant/message', 'data': {'turn': turn, 'message': {'content': [{'type': 'text', 'text': text}]}}},
            {'seq': start + 3, 'type': 'turn/end', 'data': {'turn': turn, 'reason': {'kind': reason}}},
        ]

    def followup(self, draft='PRIVATE_DRAFT 当时人数少。'):
        with self.store.db(True) as db:
            db.execute("UPDATE activities SET ready_at=?,status='awaiting_reply' WHERE id=?", (self.store.stamp(), self.aid))
        receipt = self.store.submit(self.aid, 'isolated-followup-submission', '请解释这份材料。')
        self.bridge.event_list = self.native_turn(draft, request='isolated-followup-submission')
        self.worker.sync_all()
        return self.candidates()[0], receipt['receipt']

    def lesson_text(self, analysis):
        sources=self.manifest['cards']+[self.manifest['world_fun_fact']]
        return ('### 史实与文本依据\n\n'+self.manifest['cards'][0]['verified_facts'][0]+
                '\n\n'+ '\n\n'.join('来源：[出处]('+s['source_url']+')' for s in sources)+
                '\n\n### 理论分析\n\n'+analysis+
                '\n\n### 现实类比（假设）\n\n假设两组采用不同规则，可以比较规则的差异。')

    def initial_analysis(self, analysis):
        self.bridge.event_list=self.native_turn(self.lesson_text(analysis))
        self.worker.sync_all()
        return self.candidates()[0]

    @staticmethod
    def audit(reference, passed=True):
        payload = reference['payload']
        paragraphs = []
        units = {unit['id']: unit for unit in payload['packet']['units']}
        for paragraph in payload['paragraphs']:
            spans = [span for span in reference['claim_spans'] if span['paragraph_id'] == paragraph['id']]
            claims = []
            if paragraph['role'] == 'body' and paragraph['section'] == 'evidence':
                for span in spans:
                    source = next((source for source in reference['source_spans']
                                   if units[source['unit_id']]['kind'] != 'self_report'
                                   and span['text'] in source['text']), None)
                    if source:
                        claims.append({'span_id': span['id'], 'status': 'supported',
                                       'evidence': [{'source_span_id': source['id']}]})
            paragraphs.append({'id': paragraph['id'], 'kind': paragraph['section'] or 'interaction',
                               'status': 'pass' if passed else 'needs_revision', 'claims': claims,
                               'issues': [] if passed else [{'code': 'unsupported_fact', 'span_id': spans[0]['id'],
                                                            'reason': '测试反例：缺少支持这项断言的来源。'}]})
        return {'packet_sha256': payload['packet_sha256'], 'draft_sha256': payload['draft_sha256'],
                'reference_sha256': reference['reference_sha256'], 'paragraphs': paragraphs}

    def test_completed_turn_is_private_and_queued_once(self):
        self.bridge.event_list = self.native_turn()
        self.worker.sync_all(); self.worker.sync_all()
        self.assertEqual(self.public(), self.initial_messages)
        rows = self.candidates()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['draft'], 'PRIVATE_DRAFT')
        self.assertEqual(rows[0]['mode'], 'initial')
        self.assertEqual(rows[0]['status'], 'pending')
        self.assertIsNone(self.activity()['ready_at'])
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE kind='learning_review'").fetchone()[0], 1)

    def test_partial_or_failed_turn_never_becomes_public_or_candidate(self):
        for events in (self.native_turn()[:-1], self.native_turn(reason='error')):
            self.bridge.event_list = events
            self.worker.sync_all()
            self.assertEqual(self.public(), self.initial_messages)
            self.assertEqual(self.candidates(), [])

    def test_unstarted_or_mismatched_turn_cannot_become_candidate(self):
        variants = [self.native_turn()[1:]]
        wrong_end = self.native_turn(); wrong_end[-1]['data']['turn'] = 2; variants.append(wrong_end)
        wrong_assistant = self.native_turn(); wrong_assistant[-2]['data']['turn'] = 2; variants.append(wrong_assistant)
        for events in variants:
            with self.subTest(events=events):
                self.bridge.event_list = events
                self.worker.sync_all()
                self.assertEqual(self.candidates(), [])

    def test_resumed_turn_keeps_known_request_without_new_user_message(self):
        failed = self.native_turn(reason='error')
        resumed = self.native_turn(start=20, turn=2)
        self.bridge.event_list = failed + [resumed[0], resumed[2], resumed[3]]
        self.worker.sync_all()
        rows = self.candidates()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['end_seq'], 23)
        self.assertEqual(rows[0]['turn']['request_id'], 'learning-' + self.aid)

    def test_new_tools_and_question_batches_are_never_public(self):
        self.bridge.questions = [{'callId': 'new-unreviewed', 'questions': [{'id': 'q', 'question': 'PRIVATE_QUESTION'}]}]
        self.bridge.event_list = self.native_turn()[:-1] + [
            {'seq': 14, 'type': 'tool/call', 'data': {'name': 'ask_user_question', 'arguments': 'PRIVATE_TOOL'}},
            {'seq': 15, 'type': 'tool/result', 'data': {'content': [{'type': 'text', 'text': 'PRIVATE_RESULT'}]}},
        ]
        self.worker.sync_all()
        self.assertEqual(self.public(), self.initial_messages)
        self.assertEqual(self.store.activity(self.aid)['questions'], [])
        self.assertNotIn('PRIVATE', json.dumps(self.store.activity(self.aid)))

    def test_tool_only_terminal_turn_has_fixed_status_and_new_turn_clears_it(self):
        events = self.native_turn()
        self.bridge.event_list = events[:2] + [
            {'seq': 12, 'type': 'tool/call', 'data': {'name': 'ask_user_question', 'arguments': 'PRIVATE_TOOL'}},
            {**events[-1], 'data': {'turn': 1, 'reason': {'kind': 'blocked'}}},
        ]
        self.worker.sync_all()
        view = self.store.activity(self.aid)
        self.assertIsNotNone(view['processing'])
        self.assertEqual(view['processing']['status'], 'failed')
        self.assertNotIn('PRIVATE', json.dumps(view))
        self.bridge.event_list.append({'seq': 20, 'type': 'turn/start', 'data': {'turn': 2}})
        self.worker.sync_all()
        self.assertIsNone(self.store.activity(self.aid)['processing'])

    def test_existing_pending_question_is_retained_without_adopting_new_batch(self):
        known = {'callId': 'known-old', 'questions': [{'id': 'q', 'question': '旧课程问题'}]}
        with self.store.db(True) as db:
            db.execute('INSERT INTO native_questions VALUES(?,?,?)', (self.aid, json.dumps([known]), self.store.stamp()))
        self.bridge.questions = [known, {'callId': 'new-unreviewed', 'questions': [{'id': 'new', 'question': 'PRIVATE_QUESTION'}]}]
        self.worker.sync_all()
        self.assertEqual(self.store.activity(self.aid)['questions'], [known])
        self.bridge.questions = []
        self.worker.sync_all()
        self.assertEqual(self.store.activity(self.aid)['questions'], [])

    def test_legacy_public_message_is_not_adopted_or_rewritten(self):
        mid = 'dsh:' + self.sid + ':12'
        with self.store.db(True) as db:
            db.execute("INSERT INTO messages VALUES(?,?,NULL,'assistant',?,?,'native','12',NULL,0)",
                       (mid, self.aid, '历史原有公开课程', self.store.stamp()))
        before = self.public()
        self.bridge.event_list = self.native_turn('不同的原生重放内容')
        self.worker.sync_all()
        self.assertEqual(self.public(), before)
        self.assertEqual(self.candidates(), [])

    def test_one_repair_is_reaudited_then_only_repaired_text_is_published(self):
        candidate, mid = self.followup()
        calls = []
        def model(system, payload, *, repair=False):
            calls.append(repair)
            if repair:
                return '收到，可以继续追问。'
            return self.audit(payload, passed=len(calls) > 1)
        self.worker.learning_model = model
        result = self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertTrue(result['published'])
        self.assertEqual(calls, [False, True, False])
        public = next(m for m in self.public() if m['id'] == candidate['message_id'])
        self.assertEqual(public['body'], '收到，可以继续追问。')
        self.assertEqual(public['processing_state'], 'learning_reviewed')
        self.assertNotIn('PRIVATE_DRAFT', json.dumps(self.public()))
        self.assertTrue(any(m['id'] == mid and m['body'] == '请解释这份材料。' for m in self.public()))
        self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertEqual(calls, [False, True, False])

    def test_failed_repair_is_never_published_or_repaired_a_second_time(self):
        candidate, _ = self.followup()
        calls = []
        def model(system, payload, *, repair=False):
            calls.append(repair)
            return '依旧没有支持来源。' if repair else self.audit(payload, passed=False)
        self.worker.learning_model = model
        self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertEqual(calls, [False, True, False])
        self.assertEqual(guard.snapshot(self.store, candidate['id'])['status'], 'rejected')
        self.assertFalse(any(m['id'] == candidate['message_id'] for m in self.public()))
        self.assertTrue(guard.retry(self.store, self.aid))
        self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertEqual(calls, [False, True, False, False])
        self.assertEqual(calls.count(True), 1)
        self.assertEqual(guard.snapshot(self.store, candidate['id'])['status'], 'rejected')

    def test_unknown_repair_outcome_after_network_failure_is_not_reissued(self):
        candidate, _ = self.followup()
        calls = []
        def model(system, payload, *, repair=False):
            calls.append(repair)
            if repair:
                raise TimeoutError('PRIVATE_PROVIDER_ERROR')
            return self.audit(payload, passed=False)
        self.worker.learning_model = model
        with self.assertRaises(TimeoutError):
            self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertEqual(calls, [False, True])
        self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertEqual(calls, [False, True])
        self.assertEqual(guard.snapshot(self.store, candidate['id'])['status'], 'rejected')
        self.assertFalse(any(m['id'] == candidate['message_id'] for m in self.public()))

    def test_crash_after_repair_receipt_only_retries_missing_audit(self):
        candidate, _ = self.followup()
        calls = []
        def model(system, payload, *, repair=False):
            calls.append(repair)
            if repair:
                return '收到，可以继续追问。'
            if len(calls) == 3:
                raise TimeoutError('审稿暂时无结果')
            return self.audit(payload, passed=len(calls) > 1)
        self.worker.learning_model = model
        with self.assertRaises(TimeoutError):
            self.worker.review_learning(self.aid, candidate['job_version'])
        self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertEqual(calls, [False, True, False, False])
        self.assertEqual(guard.snapshot(self.store, candidate['id'])['status'], 'approved')

    def test_protocol_error_retries_same_payload_once_without_rewriting_lesson(self):
        from dsh_personal.learning_content import COMBINED_REFERENCE_SYSTEM
        candidate, _ = self.followup('收到，可以继续追问。')
        calls = []
        def model(system, payload, *, repair=False):
            calls.append((system, json.loads(json.dumps(payload)), repair))
            self.assertFalse(repair)
            result = self.audit(payload)
            if len(calls) == 1:
                result['paragraphs'] = []
            return result
        self.worker.learning_model = model
        result = self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertTrue(result['published'])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], COMBINED_REFERENCE_SYSTEM)
        self.assertIn('本次追加严格协议纠错', calls[1][0])
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertEqual(next(m['body'] for m in self.public() if m['id'] == candidate['message_id']), candidate['draft'])
        records = [entry['result'] for entry in guard.snapshot(self.store, candidate['id'])['audit']]
        started = [row for row in records if row['stage'] == 'audit_protocol_retry_started']
        received = [row for row in records if row['stage'] == 'audit_protocol_retry_received']
        self.assertEqual(len(started), 1)
        self.assertEqual(len(received), 1)
        self.assertEqual(started[0]['scope'], received[0]['scope'])
        payload = calls[0][1]
        self.assertEqual(started[0]['scope'], {
            'audit_stage': 'audit', 'draft_sha256': payload['payload']['draft_sha256'],
            'full_draft_sha256': payload['payload']['draft_sha256'],
                 'policy_sha256': hashlib.sha256(COMBINED_REFERENCE_SYSTEM.encode()).hexdigest(),
            'packet_sha256': hashlib.sha256(dumps(payload).encode()).hexdigest(),
        })
        self.assertFalse(any(row['stage'] == 'repair_started' for row in records))

    def test_semantic_failure_never_enters_protocol_retry(self):
        unsupported = '当时人数少。'
        candidate, _ = self.followup('### 理论分析\n\n' + unsupported)
        calls = []
        def model(system, payload, *, repair=False):
            self.assertNotIn('本次追加严格协议纠错', system)
            calls.append(repair)
            if repair:
                repair_data = json.loads(payload.rsplit('\n', 1)[-1])
                claims = [claim for paragraph in repair_data['audit']['review']['paragraphs']
                          for claim in paragraph['claims']]
                self.assertEqual(claims[0]['text'], unsupported)
                self.assertEqual(claims[0]['status'], 'unsupported')
                self.assertEqual(claims[0]['evidence'], [])
                self.assertNotIn('span_id', claims[0])
                return '收到，可以继续追问。'
            raw = self.audit(payload, passed=len(calls) > 1)
            if len(calls) == 1:
                span = next(span for span in payload['claim_spans'] if span['text'] == unsupported)
                paragraph = next(p for p in raw['paragraphs'] if p['id'] == span['paragraph_id'])
                paragraph['claims'] = [{'span_id': span['id'], 'status': 'unsupported', 'evidence': []}]
            return raw
        self.worker.learning_model = model
        self.assertTrue(self.worker.review_learning(self.aid, candidate['job_version'])['published'])
        self.assertEqual(calls, [False, True, False])
        records = [entry['result'] for entry in guard.snapshot(self.store, candidate['id'])['audit']]
        self.assertFalse(any(row['stage'].startswith('audit_protocol_retry_') for row in records))

    def test_protocol_retry_started_without_receipt_is_not_reissued_after_restart_or_retry(self):
        candidate, _ = self.followup('收到，可以继续追问。')
        calls = []
        def model(system, payload, *, repair=False):
            self.assertFalse(repair)
            protocol = '本次追加严格协议纠错' in system
            calls.append(protocol)
            if protocol:
                raise TimeoutError('协议纠错返回未知')
            result = self.audit(payload); result['paragraphs'] = []
            return result
        self.worker.learning_model = model
        with self.assertRaises(TimeoutError):
            self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertEqual(calls, [False, True])
        result = self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertFalse(result['published'])
        self.assertEqual(calls, [False, True])
        self.assertTrue(guard.retry(self.store, self.aid))
        self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertEqual(calls, [False, True, False])
        self.assertEqual(guard.snapshot(self.store, candidate['id'])['status'], 'rejected')
        self.assertFalse(any(m['id'] == candidate['message_id'] for m in self.public()))

    def test_protocol_retry_received_is_reused_after_crash_without_new_model_call(self):
        candidate, _ = self.followup('收到，可以继续追问。')
        calls = []
        def model(system, payload, *, repair=False):
            self.assertFalse(repair)
            calls.append(system)
            result = self.audit(payload)
            if len(calls) == 1:
                result['paragraphs'] = []
            return result
        original_record = guard.record_attempt
        def crash_after_receipt(*args, **kwargs):
            saved = original_record(*args, **kwargs)
            stage = (args[2] if len(args) > 2 else kwargs.get('result')) or {}
            if stage.get('stage') == 'audit_protocol_retry_received':
                raise RuntimeError('模拟收妥纠错回执后进程退出')
            return saved
        self.worker.learning_model = model
        with patch.object(guard, 'record_attempt', side_effect=crash_after_receipt):
            with self.assertRaises(RuntimeError):
                self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertEqual(len(calls), 2)
        self.worker.learning_model = lambda *args, **kwargs: self.fail('复用已收到的协议纠错不得新增模型调用')
        result = self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertTrue(result['published'])
        self.assertEqual(next(m['body'] for m in self.public() if m['id'] == candidate['message_id']), candidate['draft'])

    def test_protocol_retry_received_is_revalidated_not_trusted_from_saved_pass(self):
        candidate, _ = self.followup('收到，可以继续追问。')
        calls = []
        def model(system, payload, *, repair=False):
            self.assertFalse(repair)
            calls.append(system)
            result = self.audit(payload)
            if len(calls) == 1:
                result['paragraphs'] = []
            return result
        original_record = guard.record_attempt
        def crash_after_receipt(*args, **kwargs):
            saved = original_record(*args, **kwargs)
            stage = (args[2] if len(args) > 2 else kwargs.get('result')) or {}
            if stage.get('stage') == 'audit_protocol_retry_received':
                raise RuntimeError('模拟纠错回执已落盘但还没有发布')
            return saved
        self.worker.learning_model = model
        with patch.object(guard, 'record_attempt', side_effect=crash_after_receipt):
            with self.assertRaises(RuntimeError):
                self.worker.review_learning(self.aid, candidate['job_version'])
        row = guard.snapshot(self.store, candidate['id'])
        for entry in row['audit']:
            if entry['result'].get('stage') == 'audit_protocol_retry_received':
                entry['result']['result']['paragraphs'] = []
                entry['result']['checked'] = {'passed': True, 'issues': []}
        with self.store.db(True) as db:
            db.execute('UPDATE learning_reviews SET audit=? WHERE id=?', (json.dumps(row['audit']), candidate['id']))
        self.worker.learning_model = lambda *args, **kwargs: self.fail('同一scope已纠错，不能再次调用纠错或正文修订')
        result = self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertFalse(result['published'])
        self.assertEqual(result['status'], 'rejected')
        self.assertFalse(any(m['id'] == candidate['message_id'] for m in self.public()))

    def test_protocol_retry_still_invalid_does_not_repair_or_change_body(self):
        candidate, _ = self.followup('收到，可以继续追问。')
        calls = []
        def model(system, payload, *, repair=False):
            self.assertFalse(repair)
            calls.append(system)
            result = self.audit(payload); result['paragraphs'] = []
            return result
        self.worker.learning_model = model
        result = self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertFalse(result['published'])
        self.assertEqual(len(calls), 2)
        self.assertIn('本次追加严格协议纠错', calls[-1])
        row = guard.snapshot(self.store, candidate['id'])
        self.assertEqual(row['draft'], candidate['draft'])
        self.assertEqual(row['revised_body'], candidate['draft'])
        self.assertFalse(any(entry['result'].get('stage') == 'repair_started' for entry in row['audit']))
        self.assertFalse(any(m['id'] == candidate['message_id'] for m in self.public()))

    def test_unknown_claim_span_never_publishes_or_gets_normalized(self):
        candidate, _ = self.followup('### 理论分析\n\n假设条件相同，可以比较两种规则。')
        repair_calls = []
        def model(system, reference, *, repair=False):
            if repair:
                repair_calls.append(True)
                return candidate['draft']
            raw = self.audit(reference)
            paragraph = next(p for p in reference['payload']['paragraphs'] if p['role'] == 'body')
            result = next(p for p in raw['paragraphs'] if p['id'] == paragraph['id'])
            result['claims'] = [{'span_id': 'unknown.claim.span', 'status': 'nonfactual', 'evidence': [],
                                 'rationale': '这里只是在设置假设条件。'}]
            return raw
        self.worker.learning_model = model
        result = self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertFalse(result['published'])
        row = guard.snapshot(self.store, candidate['id'])
        self.assertIn('unknown.claim.span', json.dumps(row['audit']))
        self.assertIn('unknown_claim_span', json.dumps(row['audit']))
        self.assertEqual(repair_calls, [])
        self.assertFalse(any(entry['result'].get('stage') == 'repair_started' for entry in row['audit']))
        self.assertFalse(any(m['id'] == candidate['message_id'] for m in self.public()))

    def test_claim_span_from_another_paragraph_never_publishes(self):
        candidate, _ = self.followup('### 理论分析\n\n假设甲条件成立。\n\n假设乙条件相反。')
        repair_calls = []
        def model(system, reference, *, repair=False):
            if repair:
                repair_calls.append(True)
                return candidate['draft']
            raw = self.audit(reference)
            bodies = [p for p in reference['payload']['paragraphs'] if p['role'] == 'body']
            foreign = next(s for s in reference['claim_spans'] if s['paragraph_id'] == bodies[1]['id'])
            result = next(p for p in raw['paragraphs'] if p['id'] == bodies[0]['id'])
            result['claims'] = [{'span_id': foreign['id'], 'status': 'nonfactual', 'evidence': [],
                                 'rationale': '这是明确设置的假设。'}]
            return raw
        self.worker.learning_model = model
        result = self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertFalse(result['published'])
        row = guard.snapshot(self.store, candidate['id'])
        self.assertIn('cross_paragraph_claim_span', json.dumps(row['audit']))
        self.assertEqual(repair_calls, [])
        self.assertFalse(any(entry['result'].get('stage') == 'repair_started' for entry in row['audit']))
        self.assertFalse(any(m['id'] == candidate['message_id'] for m in self.public()))

    def test_unknown_source_span_never_publishes(self):
        fact = self.manifest['cards'][0]['verified_facts'][0]
        candidate, _ = self.followup('### 史实与文本依据\n\n' + fact)
        repair_calls = []
        def model(system, reference, *, repair=False):
            if repair:
                repair_calls.append(True)
                return candidate['draft']
            raw = self.audit(reference)
            claim = next(claim for paragraph in raw['paragraphs'] for claim in paragraph['claims'])
            claim['evidence'] = [{'source_span_id': 'unknown.source.span'}]
            return raw
        self.worker.learning_model = model
        result = self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertFalse(result['published'])
        row = guard.snapshot(self.store, candidate['id'])
        self.assertIn('unknown.source.span', json.dumps(row['audit']))
        self.assertIn('unknown_source_span', json.dumps(row['audit']))
        self.assertEqual(repair_calls, [])
        self.assertFalse(any(entry['result'].get('stage') == 'repair_started' for entry in row['audit']))
        self.assertFalse(any(m['id'] == candidate['message_id'] for m in self.public()))

    def test_reference_ids_are_deterministic_and_raw_audit_is_persisted_unchanged(self):
        from dsh_personal.learning_content import reference_audit_payload, validate_reference_audit
        fact = self.manifest['cards'][0]['verified_facts'][0]
        candidate, _ = self.followup('### 史实与文本依据\n\n' + fact)
        seen = []
        def model(system, reference, *, repair=False):
            self.assertFalse(repair)
            self.assertEqual(reference_audit_payload(reference['payload']), reference)
            raw = self.audit(reference)
            self.assertTrue(validate_reference_audit(reference, raw)['passed'])
            seen.append(json.loads(json.dumps(raw)))
            return raw
        self.worker.learning_model = model
        result = self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertTrue(result['published'])
        self.assertEqual(len(seen), 1)
        records = [entry['result'] for entry in guard.snapshot(self.store, candidate['id'])['audit']
                   if entry['result'].get('stage') == 'audit']
        self.assertEqual(records[0]['result'], seen[0])
        self.assertEqual(records[0]['reference_sha256'], seen[0]['reference_sha256'])
        claim = next(claim for paragraph in seen[0]['paragraphs'] for claim in paragraph['claims'])
        self.assertEqual(set(claim), {'span_id', 'status', 'evidence'})
        self.assertEqual(set(claim['evidence'][0]), {'source_span_id'})
        self.assertEqual(next(m['body'] for m in self.public() if m['id'] == candidate['message_id']), candidate['draft'])

    def test_full_pass_does_not_publish_when_premise_audit_fails_twice(self):
        from dsh_personal.learning_content import PREMISE_REFERENCE_SYSTEM
        candidate = self.initial_analysis('这里的事实前提尚需核对。')
        calls = []
        def model(system, payload, *, repair=False):
            stage = 'repair' if repair else 'premise' if system == PREMISE_REFERENCE_SYSTEM else 'full'
            calls.append(stage)
            if repair:
                return self.lesson_text('修订后仍有需要核对的事实前提。')
            return self.audit(payload, passed=stage == 'full')
        self.worker.learning_model = model
        result = self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertFalse(result['published'])
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(calls, ['full', 'premise', 'repair', 'full', 'premise'])
        self.assertFalse(any(m['id'] == candidate['message_id'] for m in self.public()))
        self.assertTrue(guard.retry(self.store, self.aid))
        self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertEqual(calls[-2:], ['full', 'premise'])
        self.assertEqual(calls.count('repair'), 1)
        self.assertEqual(guard.snapshot(self.store, candidate['id'])['status'], 'rejected')

    def test_premise_failure_can_publish_only_after_both_revised_audits_pass(self):
        from dsh_personal.learning_content import PREMISE_REFERENCE_SYSTEM
        candidate = self.initial_analysis('初稿含有待核对的事实前提。')
        calls = []
        revised = self.lesson_text('以下只是条件推演，不能据此确认实际发生。')
        def model(system, payload, *, repair=False):
            stage = 'repair' if repair else 'premise' if system == PREMISE_REFERENCE_SYSTEM else 'full'
            calls.append(stage)
            if repair:
                self.assertFalse(any(m['id'] == candidate['message_id'] for m in self.public()))
                return revised
            return self.audit(payload, passed=stage != 'premise' or calls.count('premise') > 1)
        self.worker.learning_model = model
        result = self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertTrue(result['published'])
        self.assertEqual(calls, ['full', 'premise', 'repair', 'full', 'premise'])
        self.assertEqual(next(m['body'] for m in self.public() if m['id'] == candidate['message_id']), revised)
        audits = [entry['result'] for entry in guard.snapshot(self.store, candidate['id'])['audit']
                  if entry['result'].get('stage') == 'premise_audit']
        self.assertEqual(len(audits), 2)
        self.assertTrue(all(len(audit['full_draft_sha256']) == 64 and len(audit['policy_sha256']) == 64 for audit in audits))
        self.assertNotEqual(audits[0]['full_draft_sha256'], audits[1]['full_draft_sha256'])

    def test_premise_timeout_retries_only_missing_specialized_audit(self):
        from dsh_personal.learning_content import PREMISE_REFERENCE_SYSTEM
        candidate = self.initial_analysis('在条件成立时，可以进一步比较规则。')
        calls = []
        def model(system, payload, *, repair=False):
            stage = 'repair' if repair else 'premise' if system == PREMISE_REFERENCE_SYSTEM else 'full'
            calls.append(stage)
            if stage == 'premise' and calls.count('premise') == 1:
                raise TimeoutError('专项审稿未返回')
            self.assertFalse(repair)
            return self.audit(payload)
        self.worker.learning_model = model
        with self.assertRaises(TimeoutError):
            self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertFalse(any(m['id'] == candidate['message_id'] for m in self.public()))
        result = self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertTrue(result['published'])
        self.assertEqual(calls, ['full', 'premise', 'premise'])

    def test_premise_timeout_after_repair_does_not_repeat_full_audit_or_repair(self):
        from dsh_personal.learning_content import PREMISE_REFERENCE_SYSTEM
        candidate = self.initial_analysis('原稿的事实前提需要核对。')
        calls = []
        def model(system, payload, *, repair=False):
            stage = 'repair' if repair else 'premise' if system == PREMISE_REFERENCE_SYSTEM else 'full'
            calls.append(stage)
            if repair:
                return self.lesson_text('修订后仅进行带条件的推演。')
            if stage == 'premise' and calls.count('premise') == 2:
                raise TimeoutError('修订稿专项审稿未返回')
            return self.audit(payload, passed=stage != 'premise' or calls.count('premise') > 1)
        self.worker.learning_model = model
        with self.assertRaises(TimeoutError):
            self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertFalse(any(m['id'] == candidate['message_id'] for m in self.public()))
        result = self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertTrue(result['published'])
        self.assertEqual(calls, ['full', 'premise', 'repair', 'full', 'premise', 'premise'])

    def test_late_review_after_session_rotation_is_cancelled(self):
        candidate, _ = self.followup('收到，可以继续追问。')
        def model(system, payload, *, repair=False):
            with self.store.db(True) as db:
                db.execute("UPDATE activities SET session_id='new-session' WHERE id=?", (self.aid,))
            return self.audit(payload)
        self.worker.learning_model = model
        self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertEqual(guard.snapshot(self.store, candidate['id'])['status'], 'cancelled')
        self.assertFalse(any(m['id'] == candidate['message_id'] for m in self.public()))

    def test_late_review_after_user_source_deletion_does_not_publish(self):
        candidate, mid = self.followup('收到，可以继续追问。')
        def model(system, payload, *, repair=False):
            from dsh_personal.deletion import apply_message_deletion
            with self.store.db(True) as db:
                apply_message_deletion(db, mid, self.store.stamp())
            return self.audit(payload)
        self.worker.learning_model = model
        self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertEqual(guard.snapshot(self.store, candidate['id'])['status'], 'deleted')
        self.assertFalse(any(m['id'] == candidate['message_id'] for m in self.public()))
        self.assertEqual(guard.snapshot(self.store, candidate['id'])['draft'], '')

    def test_late_review_after_preference_change_is_cancelled(self):
        candidate, _ = self.followup('收到，可以继续追问。')
        def model(system, payload, *, repair=False):
            self.app.records.add_profile('本次测试要求更详细。', category='preference', scope='routine:A1',
                                         confirmed=True, user_input='请保存本次测试要求。')
            return self.audit(payload)
        self.worker.learning_model = model
        self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertEqual(guard.snapshot(self.store, candidate['id'])['status'], 'cancelled')
        self.assertFalse(any(m['id'] == candidate['message_id'] for m in self.public()))

    def test_preferences_changed_during_generation_leave_fixed_visible_status(self):
        self.bridge.event_list = self.native_turn('PRIVATE_DRAFT_WITH_OLD_PREFERENCES')
        self.app.records.add_profile('以后先说明证据范围。', category='preference', scope='routine:A1',
                                     confirmed=True, user_input='请保存这条测试要求。')
        self.worker.sync_all()
        view = self.store.activity(self.aid)
        self.assertEqual(view['messages'], self.initial_messages)
        self.assertEqual(self.candidates(), [])
        self.assertIsNotNone(view['processing'])
        self.assertNotIn('PRIVATE', json.dumps(view))
        self.assertIsNone(self.activity()['ready_at'])

    def test_activity_retry_only_requeues_existing_review(self):
        candidate, _ = self.followup()
        token = guard.record_attempt(self.store, candidate['id'])['attempt_token']
        guard.commit(self.store, candidate['id'], False, attempt_token=token)
        original_messages = self.public()
        result = self.app.action({'op': 'retry', 'activity_id': self.aid})
        self.assertIn('核对', result['message'])
        self.assertEqual(self.public(), original_messages)
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE kind='native_retry'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE kind='learning_review' AND status='pending'").fetchone()[0], 1)
        self.assertEqual(len(self.candidates()), 1)

    def test_approved_review_retry_does_not_enqueue_native_regeneration(self):
        candidate, mid = self.followup('收到，可以继续追问。')
        with self.store.db(True) as db:
            db.execute("UPDATE messages SET processing_state='accepted_by_dsh' WHERE id=?", (mid,))
        self.worker.learning_model = lambda system, payload, **kwargs: self.audit(payload)
        self.worker.review_learning(self.aid, candidate['job_version'])
        before = self.public()
        targeted = self.app.action({'op': 'retry', 'activity_id': self.aid})
        self.assertIn('已经核对', targeted['message'])
        self.app.action({'op': 'retry'})
        self.assertEqual(self.public(), before)
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE kind='native_retry'").fetchone()[0], 0)

    def test_inbox_only_completed_turn_is_captured(self):
        for before_start in (True, False):
            with self.subTest(before_start=before_start):
                start = 10 if before_start else 30
                events = self.native_turn(start=start)
                inbox = {'seq': start - 1 if before_start else start + 1,
                         'type': 'agent/inbox/spliced', 'data': {'inserted': [
                             {'source': {'kind': 'user', 'rpcId': 'learning-' + self.aid},
                              'content': [{'type': 'text', 'text': '测试课程请求'}]}]}}
                self.bridge.event_list = [inbox, events[0], events[2], events[3]]
                self.worker.sync_all()
                self.assertTrue(any(c['end_seq'] == start + 3 for c in self.candidates()))
                self.assertEqual(self.public(), self.initial_messages)

    def test_old_native_question_reply_is_captured_with_its_user_sources(self):
        with self.store.db(True) as db:
            db.execute("UPDATE activities SET ready_at=?,status='awaiting_reply' WHERE id=?", (self.store.stamp(), self.aid))
        receipt = self.store.submit(self.aid, 'isolated-question-reply', '我认为条件还不充分。',
                                    call_id='old-question-batch', question_id='old-question')
        events = self.native_turn()
        events[1] = {'seq': 11, 'type': 'user/message', 'data': {
            'source': {'kind': 'user-question-reply', 'callId': 'old-question-batch'},
            'content': [{'type': 'text', 'text': '我认为条件还不充分。'}]}}
        self.bridge.event_list = events
        self.worker.sync_all()
        candidate = self.candidates()[0]
        self.assertEqual(candidate['turn']['request_id'], 'isolated-question-reply')
        self.assertEqual(candidate['user_message_ids'], [receipt['receipt']])
        self.assertEqual(candidate['mode'], 'followup')
        self.assertFalse(any(m['id'] == candidate['message_id'] for m in self.public()))

    def test_later_inbox_does_not_rebind_admitted_turn_or_cover_future_message(self):
        with self.store.db(True) as db:
            db.execute("UPDATE activities SET ready_at=?,status='awaiting_reply' WHERE id=?", (self.store.stamp(), self.aid))
        first = self.store.submit(self.aid, 'isolated-first-request', '请先解释条文。')
        second = self.store.submit(self.aid, 'isolated-second-request', '后一个问题先等着。')
        # Both submissions can already have registered the session-wide context
        # while the native writer is still finishing the first admitted turn.
        from dsh_personal.deletion import register_context
        register_context(self.store, self.sid, ['message:' + first['receipt'], 'message:' + second['receipt']])
        events = self.native_turn(request='isolated-first-request')
        events[-1]['seq'] = 14
        events.insert(-1, {'seq': 13, 'type': 'agent/inbox/spliced', 'data': {'inserted': [
            {'source': {'kind': 'user', 'rpcId': 'isolated-second-request'}}]}})
        self.bridge.event_list = events
        self.worker.sync_all()
        candidate = self.candidates()[0]
        self.assertEqual(candidate['turn']['request_id'], 'isolated-first-request')
        self.assertIn(first['receipt'], candidate['user_message_ids'])
        self.assertNotIn(second['receipt'], candidate['user_message_ids'])

    def test_inbox_only_request_remains_bound_after_answer_and_late_inbox(self):
        with self.store.db(True) as db:
            db.execute("UPDATE activities SET ready_at=?,status='awaiting_reply' WHERE id=?", (self.store.stamp(), self.aid))
        first = self.store.submit(self.aid, 'isolated-inbox-q1', '请解释第一条。')
        second = self.store.submit(self.aid, 'isolated-inbox-q2', '然后再解释第二条。')
        from dsh_personal.deletion import register_context
        register_context(self.store, self.sid, ['message:' + first['receipt'], 'message:' + second['receipt']])
        initial = self.native_turn('第一轮草稿', request='isolated-inbox-q1')
        initial[-1]['seq'] = 14
        self.bridge.event_list = [
            {'seq': 9, 'type': 'agent/inbox/spliced', 'data': {'inserted': [
                {'source': {'kind': 'user', 'rpcId': 'isolated-inbox-q1'}}]}},
            initial[0], initial[2],
            {'seq': 13, 'type': 'agent/inbox/spliced', 'data': {'inserted': [
                {'source': {'kind': 'user', 'rpcId': 'isolated-inbox-q2'}}]}},
            initial[-1],
        ]
        self.worker.sync_all()
        candidate = self.candidates()[0]
        self.assertEqual(candidate['turn']['request_id'], 'isolated-inbox-q1')
        self.assertEqual(candidate['user_message_ids'], [first['receipt']])
        following = self.native_turn('第二轮草稿', start=20, turn=2)
        self.bridge.event_list += [following[0], following[2], following[3]]
        self.worker.sync_all()
        candidate = self.candidates()[-1]
        self.assertEqual(candidate['turn']['request_id'], 'isolated-inbox-q2')
        self.assertIn(second['receipt'], candidate['user_message_ids'])
        self.assertFalse(any(m['processing_state'] == 'learning_reviewed' for m in self.public()))

    def test_consumed_inbox_does_not_override_newer_failed_request_on_resume(self):
        with self.store.db(True) as db:
            db.execute("UPDATE activities SET ready_at=?,status='awaiting_reply' WHERE id=?", (self.store.stamp(), self.aid))
        first = self.store.submit(self.aid, 'isolated-consumed-q1', '请先解释第一条。')
        second = self.store.submit(self.aid, 'isolated-admitted-q2', '继续解释第二条。')
        initial = self.native_turn('第一轮草稿', request='isolated-consumed-q1')
        failed = self.native_turn('未完成的第二轮草稿', start=20, turn=2,
                                  request='isolated-admitted-q2', reason='error')
        resumed = self.native_turn('恢复后的第二轮草稿', start=30, turn=3)
        self.bridge.event_list = [
            {'seq': 9, 'type': 'agent/inbox/spliced', 'data': {'inserted': [
                {'source': {'kind': 'user', 'rpcId': 'isolated-consumed-q1'}}]}},
            initial[0], initial[2], initial[3],
        ] + failed + [resumed[0], resumed[2], resumed[3]]
        self.worker.sync_all()
        rows = self.candidates()
        self.assertEqual([row['end_seq'] for row in rows], [13, 33])
        self.assertEqual(rows[0]['turn']['request_id'], 'isolated-consumed-q1')
        self.assertIn(first['receipt'], rows[0]['user_message_ids'])
        self.assertEqual(rows[1]['turn']['request_id'], 'isolated-admitted-q2')
        self.assertIn(second['receipt'], rows[1]['user_message_ids'])
        self.assertEqual(rows[1]['draft'], '恢复后的第二轮草稿')

    def test_cancelled_old_session_does_not_mask_current_session_status_or_retry(self):
        candidate, mid = self.followup('收到，可以继续追问。')
        token = guard.record_attempt(self.store, candidate['id'])['attempt_token']
        with self.store.db(True) as db:
            db.execute("UPDATE activities SET session_id='new-session' WHERE id=?", (self.aid,))
            db.execute("UPDATE messages SET processing_state='accepted_by_dsh' WHERE id=?", (mid,))
        guard.commit(self.store, candidate['id'], True, attempt_token=token)
        self.store.set_meta('learning_guard_wait:' + self.aid, json.dumps({
            'session_id': 'new-session', 'status': 'failed', 'source_user_ids': [mid],
            'message': '本轮原生回复尚未完成。'}))
        self.assertEqual(self.store.activity(self.aid)['processing']['message'], '本轮原生回复尚未完成。')
        self.app.action({'op': 'retry', 'activity_id': self.aid})
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE kind='native_retry' AND target=?", (self.aid,)).fetchone()[0], 1)

    def test_review_job_runs_without_native_readiness_and_sanitizes_network_failure(self):
        candidate, _ = self.followup('收到，可以继续追问。')
        with self.store.db(True) as db:
            db.execute("UPDATE jobs SET status='done' WHERE kind!='learning_review'")
        def native_should_not_run(*args, **kwargs):
            raise AssertionError('review must not depend on native readiness')
        def model(*args, **kwargs):
            raise RuntimeError('PRIVATE_PROVIDER_ERROR')
        self.worker.native_ready = native_should_not_run
        self.worker.learning_model = model
        self.assertTrue(self.worker.work_one())
        with self.store.db() as db:
            row = db.execute("SELECT status,error FROM jobs WHERE kind='learning_review'").fetchone()
        self.assertEqual(row['status'], 'pending')
        self.assertNotIn('PRIVATE_PROVIDER_ERROR', row['error'])
        self.assertNotIn('PRIVATE_PROVIDER_ERROR', json.dumps(self.store.activity(self.aid)))
        self.assertEqual(self.bridge.submissions, [])

    def test_complete_first_lesson_only_becomes_ready_after_review(self):
        chunks = []
        for card in self.manifest['cards']:
            chunks += ['## ' + card['title'], '出处：[' + card['source_title'] + '](' + card['source_url'] + ')',
                       '### 史实与文本依据', card['verified_facts'][0],
                       '### 理论分析', '以上文本可以作为分析对象，暂不外推实际效果。',
                       '### 现实类比（假设）', '假设有一个小组，可比较规则是否完整；这不说明历史组织的实际规模。']
        world = self.manifest.get('world_fun_fact')
        if world:
            chunks += ['## 外国史一句趣闻', world['sentence'] + ' [出处](' + world['source_url'] + ')']
        self.bridge.event_list = self.native_turn('\n\n'.join(chunks))
        self.worker.sync_all()
        self.assertIsNone(self.activity()['ready_at'])
        candidate = self.candidates()[0]
        self.worker.learning_model = lambda system, payload, **kwargs: self.audit(payload)
        result = self.worker.review_learning(self.aid, candidate['job_version'])
        self.assertTrue(result['published'])
        self.assertIsNotNone(self.activity()['ready_at'])
        selection = json.loads(self.store.meta('learning_selection:' + self.aid))
        self.assertEqual(selection['delivery']['message_id'], candidate['message_id'])


    def test_cancelled_review_job_is_not_resurrected_by_late_network_error(self):
        candidate, mid = self.followup()
        with self.store.db(True) as db:
            db.execute("UPDATE jobs SET status='done' WHERE kind!='learning_review'")
        def network(system, payload, **kwargs):
            with self.store.db(True) as db:
                guard.scrub(db, message_ids=[mid], stamp=self.store.stamp())
            raise OSError('private upstream details')
        self.worker.learning_model = network
        self.worker.work_one()
        with self.store.db() as db:
            job = db.execute("SELECT status,error FROM jobs WHERE kind='learning_review'").fetchone()
        self.assertEqual(job['status'], 'cancelled')
        self.assertIsNone(job['error'])
        self.assertEqual(guard.snapshot(self.store,candidate['id'])['status'],'deleted')


if __name__ == '__main__':
    unittest.main()
