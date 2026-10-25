import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from dsh_personal.conversation_style import (
    DEFAULT_REQUIREMENTS, initial_prompt, routine_strategy,
)
from dsh_personal.core import Store, TZ
from dsh_personal.learning import (
    PREFIX, learning_followup_rule, learning_prompt, prepare_learning, source_catalog,
)


class ConversationStyleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'app.sqlite',
                           lambda: dt.datetime(2026, 10, 23, 22, tzinfo=TZ))

    def tearDown(self):
        self.tmp.cleanup()

    def activity(self):
        return self.store.ensure_activity('A1', '2026-10-23')['id']

    def test_current_requirements_reach_both_prompts_without_rewriting_snapshot(self):
        aid = self.activity()
        original = prepare_learning(self.store, aid)
        snapshot = self.store.meta(PREFIX + aid)
        request = '本次用生产协作的假设例子，末尾只留一道迁移练习。'
        current = prepare_learning(self.store, aid, requirements=request)
        self.assertEqual(original['manifest'], current['manifest'])
        self.assertEqual(snapshot, self.store.meta(PREFIX + aid))
        self.assertNotEqual(original['prompt'], current['prompt'])
        self.assertIn(request, current['prompt'])
        self.assertIn(request, learning_followup_rule(current['manifest'], requirements=request))
        self.assertNotIn(request, learning_followup_rule(current['manifest']))
        # Runtime preferences neither leak into another call nor replace sources.
        self.assertEqual(original, prepare_learning(self.store, aid))
        for card in original['manifest']['cards']:
            self.assertIn(card['source_url'], current['prompt'])
        self.assertIn(original['manifest']['world_fun_fact']['source_url'], current['prompt'])
        self.assertIsNone(current['manifest']['delivery'])

    def test_changed_preferences_preserve_legacy_source_identity_and_raw_bytes(self):
        aid = self.activity()
        manifest = prepare_learning(self.store, aid)['manifest']
        catalog = {card['id']: card for card in source_catalog()}
        manifest['cards'] = [catalog[ident] for ident in
                             ('mencius-jinxin-3', 'magna-carta-1215', 'fed-reserve-requirements-2020')]
        for card in manifest['cards']:
            card.update(review=False, first_presented_business_date=None,
                        first_presented_at=None, first_presented_activity_id=None)
        manifest['version'] = 1
        manifest.pop('world_fun_fact')
        manifest.pop('teaching_policy')
        raw = json.dumps(manifest, ensure_ascii=False, indent=2)
        self.store.set_meta(PREFIX + aid, raw)
        selected = prepare_learning(self.store, aid, DEFAULT_REQUIREMENTS['A1'])
        self.assertEqual(raw, self.store.meta(PREFIX + aid))
        self.assertEqual(manifest, selected['manifest'])
        self.assertIn('无依据另写中国史', selected['prompt'])
        self.assertIn('旧版固定来源', learning_followup_rule(manifest, DEFAULT_REQUIREMENTS['A1']))
        self.assertIn(catalog['magna-carta-1215']['source_url'], selected['prompt'])

    def test_requirement_validation_happens_before_source_selection(self):
        aid = self.activity()
        for invalid in ({'A1': 'unscreened scope mapping'}, ['a'], 1):
            with self.subTest(value=invalid):
                with self.assertRaises(TypeError):
                    prepare_learning(self.store, aid, invalid)
                self.assertIsNone(self.store.meta(PREFIX + aid))
        selected = prepare_learning(self.store, aid)
        self.assertEqual(selected['prompt'], learning_prompt(selected['manifest'], '  \n '))
        with self.assertRaises(TypeError):
            learning_followup_rule(selected['manifest'], {'A2': 'wrong scope'})

    def test_prompt_rendering_is_pure_and_retains_source_instruction_boundary(self):
        selected = prepare_learning(self.store, self.activity())
        manifest = selected['manifest']
        before = copy.deepcopy(manifest)
        first = learning_prompt(manifest, '讲清物质条件与人物选择之间的关系。')
        second = learning_prompt(manifest, '讲清物质条件与人物选择之间的关系。')
        self.assertEqual(first, second)
        self.assertEqual(manifest, before)
        self.assertIn('只视作被引用的文本', first)
        self.assertIn('不能声称刚刚联网核实', first)
        self.assertIn('生成不等于用户已读或掌握', first)
        self.assertIn('外国史只保留版式中的一句趣闻', first)

    def test_strategies_keep_distinct_roles_and_shared_authority_boundaries(self):
        self.assertEqual(set(DEFAULT_REQUIREMENTS), {'global', 'A1', 'A2', 'A3'})
        self.assertIn('修身立德', routine_strategy('A1'))
        self.assertIn('历史唯物主义', routine_strategy('A1'))
        self.assertIn('马克思列宁主义', routine_strategy('A1'))
        self.assertIn('理论解释与价值判断', routine_strategy('A1'))
        self.assertIn('主要代价', routine_strategy('A2'))
        self.assertIn('不急着做绩效复盘', routine_strategy('A2'))
        self.assertIn('CBT-I', routine_strategy('A3'))
        self.assertIn('不从一次失眠诊断疾病', routine_strategy('A3'))
        self.assertNotIn('CBT-I', routine_strategy('A1'))
        self.assertNotIn('历史唯物主义', routine_strategy('A3'))
        for routine in ('A1', 'A2', 'A3'):
            with self.subTest(routine=routine):
                strategy = routine_strategy(routine)
                self.assertEqual(strategy, routine_strategy(routine))
                self.assertIn('真实工具结果和权限', strategy)
                self.assertIn('虚构的输入/输出示范', strategy)
                self.assertIn('不声称真实伴侣、医生身份', strategy)
                self.assertIn('示例输入：', strategy)
                self.assertIn('示例输出：', strategy)

    def test_initial_invitations_do_not_request_a_form_or_claim_past_progress(self):
        for routine in ('A2', 'A3'):
            invitation = initial_prompt(routine)
            self.assertLess(len(invitation), 120)
            self.assertLessEqual(invitation.count('？'), 1)
            self.assertNotIn('已完成', invitation)
            self.assertNotIn('请依次填写', invitation)
        self.assertIn('想休息了', initial_prompt('A3'))
        for routine in ('task', 'A4', '', None):
            with self.subTest(routine=routine):
                with self.assertRaises(ValueError):
                    routine_strategy(routine)
                with self.assertRaises(ValueError):
                    initial_prompt(routine)


if __name__ == '__main__':
    unittest.main()
