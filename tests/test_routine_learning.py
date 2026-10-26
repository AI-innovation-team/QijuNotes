"""Actual-source curriculum customization, using isolated SQLite fixtures."""
import copy
import datetime as dt
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dsh_personal.core import Store, TZ, dumps
from dsh_personal.learning import (ARCHIVE_PREFIX, PREFIX, TopicUnavailable,
                                   learning_history, mark_learning_presented,
                                   prepare_learning, source_identity_frozen, source_options,
                                   topic_availability)
from dsh_personal.learning_catalog import canonical_url
from dsh_personal.learning_schedule import tick
from dsh_personal.routine_settings import DEFAULTS
from dsh_personal.records import RecordsStore
from dsh_personal.worker import Worker


class RoutineLearningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = dt.datetime(2026, 10, 22, 20, tzinfo=TZ)
        self.store = Store(Path(self.tmp.name) / 'app.sqlite', lambda: self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def settings(self, version=1, **topics):
        value = copy.deepcopy(DEFAULTS['A1'])
        value['teaching_request'] = topics.pop('teaching_request', '')
        value['topics'].update(topics)
        self.store.set_meta('routine_settings:A1', dumps({
            'routine': 'A1', 'version': version, 'settings': value, 'updated_at': self.store.stamp()}))
        return value

    def activity(self, day='2026-10-22'):
        return self.store.ensure_activity('A1', day)['id']

    def hidden(self):
        tick(self.store)
        with self.store.db() as db:
            return db.execute("SELECT a.id FROM activities a JOIN learning_releases r ON r.activity_id=a.id "
                              "WHERE business_date=? AND activated_at IS NULL", (self.store.today(),)).fetchone()[0]

    def test_different_available_topics_choose_different_actual_sources(self):
        self.settings(classic='孟子', history='陈胜', modern='ＧＤＰ')
        first = prepare_learning(self.store, self.activity('2026-10-20'))
        self.settings(2, classic='孙子兵法', history='1978', modern='分散化')
        second = prepare_learning(self.store, self.activity('2026-10-21'))
        self.assertEqual([c['id'] for c in first['manifest']['cards']],
                         ['mencius-jinxin-3', 'shiji-chenshe-48', 'bea-gdp-definition'])
        self.assertEqual([c['id'] for c in second['manifest']['cards']],
                         ['sunzi-mougong', 'cpc-communique-1978', 'sec-diversification'])
        self.assertEqual(second['manifest']['routine_settings_version'], 2)
        self.assertEqual(second['manifest']['source_selection']['mode'], 'custom_topics')

    def test_unavailable_topic_is_explicit_and_does_not_save_unrelated_lesson(self):
        value = self.settings(history='绝不存在的隔离试验专题')
        aid = self.activity()
        with self.store.db() as db:
            self.assertEqual(topic_availability(db, value, self.store.today()),
                             [{'category': 'history', 'topic': '绝不存在的隔离试验专题'}])
        with self.assertRaises(TopicUnavailable) as caught:
            prepare_learning(self.store, aid)
        self.assertEqual(caught.exception.as_dict()['status'], 'source_needed')
        self.assertIn('可用正文', str(caught.exception))
        self.assertTrue(caught.exception.available)
        self.assertIsNone(self.store.meta(PREFIX + aid))

    def test_published_activity_keeps_snapshot_after_content_settings_change(self):
        self.settings(classic='孟子')
        aid = self.activity('2026-10-20')
        old = prepare_learning(self.store, aid)['manifest']
        text = '\n'.join(c['source_url'] for c in old['cards']) + '\n' + old['world_fun_fact']['source_url']
        with self.store.db(True) as db:
            db.execute('INSERT INTO messages VALUES(?,?,NULL,?,?,?,?,?,NULL,0)',
                       ('delivered-fixture', aid, 'assistant', text, self.store.stamp(), 'native', '42'))
        self.assertTrue(mark_learning_presented(self.store, aid, 'delivered-fixture'))
        original = self.store.meta(PREFIX + aid)
        self.settings(2, classic='孙子兵法')
        revised = prepare_learning(self.store, aid)['manifest']
        self.assertEqual(revised['cards'], old['cards'])
        self.assertEqual(revised['routine_settings_version'], 1)
        self.assertEqual(self.store.meta(PREFIX + aid), original)
        self.assertIsNone(self.store.meta(ARCHIVE_PREFIX + aid + ':1'))

    def test_manual_placeholder_without_delivery_reselects(self):
        self.settings(classic='孟子')
        aid = self.activity()
        first = prepare_learning(self.store, aid)['manifest']
        self.settings(2, classic='孙子兵法')
        second = prepare_learning(self.store, aid)['manifest']
        self.assertNotEqual(first['cards'][0]['id'], second['cards'][0]['id'])

    def test_existing_user_discussion_freezes_unfinished_lesson(self):
        self.settings(classic='孟子')
        aid = self.activity()
        first = prepare_learning(self.store, aid)['manifest']
        self.store.submit(aid, 'reply-fixture', '先谈谈这篇文章。')
        self.settings(2, classic='孙子兵法')
        self.assertEqual(prepare_learning(self.store, aid)['manifest'], first)
        with self.store.db() as db:
            self.assertTrue(source_identity_frozen(db, aid))

    def test_edited_published_message_freezes_sources_even_without_ready_pointer(self):
        self.settings(classic='孟子')
        aid = self.activity()
        first = prepare_learning(self.store, aid)['manifest']
        with self.store.db(True) as db:
            db.execute('INSERT INTO messages VALUES(?,?,NULL,?,?,?,?,?,NULL,0)',
                       ('edited-fixture', aid, 'assistant', '隔离修订正文。', self.store.stamp(), 'learning_edited', '42'))
        self.settings(2, classic='孙子兵法')
        self.assertEqual(prepare_learning(self.store, aid)['manifest'], first)

    def test_private_preparation_reselects_and_archives_exact_old_snapshot(self):
        self.settings(classic='孟子')
        aid = self.hidden()
        first = prepare_learning(self.store, aid)['manifest']
        raw = self.store.meta(PREFIX + aid)
        self.settings(2, classic='孙子兵法')
        second = prepare_learning(self.store, aid)['manifest']
        self.assertNotEqual(first['cards'][0]['id'], second['cards'][0]['id'])
        self.assertEqual(second['routine_settings_version'], 2)
        self.assertEqual(self.store.meta(ARCHIVE_PREFIX + aid + ':1'), raw)
        self.assertEqual(len(learning_history(self.store)), 1)
        self.assertEqual(prepare_learning(self.store, aid)['manifest'], second)

    def test_failed_reselection_preserves_existing_preparation(self):
        self.settings(classic='孟子')
        aid = self.hidden()
        prepare_learning(self.store, aid)
        raw = self.store.meta(PREFIX + aid)
        self.settings(2, classic='绝不存在的隔离试验专题')
        with self.assertRaises(TopicUnavailable):
            prepare_learning(self.store, aid)
        self.assertEqual(self.store.meta(PREFIX + aid), raw)
        self.assertIsNone(self.store.meta(ARCHIVE_PREFIX + aid + ':1'))

    def test_teaching_request_is_saved_and_used_in_prompt(self):
        value = self.settings(teaching_request='每课先给一个思考问题，最后回到这个问题。')
        selected = prepare_learning(self.store, self.activity(), '保持连贯段落。')
        self.assertIn(value['teaching_request'], selected['prompt'])
        self.assertIn('保持连贯段落。', selected['prompt'])
        self.assertEqual(selected['manifest']['routine_settings'], value)

    def test_options_are_flat_public_metadata_and_exclude_foreign_main_history(self):
        with self.store.db() as db:
            before = db.total_changes
            options = source_options(db, self.store.today())
            self.assertEqual(before, db.total_changes)
        self.assertTrue(options)
        self.assertTrue(all(set(x) == {'category', 'id', 'title', 'source_title', 'scope'} for x in options))
        self.assertNotIn('magna-carta-1215', [x['id'] for x in options])
        self.settings(history='大宪章')
        with self.assertRaises(TopicUnavailable):
            prepare_learning(self.store, self.activity())

    def test_alternative_keywords_rotate_real_matching_sources(self):
        self.settings(classic='《孟子》、孙子兵法')
        a = prepare_learning(self.store, self.activity('2026-10-20'))['manifest']['cards'][0]
        b = prepare_learning(self.store, self.activity('2026-10-21'))['manifest']['cards'][0]
        self.assertEqual({a['id'], b['id']}, {'mencius-jinxin-3', 'sunzi-mougong'})

    def test_cached_catalog_and_night_study_material_are_selectable(self):
        baseline = prepare_learning(self.store, self.activity('2026-10-19'))['manifest']['cards']
        studied = copy.deepcopy(baseline[0])
        studied.update(id='night-study-' + 'a' * 32, title='阳明心学专题', source_title='传习录',
                       reviewed_on=None, verified_facts=[], excerpt='此隔离原文仅用于测试主题选材，不是正式史料。')
        studied['retrieval'] = {'mode': 'night_study_source', 'source_verified': False,
                               'fetched_at': self.store.stamp(), 'note_id': 'a' * 32,
                               'excerpt_sha256': hashlib.sha256(studied['excerpt'].encode()).hexdigest()}
        cached = copy.deepcopy(baseline[1])
        cached.update(id='catalog-test', title='史记·廉颇蔺相如列传', source_title='史记·廉颇蔺相如列传',
                      source_url=canonical_url('https://zh.wikisource.org/wiki/史記/卷081'), scope='二十四史',
                      reviewed_on=None, verified_facts=[], excerpt='此隔离原文仅用于测试主题选材，不是正式史料。')
        cached['retrieval'] = {'mode': 'automatic_source_text', 'source_verified': False,
                              'fetched_at': self.store.stamp(),
                              'excerpt_sha256': hashlib.sha256(cached['excerpt'].encode()).hexdigest()}
        self.settings(classic='传习录', history='廉颇')
        with patch('dsh_personal.study.learning_cards', return_value=[studied]), \
                patch('dsh_personal.learning.ready_history_cards', return_value=[cached]):
            selected = prepare_learning(self.store, self.activity())['manifest']
            self.assertEqual(selected['cards'][0]['id'], studied['id'])
            self.assertEqual(selected['cards'][1]['id'], cached['id'])
            self.assertEqual(prepare_learning(self.store, selected['activity_id'])['manifest'], selected)


    def test_cross_day_material_wait_resumes_worker_once_with_newly_available_source(self):
        # Keep actual Worker claim/prepare/submit transitions. Only the native
        # transport and the study catalogue are isolated in-process fixtures.
        base = prepare_learning(self.store, self.activity('2026-10-21'))['manifest']['cards'][0]
        self.settings(classic='阳明心学隔离新课')
        aid = self.activity()
        records = RecordsStore(self.store.path, lambda: self.now)

        class CaptureBridge:
            def __init__(self): self.submissions = []
            def ensure_session(self, name): return 'isolated-session-' + name
            def submit(self, *args, **kwargs): self.submissions.append((args, kwargs))

        bridge = CaptureBridge()
        worker = Worker(self.store, bridge, records, lambda: {})
        with self.store.db(True) as db:
            db.execute("UPDATE jobs SET status='done' WHERE target!=?", (aid,))
        with patch('dsh_personal.study.learning_cards', return_value=[]):
            self.assertTrue(worker.work_one())  # session
            self.assertTrue(worker.work_one())  # learning -> material_wait
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT status FROM jobs WHERE kind='learning' AND target=?", (aid,)).fetchone()[0], 'material_wait')
        self.assertEqual(bridge.submissions, [])
        self.assertIsNotNone(self.store.meta('learning_topic_gap:' + aid))

        original_day = self.store.today()
        self.now = self.now + dt.timedelta(days=1)
        available_day = self.store.today()
        # A separate current-day manual activity keeps this test concerned with
        # the old waiting job, rather than newly authorized daily preparation.
        self.activity(available_day)
        with self.store.db(True) as db:
            db.execute("UPDATE jobs SET status='done' WHERE target!=?", (aid,))
        new_card = copy.deepcopy(base)
        new_card.update(id='night-study-' + 'b' * 32, title='阳明心学隔离新课',
                        source_title='传习录：隔离新到原文', reviewed_on=None,
                        verified_facts=[], excerpt='此段隔离原文仅用于跨日期备课验证，不是正式史料。')
        new_card['retrieval'] = {'mode': 'night_study_source', 'source_verified': False,
                                 'fetched_at': self.store.stamp(), 'note_id': 'b' * 32,
                                 'excerpt_sha256': hashlib.sha256(new_card['excerpt'].encode()).hexdigest()}
        with patch('dsh_personal.study.learning_cards', side_effect=lambda db, day:
                   [new_card] if day >= available_day else []):
            tick(self.store)
            self.assertTrue(worker.work_one())
            self.assertEqual(len(bridge.submissions), 1)
            manifest = json.loads(self.store.meta(PREFIX + aid))
            self.assertEqual(manifest['business_date'], original_day)
            self.assertEqual(manifest['prepared_at'], self.store.stamp())
            self.assertEqual(manifest['source_available_through'], available_day)
            self.assertEqual(manifest['cards'][0]['id'], new_card['id'])
            self.assertIsNone(self.store.meta('learning_topic_gap:' + aid))
            self.assertIn(new_card['source_title'], bridge.submissions[0][0][2])
            self.now += dt.timedelta(minutes=2)
            tick(self.store)
            self.assertFalse(worker.work_one())
            self.assertEqual(len(bridge.submissions), 1)

    def test_future_waiting_activity_uses_same_availability_cutoff_as_preparation(self):
        self.now = self.now.replace(hour=22)
        tick(self.store)
        with self.store.db(True) as db:
            aid, day = db.execute("SELECT id,business_date FROM activities WHERE routine='A1'").fetchone()
            db.execute('INSERT INTO meta VALUES(?,?)', ('learning_topic_gap:' + aid,
                       dumps({'checked_at': (self.now - dt.timedelta(minutes=2)).isoformat()})))
        with patch('dsh_personal.learning.topic_availability', return_value=[{'category': 'classic', 'topic': '等待'}]) as available:
            tick(self.store)
            self.assertEqual(available.call_args.args[2], day)
        selected = prepare_learning(self.store, aid)['manifest']
        self.assertEqual(selected['source_available_through'], day)
        self.assertEqual(selected['business_date'], day)


if __name__ == '__main__':
    unittest.main()
