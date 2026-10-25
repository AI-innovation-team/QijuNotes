import datetime as dt
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dsh_personal.core import Store, TZ
from dsh_personal.records import RecordsStore
from dsh_personal import learning_guard as guard


class LearningGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = dt.datetime(2026, 10, 23, 22, tzinfo=TZ)
        self.store = Store(Path(self.tmp.name) / 'personal.sqlite', lambda: self.now)
        self.records = RecordsStore(self.store.path, lambda: self.now)
        with self.store.db(True) as db:
            guard.initialize(db)
        self.activity = self.store.ensure_activity('A1')
        self.activity['session_id'] = 'test-session'
        self.activity['session_state'] = 'ready'
        with self.store.db(True) as db:
            db.execute("UPDATE activities SET session_id=?,session_state='ready' WHERE id=?", ('test-session', self.activity['id']))
        self.manifest = {'version': 2, 'activity_id': self.activity['id'],
                         'cards': [{'id': 'fixture-source', 'verified_facts': ['史料仅载甲事件。']}], 'delivery': None}
        self.store.set_meta('learning_selection:' + self.activity['id'], json.dumps(self.manifest))
        self.draft = 'PRIVATE UNSUPPORTED DRAFT: 原资料以外的具体历史细节。'
        self.final = '史实：史料仅载甲事件。\n理论分析：以下是对材料关系的解释。'

    def tearDown(self):
        self.tmp.cleanup()

    def enqueue(self, seq=8, **kwargs):
        return guard.enqueue(self.store, self.activity,
                             {'session_id': self.activity['session_id'], 'end_seq': seq + 1,
                              'assistant_seq': seq, 'received_at': self.store.stamp()},
                             self.draft, self.manifest, self.records.context('routine:A1'), **kwargs)

    def start(self, row):
        return guard.record_attempt(self.store, row['id'])['attempt_token']

    def commit(self, row, approved=True, token=None, **kwargs):
        token = token or self.start(row)
        return guard.commit(self.store, row['id'], approved, attempt_token=token, **kwargs)

    def public_messages(self):
        return self.store.activity(self.activity['id'])['messages']

    def test_pending_private_across_views_and_readable_export(self):
        from dsh_personal.operations import _markdown_from
        row = self.enqueue()
        self.assertEqual(row['status'], 'pending')
        self.assertNotIn(self.draft, json.dumps(self.store.activity(self.activity['id'])))
        self.assertNotIn(self.draft, json.dumps(self.store.journal()))
        self.assertNotIn(self.draft, _markdown_from(self.store.path, self.records))
        public = guard.public_status(self.store, self.activity['id'])
        self.assertEqual(set(public), {'state', 'status', 'message', 'mode', 'retryable'})
        self.assertNotIn('PRIVATE', json.dumps(public))

    def test_success_publishes_revised_body_once_with_original_native_identity(self):
        row = self.enqueue()
        token = self.start(row)
        guard.record_attempt(self.store, row['id'], {'passed': False, 'unsafe': self.draft}, attempt_token=token)
        guard.record_attempt(self.store, row['id'], {'passed': True}, self.final, attempt_token=token)
        result = self.commit(row, token=token)
        self.assertTrue(result['published'])
        message = next(m for m in self.public_messages() if m['id'] == row['message_id'])
        self.assertEqual(message['body'], self.final)
        self.assertEqual(message['processing_state'], 'learning_reviewed')
        self.assertEqual(message['native_id'], '8')
        self.assertEqual(message['id'], 'dsh:test-session:8')
        self.assertFalse(self.commit(row, token=token)['published'])
        self.assertEqual(len([m for m in self.public_messages() if m['id'] == row['message_id']]), 1)
        self.assertIsNone(guard.public_status(self.store, self.activity['id']))
        activity = self.store.activity(self.activity['id'])['activity']
        self.assertIsNotNone(activity['ready_at'])
        self.assertEqual(activity['status'], 'awaiting_reply')
        self.assertEqual(json.loads(self.store.meta('learning_selection:' + self.activity['id']))['delivery']['message_id'], row['message_id'])

    def test_legacy_missing_manifest_accepts_only_empty_packet(self):
        with self.store.db(True) as db:
            db.execute('DELETE FROM meta WHERE key=?', ('learning_selection:' + self.activity['id'],))
        self.manifest = {'cards': []}
        row = self.enqueue(mode='followup'); token = self.start(row)
        self.assertTrue(self.commit(row, token=token, revised_body=self.final)['published'])
        later = self.enqueue(seq=12, mode='followup'); token = self.start(later)
        self.store.set_meta('learning_selection:' + self.activity['id'], json.dumps({'cards': [{'id': 'new'}]}))
        self.assertEqual(self.commit(later, token=token)['status'], 'cancelled')

    def test_network_failure_can_retry_retained_review_without_regeneration(self):
        row = self.enqueue(); self.start(row)
        with self.store.db(True) as db:
            db.execute("UPDATE jobs SET status='failed' WHERE kind='learning_review'")
        public = guard.public_status(self.store, self.activity['id'])
        self.assertTrue(public['retryable']); self.assertEqual(public['status'], 'failed')
        self.assertTrue(guard.retry(self.store, self.activity['id']))
        self.assertEqual(guard.get_pending(self.store, self.activity['id'])['draft'], self.draft)

    def test_rejection_and_retry_reuse_one_candidate_one_job_no_new_user(self):
        row = self.enqueue()
        self.commit(row, approved=False, audit={'private': self.draft})
        self.assertNotIn(self.draft, json.dumps(self.public_messages()))
        self.assertTrue(guard.public_status(self.store, self.activity['id'])['retryable'])
        self.assertTrue(guard.retry(self.store, self.activity['id']))
        self.assertFalse(guard.retry(self.store, self.activity['id']))
        self.assertEqual(self.enqueue()['id'], row['id'])
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM learning_reviews').fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE kind='learning_review'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE role='user'").fetchone()[0], 0)
        self.assertTrue(self.commit(row, revised_body=self.final)['published'])

    def test_duplicate_ingestion_never_overwrites_candidate_inputs(self):
        row = self.enqueue()
        self.draft = 'NEW unreviewed draft'
        self.manifest['cards'][0]['verified_facts'] = ['changed']
        repeated = self.enqueue()
        self.assertEqual(repeated['draft'], row['draft'])
        self.assertEqual(repeated['manifest_hash'], row['manifest_hash'])

    def test_existing_public_history_is_never_adopted_or_changed(self):
        with self.store.db(True) as db:
            db.execute("INSERT INTO messages VALUES(?,?,NULL,'assistant',?,?,'native','8',NULL,0)",
                       ('dsh:test-session:8', self.activity['id'], 'retained old answer', self.store.stamp()))
        self.assertIsNone(self.enqueue())
        self.assertEqual(self.public_messages()[-1]['body'], 'retained old answer')
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM learning_reviews').fetchone()[0], 0)

    def test_source_packet_change_blocks_late_approval(self):
        row = self.enqueue(); token = self.start(row)
        changed = json.loads(json.dumps(self.manifest))
        changed['cards'][0]['verified_facts'].append('new fact')
        self.store.set_meta('learning_selection:' + self.activity['id'], json.dumps(changed))
        result = self.commit(row, token=token, revised_body=self.final)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['reason'], 'source_changed')
        self.assertNotIn(self.final, json.dumps(self.public_messages()))

    def test_delivery_metadata_does_not_change_source_packet(self):
        row = self.enqueue(); token = self.start(row)
        self.manifest['delivery'] = {'message_id': 'previous-reviewed-message'}
        self.store.set_meta('learning_selection:' + self.activity['id'], json.dumps(self.manifest))
        self.assertTrue(self.commit(row, token=token, revised_body=self.final)['published'])

    def test_saved_requirement_addition_and_revision_block_late_results(self):
        row = self.enqueue(); token = self.start(row)
        self.records.add_profile('以后学习要区分史实与分析。', category='preference', scope='routine:A1', confirmed=True, user_input='本人要求')
        self.assertEqual(self.commit(row, token=token)['reason'], 'preferences_changed')
        next_row = self.enqueue(seq=12); token = self.start(next_row)
        profile = self.records.context('routine:A1')[0]
        self.records.revise_profile(profile['item_id'], 1, '更新要求', content='以后分析还须交代适用边界。')
        self.assertEqual(self.commit(next_row, token=token)['reason'], 'preferences_changed')

    def test_unrelated_routine_preferences_do_not_cancel(self):
        row = self.enqueue(); token = self.start(row)
        self.records.add_profile('睡眠交流温柔一些。', category='preference', scope='routine:A3', confirmed=True, user_input='本人要求')
        self.assertTrue(self.commit(row, token=token, revised_body=self.final)['published'])

    def test_session_rotation_and_deleted_source_fence_late_callbacks(self):
        row = self.enqueue(); token = self.start(row)
        with self.store.db(True) as db:
            db.execute('UPDATE activities SET session_id=? WHERE id=?', ('replacement', self.activity['id']))
        self.assertEqual(self.commit(row, token=token)['reason'], 'session_changed')

    def test_followup_source_deleted_is_blocked_before_scrub(self):
        mid = self.store.submit(self.activity['id'], 'source-user-1', '这个解释有什么边界？')['receipt']
        row = self.enqueue(mode='followup', user_message_ids=[mid]); token = self.start(row)
        with self.store.db(True) as db:
            db.execute('UPDATE messages SET deleted=1,body=\'\' WHERE id=?', (mid,))
        result = self.commit(row, token=token)
        self.assertEqual(result['status'], 'deleted')
        self.assertEqual(result['reason'], 'source_deleted')
        self.assertIsNone(guard.public_status(self.store, self.activity['id']))
        self.assertEqual(guard.snapshot(self.store, row['id'])['draft'], '')

    def test_source_record_tombstone_blocks_initial_review(self):
        row = self.enqueue(source_refs=['import:source-record'])
        with self.store.db(True) as db:
            db.execute('INSERT INTO records_tombstones VALUES(?,NULL,NULL,?,?)', ('source-record', self.store.stamp(), 'global'))
        self.assertIsNone(guard.record_attempt(self.store, row['id']))
        self.assertEqual(guard.snapshot(self.store, row['id'])['status'], 'deleted')

    def test_late_native_draft_cannot_retain_deleted_source_content(self):
        with self.store.db(True) as db:
            db.execute('INSERT INTO records_tombstones VALUES(?,NULL,NULL,?,?)', ('already-erased', self.store.stamp(), 'global'))
        row = self.enqueue(source_refs=['import:already-erased'])
        self.assertEqual(row['status'], 'deleted')
        self.assertEqual(row['draft'], '')
        self.assertEqual(row['manifest'], {})
        with self.store.db() as db:
            self.assertFalse(db.execute("SELECT 1 FROM jobs WHERE kind='learning_review'").fetchone())

    def test_old_attempt_cannot_record_or_publish_after_reclaim(self):
        row = self.enqueue(); old = self.start(row); new = self.start(row)
        self.assertNotEqual(old, new)
        self.assertIsNone(guard.record_attempt(self.store, row['id'], {'passed': True}, attempt_token=old))
        self.assertFalse(self.commit(row, token=old, revised_body='stale')['published'])
        self.assertTrue(self.commit(row, token=new, revised_body=self.final)['published'])

    def test_concurrent_commit_publishes_exactly_once(self):
        row = self.enqueue(); token = self.start(row)
        barrier = threading.Barrier(2)
        results, errors = [], []
        def work():
            try:
                barrier.wait()
                results.append(self.commit(row, token=token, revised_body=self.final))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=work) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(sum(r['published'] for r in results), 1)

    def test_scrub_removes_all_private_text_and_blocks_late_approval(self):
        row = self.enqueue(source_refs=['import:private-source']); token = self.start(row)
        guard.record_attempt(self.store, row['id'], {'quote': self.draft}, 'private repaired text', attempt_token=token)
        with self.store.db(True) as db:
            self.assertEqual(guard.scrub(db, source_refs=['import:private-source'], stamp=self.store.stamp()), [row['id']])
        retained = guard.snapshot(self.store, row['id'])
        for field, empty in [('draft', ''), ('revised_body', None), ('manifest', {}), ('audit', []), ('preferences', []), ('source_refs', []), ('turn', {})]:
            self.assertEqual(retained[field], empty)
        self.assertEqual(self.commit(row, token=token)['status'], 'deleted')
        self.assertEqual(self.enqueue()['status'], 'deleted')
        self.assertIsNone(guard.public_status(self.store, self.activity['id']))

    def test_scrub_published_derivative_too(self):
        row = self.enqueue(); self.commit(row, revised_body=self.final)
        with self.store.db(True) as db:
            guard.scrub(db, activity_ids=[self.activity['id']], stamp=self.store.stamp())
        self.assertNotIn(self.final, json.dumps(self.public_messages()))

    def test_recovery_keeps_published_body_but_holds_unfinished_candidate(self):
        approved = self.enqueue(); self.commit(approved, revised_body=self.final)
        pending = self.enqueue(seq=20)
        with self.store.db(True) as db:
            ledger = guard.ledger(db)
            self.assertNotIn(self.draft, json.dumps(ledger))
            self.assertNotIn(self.final, json.dumps(ledger))
            guard.recover(db, ledger, self.store.stamp())
        self.assertEqual(guard.snapshot(self.store, approved['id'])['status'], 'approved')
        self.assertEqual(guard.snapshot(self.store, pending['id'])['status'], 'recovery_hold')
        self.assertIsNone(guard.get_pending(self.store, self.activity['id']))

    def test_newer_ledger_cannot_manufacture_missing_public_message(self):
        row = self.enqueue(); self.commit(row, revised_body=self.final)
        with self.store.db(True) as db:
            ledger = guard.ledger(db)
            db.execute('DELETE FROM learning_reviews')
            db.execute('DELETE FROM messages WHERE id=?', (row['message_id'],))
            guard.recover(db, ledger, self.store.stamp())
        self.assertEqual(guard.snapshot(self.store, row['id'])['status'], 'recovery_hold')
        self.assertFalse(guard.retry(self.store, self.activity['id']))
        self.assertNotIn(self.final, json.dumps(self.public_messages()))

    def test_latest_deletion_ledger_scrubs_an_older_backup(self):
        row = self.enqueue(); self.commit(row, revised_body=self.final)
        with self.store.db(True) as db:
            old_row = dict(db.execute('SELECT * FROM learning_reviews').fetchone())
            guard.scrub(db, message_ids=[row['message_id']], stamp=self.store.stamp())
            ledger = guard.ledger(db)
            # An older private snapshot retained the text; current tombstones
            # and the latest ledger must still win during restoration.
            db.execute('DELETE FROM learning_reviews')
            db.execute('INSERT INTO learning_reviews (' + ','.join(old_row) + ') VALUES (' + ','.join('?' for _ in old_row) + ')', list(old_row.values()))
            guard.recover(db, ledger, self.store.stamp())
        restored = guard.snapshot(self.store, row['id'])
        self.assertEqual(restored['status'], 'deleted')
        self.assertEqual(restored['draft'], '')

    def test_delivery_evidence_failure_rolls_back_both_message_and_approval(self):
        self.manifest['prepared_at'] = self.store.stamp()
        self.store.set_meta('learning_selection:' + self.activity['id'], json.dumps(self.manifest))
        row = self.enqueue(); token = self.start(row)
        with patch('dsh_personal.learning._delivered_message', return_value=None):
            with self.assertRaisesRegex(ValueError, 'delivery evidence'):
                self.commit(row, token=token, revised_body=self.final)
        self.assertEqual(guard.snapshot(self.store, row['id'])['status'], 'reviewing')
        self.assertIsNone(self.store.activity(self.activity['id'])['activity']['ready_at'])
        self.assertNotIn(self.final, json.dumps(self.public_messages()))

    def test_public_json_does_not_export_private_candidate_or_audit(self):
        from dsh_personal.operations import _database_json
        row = self.enqueue(); token = self.start(row)
        guard.record_attempt(self.store, row['id'], {'private': self.draft}, 'PRIVATE REPAIRED DRAFT', attempt_token=token)
        exported = _database_json(self.store.path)
        self.assertEqual(exported['learning_reviews'][0]['id'], row['id'])
        self.assertNotIn('PRIVATE', json.dumps(exported))
        self.assertNotIn('manifest', exported['learning_reviews'][0])

    def bundle_app(self):
        data = Path(self.tmp.name)
        config = {'model': {}, 'notification': {}, 'backup': {}}
        (data / 'config.json').write_text(json.dumps(config))
        return SimpleNamespace(data=data, store=self.store, records=self.records, config=lambda: config)

    def formal_manifest(self):
        from dsh_personal.learning import prepare_learning, _source_urls
        with self.store.db(True) as db:
            db.execute('DELETE FROM meta WHERE key=?', ('learning_selection:' + self.activity['id'],))
        self.manifest = prepare_learning(self.store, self.activity['id'])['manifest']
        self.final += '\n' + '\n'.join(_source_urls(self.manifest))

    def test_bundle_restore_holds_private_pending_and_readable_files_stay_clean(self):
        from dsh_personal.operations import export_bundle, restore_bundle
        import io
        import zipfile
        self.formal_manifest()
        row = self.enqueue()
        content = export_bundle(self.bundle_app())
        with zipfile.ZipFile(io.BytesIO(content)) as bundle:
            for name in ('data.json', 'ledger.json', 'diary.md'):
                self.assertNotIn(self.draft, bundle.read(name).decode())
        target = Path(self.tmp.name) / 'restored'
        restore_bundle(content, target)
        restored = Store(target / 'personal.sqlite', lambda: self.now)
        value = guard.snapshot(restored, row['id'])
        self.assertEqual(value['status'], 'recovery_hold')
        self.assertEqual(value['draft'], self.draft)  # Private recovery data only.
        self.assertNotIn(self.draft, (target / 'diary.md').read_text())

    def test_operations_replays_derived_tombstone_into_private_candidate(self):
        from dsh_personal.operations import export_bundle, recovery_ledger, restore_bundle
        self.formal_manifest()
        row = self.enqueue(); self.commit(row, revised_body=self.final)
        app = self.bundle_app(); content = export_bundle(app)
        with self.store.db(True) as db:
            db.execute('INSERT INTO tombstones VALUES(?,?,?,?,?)', ('only-native-tomb', 'derived_message', row['message_id'], None, self.store.stamp()))
        latest = recovery_ledger(app)
        target = Path(self.tmp.name) / 'restored-tomb'
        restore_bundle(content, target, latest)
        restored = Store(target / 'personal.sqlite', lambda: self.now)
        value = guard.snapshot(restored, row['id'])
        self.assertEqual(value['status'], 'deleted')
        self.assertEqual(value['draft'], '')
        self.assertNotIn(self.final, (target / 'diary.md').read_text())

    def test_operations_replays_imported_source_tombstone_before_records_merge(self):
        from dsh_personal.operations import export_bundle, recovery_ledger, restore_bundle
        self.formal_manifest()
        row = self.enqueue(source_refs=['import:fixture-old-import'])
        app = self.bundle_app(); content = export_bundle(app)
        with self.store.db(True) as db:
            db.execute('INSERT INTO records_tombstones VALUES(?,NULL,NULL,?,?)', ('fixture-old-import', self.store.stamp(), 'global'))
        latest = recovery_ledger(app)
        target = Path(self.tmp.name) / 'restored-import'
        restore_bundle(content, target, latest)
        restored = Store(target / 'personal.sqlite', lambda: self.now)
        self.assertEqual(guard.snapshot(restored, row['id'])['draft'], '')
        self.assertEqual(guard.snapshot(restored, row['id'])['status'], 'deleted')


if __name__ == '__main__':
    unittest.main()
