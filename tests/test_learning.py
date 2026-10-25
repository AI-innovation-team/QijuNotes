import concurrent.futures
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from dsh_personal.core import Store, TZ
from dsh_personal.learning import (PREFIX, learning_history, mark_learning_presented,
                                   learning_followup_rule, prepare_learning,
                                   prune_learning_history, source_catalog)


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = dt.datetime(2026, 10, 22, 23, tzinfo=TZ)
        self.s = Store(Path(self.tmp.name) / 'app.sqlite', lambda: self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def activity(self, day=22):
        return self.s.ensure_activity('A1', f'2026-10-{day:02}')['id']

    def native(self, aid, text, mid='native-fixture', at=None):
        with self.s.db(True) as c:
            c.execute('INSERT INTO messages VALUES(?,?,NULL,?,?,?,?,?,NULL,0)',
                      (mid, aid, 'assistant', text, at or self.s.stamp(), 'native', '42'))
        return mid

    def lesson_body(self, selected):
        manifest = selected['manifest']
        links = ['[' + c['title'] + '](' + c['source_url'] + ')' for c in manifest['cards']]
        if manifest.get('world_fun_fact'):
            links.append(manifest['world_fun_fact']['source_url'])
        return '\n'.join(links)

    def test_same_activity_concurrent_retry_and_restart_keep_snapshot(self):
        aid = self.activity()
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: prepare_learning(self.s, aid), range(12)))
        self.assertTrue(all(r == results[0] for r in results))
        restarted = Store(self.s.path, lambda: self.now + dt.timedelta(days=3))
        self.assertEqual(prepare_learning(restarted, aid), results[0])
        self.assertEqual(len(learning_history(restarted)), 1)

    def test_study_cards_extend_three_subjects_without_changing_prepared_lessons(self):
        import copy, hashlib
        old = prepare_learning(self.s, self.activity(20))
        cards = copy.deepcopy(old['manifest']['cards'])
        for i, card in enumerate(cards):
            card.update(id='studied-fixture-'+str(i), reviewed_on=None,
                        verified_facts=[], excerpt='用于隔离验证的原文章节，不代表真实用户材料。')
            card['retrieval'] = dict(mode='night_study_source', source_verified=False,
                fetched_at=self.s.stamp(), note_id=str(i)*32,
                excerpt_sha256=hashlib.sha256(card['excerpt'].encode()).hexdigest())
        with patch('dsh_personal.study.learning_cards', return_value=cards):
            selected = prepare_learning(self.s, self.activity(21))
            self.assertEqual([c['id'] for c in selected['manifest']['cards']], [c['id'] for c in cards])
            self.assertEqual(prepare_learning(self.s, self.activity(20)), old)
            self.assertEqual(prepare_learning(self.s, self.activity(21)), selected)
        self.assertEqual(prepare_learning(self.s, self.activity(21)), selected)

    def test_four_groups_cover_full_scope_and_each_has_three_categories(self):
        selected = [prepare_learning(self.s, self.activity(day))['manifest'] for day in range(18, 22)]
        self.assertEqual(len({m['pack_index'] for m in selected}), 4)
        for manifest in selected:
            self.assertEqual({c['category'] for c in manifest['cards']}, {'classic', 'history', 'modern'})
            self.assertTrue(all(c['reviewed_on'] and c['source_url'].startswith('https://') for c in manifest['cards']))
        self.assertEqual({m['cards'][2]['scope'] for m in selected}, {'政治', '经济', '金融', '货币'})
        self.assertEqual({m['cards'][0]['scope'] for m in selected}, {'四书', '五经', '法家', '兵法'})

    def test_new_history_main_lessons_are_chinese_and_cpc_with_separate_short_foreign_fact(self):
        selected = [prepare_learning(self.s, self.activity(day))['manifest'] for day in range(18, 22)]
        history_scopes = [next(c['scope'] for c in m['cards'] if c['category'] == 'history') for m in selected]
        self.assertEqual(history_scopes.count('党史'), 2)
        self.assertTrue(all(s in ('中国史', '二十四史', '党史') for s in history_scopes))
        for manifest in selected:
            self.assertEqual(manifest['version'], 2)
            self.assertEqual(manifest['world_fun_fact']['sentence'].count('。'), 1)
            self.assertLess(len(manifest['world_fun_fact']['sentence']), 100)
            self.assertNotIn(manifest['world_fun_fact']['id'], [c['id'] for c in manifest['cards']])
            for card in manifest['cards']:
                self.assertTrue(card['teaching_plan']['objective'])
                self.assertGreaterEqual(len(card['teaching_plan']['explanation_steps']), 2)
                self.assertTrue(card['teaching_plan']['worked_example'])
                self.assertTrue(card['teaching_plan']['common_misconception'])
                self.assertTrue(card['teaching_plan']['optional_check'])

    def test_all_new_sources_including_foreign_fact_required_for_delivery(self):
        aid = self.activity()
        picked = prepare_learning(self.s, aid)
        only_main = '\n'.join(c['source_url'] for c in picked['manifest']['cards'])
        incomplete = self.native(aid, only_main, 'missing-world-source')
        self.assertFalse(mark_learning_presented(self.s, aid, incomplete))
        self.assertIsNone(learning_history(self.s)[0]['delivery'])
        complete = self.native(aid, self.lesson_body(picked), 'all-four-sources')
        self.assertTrue(mark_learning_presented(self.s, aid, complete))

    def legacy_snapshot(self, aid):
        manifest = prepare_learning(self.s, aid)['manifest']
        by_id = {c['id']: c for c in source_catalog()}
        manifest['cards'] = [by_id[ident] for ident in
                             ('mencius-jinxin-3', 'magna-carta-1215', 'fed-reserve-requirements-2020')]
        for card in manifest['cards']:
            card.update(review=False, first_presented_business_date=None,
                        first_presented_at=None, first_presented_activity_id=None)
        manifest['version'] = 1
        manifest.pop('world_fun_fact')
        manifest.pop('teaching_policy')
        raw = json.dumps(manifest, ensure_ascii=False)
        self.s.set_meta(PREFIX + aid, raw)
        return manifest, raw

    def test_legacy_source_snapshot_remains_fixed_and_delivery_uses_its_own_contract(self):
        aid = self.activity(15)
        original, raw = self.legacy_snapshot(aid)
        picked = prepare_learning(self.s, aid)
        self.assertEqual(picked['manifest'], original)
        self.assertEqual(self.s.meta(PREFIX + aid), raw)
        self.assertIn('magna-carta-1215', [c['id'] for c in picked['manifest']['cards']])
        self.assertNotIn('world_fun_fact', picked['manifest'])
        complete_old = self.native(aid, self.lesson_body(picked), 'legacy-three-sources')
        self.assertTrue(mark_learning_presented(self.s, aid, complete_old))
        # Legacy pack index 0 did not teach the v2 Chinese-history pack 0.
        new = prepare_learning(self.s, self.activity(16))['manifest']
        self.assertEqual(new['pack_index'], 0)
        self.assertEqual(new['cards'][1]['id'], 'qin-two-edicts-bronze')
        self.assertIn('旧版固定来源', learning_followup_rule(original))

    def test_new_manifest_corruption_never_silently_reverts_to_legacy_or_reselects(self):
        aid = self.activity()
        manifest = prepare_learning(self.s, aid)['manifest']
        manifest['cards'][1]['scope'] = '世界史'
        raw = json.dumps(manifest, ensure_ascii=False)
        self.s.set_meta(PREFIX + aid, raw)
        with self.assertRaisesRegex(ValueError, '保留原记录'):
            prepare_learning(self.s, aid)
        self.assertEqual(self.s.meta(PREFIX + aid), raw)

    def test_teaching_notes_are_saved_with_sources_not_recomputed_for_old_activity(self):
        aid = self.activity()
        first = prepare_learning(self.s, aid)
        with patch('dsh_personal.learning._TEACHING', {}):
            retry = prepare_learning(self.s, aid)
        self.assertEqual(retry, first)
        retry['manifest']['cards'][0]['teaching_plan']['explanation_steps'].append('LOCAL MUTATION')
        self.assertNotIn('LOCAL MUTATION', self.s.meta(PREFIX + aid))

    def test_review_uses_actual_delivery_and_original_backfill_date(self):
        first = self.activity(15)
        picked = prepare_learning(self.s, first)
        message = self.native(first, self.lesson_body(picked))
        self.assertTrue(mark_learning_presented(self.s, first, message))
        self.assertTrue(mark_learning_presented(self.s, first, message))
        for day in range(16, 19):
            prepare_learning(self.s, self.activity(day))
        fifth = prepare_learning(self.s, self.activity(19))
        self.assertTrue(all(c['review'] for c in fifth['manifest']['cards']))
        self.assertTrue(all(c['first_presented_business_date'] == '2026-10-15' for c in fifth['manifest']['cards']))
        self.assertIn('复习；首次生成归属日：2026-10-15', fifth['prompt'])
        self.assertIn('不等于用户已读或掌握', fifth['prompt'])

    def test_failed_preparation_is_not_reading_or_review(self):
        for day in range(15, 19):
            prepare_learning(self.s, self.activity(day))
        fifth = prepare_learning(self.s, self.activity(19))['manifest']
        self.assertFalse(any(c['review'] for c in fifth['cards']))
        self.assertIsNone(fifth['delivery'])

    def test_preamble_partial_sources_and_old_message_do_not_mark_presented(self):
        aid = self.activity()
        selected = prepare_learning(self.s, aid)
        for i, text in enumerate(['我正在准备材料', selected['manifest']['cards'][0]['source_url']]):
            mid = self.native(aid, text, 'preamble-' + str(i))
            self.assertFalse(mark_learning_presented(self.s, aid, mid))
        old = self.native(aid, self.lesson_body(selected), 'old', (self.now - dt.timedelta(minutes=1)).isoformat())
        self.assertFalse(mark_learning_presented(self.s, aid, old))
        self.assertIsNone(learning_history(self.s)[0]['delivery'])
        encoded = quote(self.lesson_body(selected), safe=':/()[]\n')
        good = self.native(aid, encoded, 'actual')
        self.assertTrue(mark_learning_presented(self.s, aid, good))

    def test_cannot_attribute_user_placeholder_unrelated_or_legacy_message(self):
        aid = self.activity()
        prepare_learning(self.s, aid)
        placeholder = self.s.activity(aid)['messages'][0]['id']
        with self.assertRaises(ValueError):
            mark_learning_presented(self.s, aid, placeholder)
        other = self.activity(21)
        foreign = self.native(other, 'foreign')
        with self.assertRaises(ValueError):
            mark_learning_presented(self.s, aid, foreign)
        self.assertFalse(mark_learning_presented(self.s, other, foreign))
        with self.assertRaises(ValueError):
            prepare_learning(self.s, self.s.ensure_activity('A2')['id'])

    def test_corrupt_history_fails_closed_and_does_not_overwrite(self):
        aid = self.activity()
        self.s.set_meta(PREFIX + aid, '{bad')
        with self.assertRaisesRegex(ValueError, '保留原记录'):
            prepare_learning(self.s, aid)
        with self.assertRaises(ValueError):
            prepare_learning(self.s, self.activity(21))
        self.assertEqual(self.s.meta(PREFIX + aid), '{bad')

    def test_public_snapshot_is_offline_and_does_not_collect_private_text(self):
        aid = self.activity()
        self.s.submit(aid, 'private-user-submission', 'PRIVATE-USER-MARKER')
        self.s.set_meta('unrelated_setting', 'keep')
        with patch('urllib.request.urlopen', side_effect=AssertionError('unexpected network')):
            selected = prepare_learning(self.s, aid)
        self.assertNotIn('PRIVATE-USER-MARKER', json.dumps(selected, ensure_ascii=False))
        self.assertIn('不能声称刚刚联网核实', selected['prompt'])
        self.assertIn('不自行承诺调度或生成次数', selected['prompt'])
        self.assertEqual(self.s.meta('unrelated_setting'), 'keep')
        source_catalog()[0]['verified_facts'].append('MUTATED')
        self.assertNotIn('MUTATED', json.dumps(source_catalog()))

    def test_source_deletion_invalidates_cached_future_review_and_delivery(self):
        from dsh_personal.deletion import apply_message_deletion
        first = self.activity(15)
        selected = prepare_learning(self.s, first)
        native = self.native(first, self.lesson_body(selected))
        mark_learning_presented(self.s, first, native)
        self.s.submit(first, 'original-for-deletion', 'private original')
        with self.s.db() as c:
            original = c.execute("SELECT id FROM messages WHERE submission_id='original-for-deletion'").fetchone()[0]
        for day in range(16, 19):
            prepare_learning(self.s, self.activity(day))
        review_id = self.activity(19)
        self.assertTrue(prepare_learning(self.s, review_id)['manifest']['cards'][0]['review'])
        with self.s.db(True) as c:
            apply_message_deletion(c, original, self.s.stamp())
            prune_learning_history(c)
        self.assertIsNone(json.loads(self.s.meta(PREFIX + first))['delivery'])
        retried = prepare_learning(self.s, review_id)['manifest']
        self.assertFalse(any(c['review'] for c in retried['cards']))
        self.assertTrue(all(c['first_presented_business_date'] is None for c in retried['cards']))
        self.assertEqual(retried['pack_index'], selected['manifest']['pack_index'])
        self.assertNotIn('private original', self.s.meta(PREFIX + first))

    def test_old_snapshot_with_new_tombstone_cannot_resurrect_review(self):
        first = self.activity(15)
        selected = prepare_learning(self.s, first)
        native = self.native(first, self.lesson_body(selected))
        mark_learning_presented(self.s, first, native)
        with self.s.db(True) as c:
            c.execute('INSERT INTO tombstones VALUES(?,?,?,?,?)',
                      ('ledger-tombstone', 'derived_message', native, None, self.s.stamp()))
        # Simulate old content still present before physical deletion replay.
        with self.s.db() as c:
            self.assertEqual(c.execute('SELECT deleted FROM messages WHERE id=?', (native,)).fetchone()[0], 0)
        self.assertIsNone(learning_history(self.s)[0]['delivery'])
        self.assertIsNone(json.loads(self.s.meta(PREFIX + first))['delivery'])
        self.assertFalse(mark_learning_presented(self.s, first, native))


if __name__ == '__main__':
    unittest.main()
