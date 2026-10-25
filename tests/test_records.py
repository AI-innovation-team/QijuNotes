import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from dsh_personal.records import RecordsStore, LINES


NOW = datetime(2026, 10, 22, 15, 30, tzinfo=timezone.utc)


def chatgpt_export():
    return json.dumps([{"id": "conversation-test", "title": "测试聊天", "mapping": {
        "root": {"message": None, "parent": None},
        "u": {"parent": "root", "children": ["a", "alternate"], "message": {"id": "msg-u", "author": {"role": "user"}, "create_time": 1792600000, "content": {"content_type": "text", "parts": ["博士实验方案"]}}},
        "a": {"parent": "u", "message": {"id": "msg-a", "author": {"role": "assistant"}, "create_time": 1792600001, "content": {"content_type": "text", "parts": ["建议先检查重复测量"]}}},
        "alternate": {"parent": "u", "message": {"id": "msg-alt", "author": {"role": "assistant"}, "create_time": 1792600002, "content": {"content_type": "multimodal_text", "parts": ["另一分支", {"content_type": "image_asset_pointer", "asset_pointer": "file-service://asset-test"}]}}}
    }}], ensure_ascii=False)


class RecordsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "records.db")
        self.store = RecordsStore(self.path, clock=lambda: NOW)

    def import_one(self):
        return self.store.import_apply("kimi", json.dumps({"id": "kimi-chat", "messages": [{"id": "m1", "role": "user", "content": "我研究视觉编码", "created_at": "2026-10-20T10:20:00+08:00"}]}), "kimi.json")

    def test_chatgpt_branches_timestamps_and_missing_attachment(self):
        content = chatgpt_export()
        preview = self.store.import_preview("chatgpt", content, "conversations.json")
        self.assertEqual(preview["counts"]["new"], 3)
        self.assertEqual(len(preview["missing_attachments"]), 1)
        self.assertFalse(self.store.imported_messages())
        applied = self.store.import_apply("chatgpt", content, "conversations.json", preview_token=preview["preview_token"])
        self.assertEqual(applied["counts"]["new"], 3)
        rows = self.store.imported_messages()
        self.assertEqual({r["source_id"] for r in rows}, {"msg-u", "msg-a", "msg-alt"})
        self.assertEqual(next(r for r in rows if r["source_id"] == "msg-alt")["metadata"]["parent"], "u")
        self.assertTrue(all(r["original_at"] != r["imported_at"] for r in rows))

    def test_codex_jsonl_real_schema_tools_and_repeated_events(self):
        rows = [
            {"timestamp": "2026-10-20T00:00:00Z", "type": "session_meta", "payload": {"id": "codex-session"}},
            {"timestamp": "2026-10-20T00:00:01Z", "type": "event_msg", "payload": {"type": "user_message", "message": "请核查样本"}},
            {"timestamp": "2026-10-20T00:00:01Z", "type": "response_item", "payload": {"type": "message", "id": "u1", "role": "user", "content": [{"type": "input_text", "text": "请核查样本"}]}},
            {"timestamp": "2026-10-20T00:00:02Z", "type": "response_item", "payload": {"type": "custom_tool_call", "id": "call1", "name": "test", "input": "count", "call_id": "c1"}},
            {"timestamp": "2026-10-20T00:00:03Z", "type": "response_item", "payload": {"type": "custom_tool_call_output", "id": "out1", "output": "12", "call_id": "c1"}},
            {"timestamp": "2026-10-20T00:00:04Z", "type": "response_item", "payload": {"type": "reasoning", "encrypted_content": "not-visible"}},
            {"timestamp": "2026-10-20T00:00:05Z", "type": "response_item", "payload": {"type": "message", "id": "a1", "role": "assistant", "content": [{"type": "output_text", "text": "有12个样本"}]}},
            {"timestamp": "2026-10-20T00:00:06Z", "type": "response_item", "payload": {"type": "message", "id": "u2", "role": "user", "content": [{"type": "input_text", "text": "请核查样本"}]}},
        ]
        content = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\nnot-json"
        result = self.store.import_apply("codex", content, "rollout.jsonl")
        self.assertEqual(result["counts"]["new"], 5)
        self.assertEqual(len(result["failures"]), 1)
        messages = self.store.imported_messages()
        self.assertEqual(sum(m["role"] == "user" for m in messages), 2)
        self.assertFalse(any("not-visible" in m["body"] for m in messages))
        self.assertEqual(next(m for m in messages if m["role"] == "tool")["metadata"]["call_id"], "c1")

    def test_kimi_json_markdown_and_ambiguous_dates(self):
        result = self.import_one()
        self.assertEqual(result["counts"]["new"], 1)
        result = self.store.import_apply("kimi", "# 用户\n睡得一般\n# Kimi\n已记录", "chat.md")
        self.assertEqual(result["counts"]["new"], 2)
        rows = self.store.imported_messages()
        self.assertEqual(next(r for r in rows if r["source_id"] == "m1")["original_date"], "2026-10-20")
        self.assertTrue(all(r["original_at"] is None for r in rows if r["title"] == "chat.md"))
        ambiguous = self.store.import_preview("kimi", json.dumps({"messages": [{"role": "user", "content": "无时区", "timestamp": "2026-10-20 10:00:00"}]}), "unknown.json")
        self.assertEqual(ambiguous["date_range"], [None, None])
        self.assertTrue(ambiguous["warnings"])

    def test_memory_and_instructions_do_not_change_context(self):
        with self.assertRaises(ValueError):
            self.store.import_apply("codex", "Always do X", "AGENTS.md", kind="instructions", scope="global")
        self.store.import_apply("codex", "Always do X", "AGENTS.md", kind="instructions", scope="project:study")
        self.store.import_apply("chatgpt", "我更喜欢简洁回答", "memory.txt", kind="memory", scope="global")
        self.assertFalse(self.store.context())
        self.assertEqual({r["kind"] for r in self.store.imported_messages()}, {"instructions", "memory"})

    def test_duplicate_concurrency_and_batch_removal(self):
        args = ("chatgpt", chatgpt_export(), "conversations.json")
        with ThreadPoolExecutor(max_workers=2) as pool:
            reports = list(pool.map(lambda _: self.store.import_apply(*args), range(2)))
        self.assertEqual(sum(r["counts"]["new"] for r in reports), 3)
        duplicate = next(r for r in reports if r["counts"]["new"] == 0)
        self.assertEqual(self.store.remove_batch(duplicate["batch_id"])["removed_new_messages"], 0)
        self.assertEqual(len(self.store.imported_messages()), 3)
        first = next(r for r in reports if r["counts"]["new"] == 3)
        self.assertEqual(self.store.remove_batch(first["batch_id"])["removed_new_messages"], 3)
        self.assertFalse(self.store.imported_messages())
        self.assertEqual(self.store.import_preview(*args)["counts"]["deleted"], 3)

    def test_preview_token_is_bound_to_input_and_scope(self):
        p = self.store.import_preview("kimi", "测试", "a.md")
        with self.assertRaises(ValueError):
            self.store.import_apply("kimi", "不同内容", "a.md", preview_token=p["preview_token"])
        self.assertFalse(self.store.list_batches())

    def test_scoped_profile_revision_confirmation_and_expiry(self):
        p = self.store.add_profile("视觉研究者", "background", source_refs=["activity:a1"], confirmed=True, user_input="记住我是视觉研究者")
        self.assertEqual(self.store.context()[0]["content"], "视觉研究者")
        revised = self.store.revise_profile(p["item_id"], 1, "更正为 IT 电生理研究", content="IT 电生理研究")
        with self.assertRaises(ValueError):
            self.store.revise_profile(p["item_id"], 1, "旧页面提交", content="旧值")
        self.assertEqual(self.store.context()[0]["content"], "IT 电生理研究")
        self.store.add_profile("使用隔离测试数据", "project_rule", "project:neural", confirmed=True, user_input="仅这个项目使用")
        self.assertEqual(len(self.store.context()), 1)
        self.assertEqual(len(self.store.context("project:neural")), 2)
        self.store.add_profile("已过期目标", "stage_goal", confirmed=True, user_input="旧目标", valid_until="2026-10-01")
        self.assertEqual(len(self.store.context()), 1)
        self.store.disable_profile(p["item_id"], revised["version"], "不要作为长期偏好")
        self.assertFalse(self.store.context())
        self.assertEqual(len(self.store.list_profile(include_inactive=True, history=True)), 5)
        with self.assertRaises(ValueError):
            self.store.add_profile("推测能力提升", "hypothesis", confirmed=True, user_input="ok")

    def test_deletion_scrubs_derived_versions_and_prevents_reimport(self):
        self.import_one()
        msg = self.store.imported_messages()[0]
        p = self.store.add_profile("视觉编码研究", source_refs=["import:" + msg["message_id"]])
        self.store.revise_profile(p["item_id"], 1, "更正", content="视觉研究")
        g = self.store.put_growth("doctorate", "goal", "研究视觉", source_refs=[msg["message_id"]])
        self.store.put_growth("doctorate", "plan", "安排实验", parent_id=g["item_id"])
        self.store.delete_imported(msg["message_id"])
        self.assertFalse(self.store.list_profile(include_inactive=True, history=True))
        self.assertFalse(self.store.growth()["items"])
        self.assertFalse(self.store.imported_messages(query="视觉"))
        ledger = self.store.export_ledger()
        self.assertNotIn("视觉", json.dumps(ledger, ensure_ascii=False))
        self.assertEqual(self.import_one()["counts"]["deleted"], 1)
        with self.assertRaises(ValueError):
            self.store.add_profile("复活", source_refs=[msg["message_id"]])

    def test_general_source_deletion_removes_own_versions_and_import_body(self):
        profile = self.store.add_profile("SYNTHETIC-SELF-PROFILE", confirmed=True, user_input="confirmed")
        self.store.revise_profile(profile["item_id"], 1, "SYNTHETIC-SELF-PROFILE edit", content="SYNTHETIC-SELF-PROFILE v2")
        child = self.store.put_growth("doctorate", "goal", "SYNTHETIC-SELF-PROFILE child", source_refs=["profile:" + profile["item_id"]])
        self.store.delete_source("profile:" + profile["item_id"])
        self.assertFalse(self.store.context())
        self.assertNotIn("SYNTHETIC-SELF-PROFILE", json.dumps(self.store.export_data()))
        with self.assertRaises(KeyError):
            self.store.get_growth(child["item_id"])
        self.import_one()
        imported = self.store.imported_messages()[0]
        self.store.delete_source("import:" + imported["message_id"])
        self.store.delete_source("import:" + imported["message_id"])
        self.assertFalse(self.store.imported_messages(include_quarantined=True))
        self.assertEqual(self.import_one()["counts"]["deleted"], 1)
        tomb = next(t for t in self.store.export_ledger()["tombstones"] if t["object_id"] == imported["message_id"])
        self.assertEqual(tomb["fingerprint"], imported["fingerprint"])

    def test_dependent_refs_is_read_only_transitive_and_includes_old_versions(self):
        profile = self.store.add_profile("derived", source_refs=["message:source"])
        self.store.revise_profile(profile["item_id"], 1, "changed sources", source_refs=["message:other"])
        goal = self.store.put_growth("doctorate", "goal", "child", source_refs=["profile:" + profile["item_id"]])
        plan = self.store.put_growth("doctorate", "plan", "grandchild", parent_id=goal["item_id"])
        before = self.store.export_data()
        expected = {"message:source", "profile:" + profile["item_id"], "growth:" + goal["item_id"], "growth:" + plan["item_id"]}
        self.assertEqual(set(self.store.dependent_refs(["message:source"])), expected)
        self.assertEqual(self.store.export_data(), before)
        self.assertEqual(set(self.store.delete_source("message:source")["affected_refs"]), expected)
        self.assertFalse(self.store.growth(include_history=True)["versions"])

    def test_derivative_invalidation_preserves_activity_and_task_for_future_input(self):
        self.store.add_profile("old derived", source_refs=["activity:continuing"])
        self.store.put_growth("doctorate", "outcome", "old task result", source_refs=["task:continuing"])
        self.store.sync_activity("continuing", "A2", "2026-10-22", ["message:old"], 1)
        self.store.delete_source("activity:continuing", preserve_source=True)
        self.store.delete_source("task:continuing", preserve_source=True)
        self.assertFalse(self.store.list_profile());self.assertFalse(self.store.growth()["items"])
        self.assertTrue(self.store.sync_activity("continuing", "A2", "2026-10-22", ["message:new"], 2))
        self.store.add_profile("new independent derived", source_refs=["activity:continuing"])
        self.store.put_growth("doctorate", "outcome", "new task result", source_refs=["task:continuing"])
        self.assertEqual(self.store.activity_evidence()[0]["source_refs"], ["message:new"])

    def test_old_growth_with_deleted_parent_does_not_revive_during_import(self):
        parent = self.store.put_growth("doctorate", "goal", "parent")
        child = self.store.put_growth("doctorate", "plan", "SYNTHETIC-CHILD", parent_id=parent["item_id"])
        snapshot = self.store.export_data()
        self.store.delete_source("growth:" + parent["item_id"])
        latest = self.store.export_ledger()
        # A newer ledger from another writer may know only the deleted parent.
        latest["tombstones"] = [t for t in latest["tombstones"] if t["object_id"] == parent["item_id"]]
        restored = RecordsStore(Path(self.temp.name) / "restored-parent.db", clock=lambda: NOW)
        restored.import_data(snapshot, latest)
        self.assertFalse(restored.growth(include_history=True)["versions"])
        self.assertNotIn("SYNTHETIC-CHILD", restored.render_markdown())

    def test_deleted_import_attachment_diagnostics_do_not_survive_or_restore(self):
        marker = "SYNTHETIC-IMPORT-ATTACHMENT"
        content = json.dumps({"id": "attachment-deletion", "messages": [{"id": "u", "role": "user", "content": marker,
                    "attachments": [{"url": "https://example.invalid/" + marker}]}]})
        self.store.import_apply("kimi", content, "fixture.json")
        snapshot = self.store.export_data()
        self.store.delete_imported(self.store.imported_messages()[0]["message_id"])
        latest = self.store.export_ledger()
        self.assertNotIn(marker, json.dumps(self.store.export_data()))
        self.store.import_apply("kimi", content, "fixture.json")
        self.assertNotIn(marker, json.dumps(self.store.export_data()))
        restored = RecordsStore(Path(self.temp.name) / "restored-attachment.db", clock=lambda: NOW)
        restored.import_data(snapshot, latest)
        self.assertNotIn(marker, json.dumps(restored.export_data()))

    def test_import_deletion_invalidates_model_quotes_but_preserves_other_users_and_conversations(self):
        source = json.dumps([{"id": "same-conversation", "messages": [
            {"id": "delete-user", "role": "user", "content": "SYNTHETIC-DELETE-QUOTE"},
            {"id": "derived", "role": "assistant", "content": "quote SYNTHETIC-DELETE-QUOTE"},
            {"id": "tool", "role": "tool", "content": "tool SYNTHETIC-DELETE-QUOTE"},
            {"id": "keep-user", "role": "user", "content": "Independent original"}]},
            {"id": "other-conversation", "messages": [{"id": "keep-assistant", "role": "assistant", "content": "Other conversation"}]}])
        self.store.import_apply("kimi", source, "fixture.json")
        snapshot = self.store.export_data()
        selected = next(m for m in self.store.imported_messages() if m["source_id"] == "delete-user")
        self.store.delete_imported(selected["message_id"])
        self.assertEqual({m["source_id"] for m in self.store.imported_messages()}, {"keep-user", "keep-assistant"})
        self.assertNotIn("SYNTHETIC-DELETE-QUOTE", json.dumps(self.store.export_data()))
        self.assertEqual(self.store.import_apply("kimi", source, "fixture.json")["counts"]["deleted"], 3)
        restored = RecordsStore(Path(self.temp.name) / "restore-import-quotes.db", clock=lambda: NOW)
        restored.import_data(snapshot, self.store.export_ledger())
        self.assertEqual({m["source_id"] for m in restored.imported_messages(include_quarantined=True)}, {"keep-user", "keep-assistant"})

    def test_each_growth_line_full_versioned_loop_and_no_auto_tasks(self):
        for line in LINES:
            with self.subTest(line=line):
                goal = self.store.put_growth(line, "goal", {"target": "测试目标", "fixture": True})
                capability = self.store.put_growth(line, "capability", {"name": "待验证能力", "judgment": "pending", "next_verification": "实验"}, source_refs=["message:fixture"], parent_id=goal["item_id"])
                plan = self.store.put_growth(line, "plan", {"goal": "测试目标", "constraints": "测试约束", "next_action": "执行测试", "expected_product": "测试成果", "review_date": "2026-11-01"}, parent_id=goal["item_id"])
                self.assertEqual(plan["content"]["horizon_days"], 90)
                action = self.store.put_growth(line, "action", {"status": "done", "fixture": True}, parent_id=goal["item_id"], source_refs=[plan["item_id"]])
                outcome = self.store.put_growth(line, "outcome", {"artifact": "fixture://result"}, parent_id=goal["item_id"], source_refs=[action["item_id"]])
                self.store.put_growth(line, "evaluation", {"assessment": "仅测试夹具评价"}, source_refs=[outcome["item_id"]], parent_id=goal["item_id"], user_input="测试用户：需要扩大样本")
                self.store.put_growth(line, "review", {"open_question": "测试中需要更多样本", "adjustment": "测试扩大样本"}, parent_id=goal["item_id"], source_refs=[outcome["item_id"], capability["item_id"]])
                revised = self.store.put_growth(line, "plan", {"next_action": "增加测试样本"}, parent_id=goal["item_id"], item_id=plan["item_id"], expected_version=1, user_input="测试用户确认修改计划")
                self.assertEqual(revised["version"], 2)
                view = self.store.growth(line, include_history=True)
                self.assertFalse(view["missing_steps"])
                self.assertEqual(len(view["items"]), 7)
                self.assertEqual(len(view["versions"]), 8)
                self.assertIsNone(view["current_state"])
        with self.store._db() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse(any("task" in x for x in tables))

    def test_growth_user_state_and_cross_line_validation(self):
        goal = self.store.put_growth("doctorate", "goal", "测试目标")
        with self.assertRaises(ValueError):
            self.store.put_growth("startup", "action", "跨主线错误", parent_id=goal["item_id"])
        with self.assertRaises(ValueError):
            self.store.put_growth("culture", "current_state", "未投入")
        state = self.store.put_growth("culture", "current_state", "本周暂未投入", user_input="本周我没有投入文化主线")
        self.assertEqual(self.store.growth("culture")["current_state"]["item_id"], state["item_id"])
        self.assertTrue(self.store.growth("culture")["missing_steps"])
        self.assertIsNone(self.store.growth("startup")["current_state"])

    def test_historical_business_date_updates_selected_week(self):
        self.store.put_growth("doctorate", "outcome", "补记上周成果", source_refs=["activity:historical"], business_date="2026-10-14")
        previous = self.store.weekly_review("2026-10-14")
        self.assertEqual(previous["start"], "2026-10-12")
        self.assertEqual(previous["label"], "上一完整周")
        self.assertEqual(len(previous["lines"]["doctorate"]), 1)
        self.assertEqual(self.store.weekly_review()["label"], "本周截至目前")
        self.assertFalse(self.store.weekly_review()["lines"]["doctorate"])
        self.assertIsNone(previous["automatic_judgment"])

    def test_clean_restore_new_ledger_deletes_old_text_and_keeps_correction(self):
        self.import_one()
        msg = self.store.imported_messages()[0]
        p = self.store.add_profile("旧偏好", "preference", confirmed=True, user_input="记住旧偏好")
        snapshot = self.store.export_data()
        self.store.delete_imported(msg["message_id"])
        self.store.revise_profile(p["item_id"], 1, "更正偏好", content="新偏好")
        latest = self.store.export_ledger()
        restored = RecordsStore(Path(self.temp.name) / "clean.db", clock=lambda: NOW)
        result = restored.import_data(snapshot, latest_ledger=latest)
        self.assertTrue(result["quarantined"])
        self.assertFalse(restored.imported_messages(include_quarantined=True))
        self.assertFalse(restored.context())
        with self.assertRaises(ValueError):
            restored.apply_ledger(latest, release_quarantine=True)
        restored.apply_ledger(latest, release_quarantine=True, user_input="测试夹具确认最新账本覆盖该恢复点以后全部修改")
        self.assertEqual(restored.context()[0]["content"], "新偏好")
        self.assertEqual(len(restored.list_profile(history=True)), 2)
        self.assertNotIn("我研究视觉编码", restored.render_markdown())

    def test_restore_without_latest_ledger_stays_quarantined(self):
        self.import_one()
        self.store.add_profile("已确认事实", confirmed=True, user_input="记住这个事实")
        restored = RecordsStore(Path(self.temp.name) / "old.db", clock=lambda: NOW)
        restored.import_data(self.store.export_data())
        self.assertFalse(restored.imported_messages())
        self.assertFalse(restored.context())
        self.assertEqual(len(restored.imported_messages(include_quarantined=True)), 1)

    def test_live_source_deletion_propagates_and_blocks_new_reference(self):
        p = self.store.add_profile("派生背景", source_refs=["message:live-1"])
        self.store.put_growth("doctorate", "capability", "派生判断", source_refs=["profile:" + p["item_id"]])
        self.store.sync_activity("activity-1", "A2", "2026-10-14", ["message:live-1", "message:live-2"], 2)
        self.store.delete_source("message:live-1")
        self.assertFalse(self.store.list_profile())
        self.assertFalse(self.store.growth()["items"])
        self.assertEqual(self.store.activity_evidence()[0]["source_refs"], ["message:live-2"])
        with self.assertRaises(ValueError):
            self.store.put_growth("doctorate", "goal", "重新引用旧原文", source_refs=["message:live-1"])

    def test_backfill_evidence_versions_and_original_week_are_dynamic(self):
        self.assertTrue(self.store.sync_activity("activity-1", "A2", "2026-10-14", ["message:m1"], 1, lines=["doctorate"]))
        self.assertTrue(self.store.sync_activity("activity-1", "A2", "2026-10-14", ["message:m1", "message:m2"], 2, correction_version=1, lines=["doctorate"]))
        self.assertFalse(self.store.sync_activity("activity-1", "A2", "2026-10-14", ["message:m1"], 1))
        previous = self.store.weekly_review("2026-10-14")["activity_evidence"]
        self.assertEqual(len(previous), 1)
        self.assertEqual(previous[0]["input_version"], 2)
        self.assertEqual(previous[0]["correction_version"], 1)
        self.assertFalse(self.store.weekly_review()["activity_evidence"])
        self.assertNotIn("body", previous[0])
        with self.assertRaises(ValueError):
            self.store.sync_activity("activity-1", "A2", "2026-10-15", [], 3)

    def test_recovered_activity_reference_obeys_newer_deletion_ledger(self):
        self.store.sync_activity("activity-1", "A3", "2026-10-14", ["message:m1"], 1)
        old = self.store.export_data()
        self.store.delete_source("activity:activity-1")
        restored = RecordsStore(Path(self.temp.name) / "refs.db", clock=lambda: NOW)
        restored.import_data(old, latest_ledger=self.store.export_ledger())
        self.assertFalse(restored.activity_evidence())

    def test_large_import_full_day_and_explicit_pagination(self):
        messages = [{"id": str(i), "role": "user", "content": f"测试消息 {i}", "created_at": "2026-10-20T10:00:00+08:00"} for i in range(205)]
        self.store.import_apply("kimi", json.dumps({"id": "many", "messages": messages}), "many.json")
        self.assertEqual(len(self.store.imported_messages(day="2026-10-20", limit=None)), 205)
        first = self.store.imported_messages(day="2026-10-20", limit=200)
        last = self.store.imported_messages(day="2026-10-20", limit=200, offset=200)
        self.assertEqual(len(last), 5)
        self.assertFalse({m["message_id"] for m in first}.intersection(m["message_id"] for m in last))

    def test_codex_single_json_is_not_misclassified_as_jsonl(self):
        result = self.store.import_apply("codex", json.dumps({"id": "session-json", "messages": [{"role": "user", "content": "单行普通JSON"}]}), "conversation.json")
        self.assertEqual(result["counts"]["new"], 1)
        self.assertEqual(self.store.imported_messages(day="unknown")[0]["body"], "单行普通JSON")

    def test_kimi_attachment_metadata_is_reported_missing(self):
        result = self.store.import_preview("kimi", json.dumps({"messages": [{"id": "attachment", "role": "user", "content": "看这张图", "attachments": [{"id": "kimi-file-1", "name": "figure.png"}]}]}), "kimi.json")
        self.assertEqual(result["missing_attachments"][0]["reference"], "kimi-file-1")
        self.assertEqual(result["missing_attachments"][0]["name"], "figure.png")

    def test_export_import_preserves_all_growth_revisions_and_disabled_state(self):
        goal = self.store.put_growth("doctorate", "goal", "目标 v1")
        self.store.put_growth("doctorate", "goal", "目标 v2", item_id=goal["item_id"], expected_version=1, user_input="调整目标")
        old = self.store.export_data()
        self.store.put_growth("doctorate", "goal", "目标 v2", item_id=goal["item_id"], expected_version=2, user_input="暂时停用目标", active=False)
        latest = self.store.export_ledger()
        restored = RecordsStore(Path(self.temp.name) / "versions.db", clock=lambda: NOW)
        restored.import_data(old, latest)
        self.assertFalse(restored.growth()["items"])
        self.assertEqual(len(restored.growth(include_history=True)["versions"]), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
