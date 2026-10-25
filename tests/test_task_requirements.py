"""Isolated SQLite checks; no live data, model or native runtime calls."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from dsh_personal.core import Store
from dsh_personal import task_requirements as requirements


class TaskRequirementsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "db.sqlite", clock=lambda: datetime(2026, 10, 23, 12, tzinfo=timezone.utc))
        with self.store.db(True) as db:
            requirements.initialize(db)
        self.activity = self.store.ensure_activity("CHAT")
        self.request = "请把现有任务改为含三个具体例子，保留来源。"
        self.source = self.store.submit(self.activity["id"], "requirements-source-01", self.request)["receipt"]
        self.task = self.store.create_task(
            "原目标", inputs="原始材料", deliverable="原交付", acceptance="原验收条件",
            run_at="2026-10-25T12:00:00+00:00", interval_days=3,
            source_activity_id=self.activity["id"], catchup_policy="skip_missed",
            deadline="2026-10-28T12:00:00+00:00")

    def apply(self, patch=None, expected=0, **kwargs):
        with self.store.db(True) as db:
            return requirements.apply(
                db, self.task["id"], expected,
                {"deliverable": "给出三个具体例子"} if patch is None else patch,
                kwargs.get("user_input", self.request), kwargs.get("source", self.source),
                kwargs.get("stamp", self.store.stamp()))

    def current(self):
        with self.store.db() as db:
            return requirements.snapshot(db, self.task["id"])

    def history(self):
        with self.store.db() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM task_requirement_revisions WHERE task_id=? ORDER BY requirements_version",
                (self.task["id"],))]

    def assert_error(self, code, operation):
        with self.assertRaises(requirements.TaskRequirementsError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_legacy_snapshot_is_zero_and_works_with_plain_sqlite_rows(self):
        with sqlite3.connect(self.store.path) as db:
            value = requirements.snapshot(db, self.task["id"])
        self.assertEqual(value, {"task_id": self.task["id"], "requirements_version": 0,
            "goal": "原目标", "inputs": "原始材料", "deliverable": "原交付",
            "acceptance": "原验收条件", "status": "queued"})

    def test_revision_records_provenance_without_changing_authority_or_schedule(self):
        value = self.apply({"goal": "新目标", "inputs": "", "acceptance": "每例有出处"})
        self.assertEqual(value["requirements_version"], 1)
        self.assertEqual(value["goal"], "新目标")
        self.assertEqual(value["inputs"], "")
        self.assertEqual(value["deliverable"], "原交付")
        row = self.history()[0]
        self.assertEqual(row["source_message_id"], self.source)
        self.assertEqual(row["user_input"], self.request)
        self.assertEqual(json.loads(row["before_json"])["requirements_version"], 0)
        self.assertEqual(json.loads(row["after_json"]), value)
        current = self.store.task(self.task["id"])
        for field in ("id", "owner", "authorization", "run_at", "interval_days", "version",
                      "source_activity_id", "catchup_policy", "deadline", "deliveries", "reviews"):
            self.assertEqual(current[field], self.task[field], field)

    def test_conflicting_expected_revision_keeps_prior_change(self):
        first = self.apply()
        self.assert_error("requirements_conflict", lambda: self.apply({"goal": "过时编辑"}))
        self.assertEqual(self.current(), first)
        self.assertEqual(len(self.history()), 1)
        self.assertEqual(self.apply({"goal": "第二次编辑"}, expected=1)["requirements_version"], 2)

    def test_running_task_is_not_cancelled_or_edited(self):
        self.store.claim_task(self.task["id"])
        before = self.store.task(self.task["id"])
        self.assert_error("task_running", self.apply)
        self.assertEqual(self.store.task(self.task["id"]), before)
        self.assertFalse(self.history())

    def test_cancelled_task_cannot_be_revived(self):
        self.store.review_task(self.task["id"], 0, "cancel", "不再执行")
        before = self.store.task(self.task["id"])
        self.assert_error("task_cancelled", self.apply)
        self.assertEqual(self.store.task(self.task["id"]), before)
        self.assertFalse(self.history())

    def test_accepted_task_revision_preserves_original_artifact_and_user_acceptance(self):
        self.store.claim_task(self.task["id"])
        self.store.deliver_task(self.task["id"], 1, "第一版成果")
        self.store.review_task(self.task["id"], 1, "accept", "第一版可以")
        before = self.store.task(self.task["id"])
        value = self.apply()
        revised = self.store.task(self.task["id"])
        self.assertEqual(value["status"], "revision")
        self.assertEqual(value["requirements_version"], 1)
        for field in ("version", "result", "deliveries", "reviews"):
            self.assertEqual(revised[field], before[field], field)
        self.assertEqual(revised["run_at"], self.store.stamp())
        run = self.store.claim_task(self.task["id"])
        self.assertEqual(run["version"], 2)
        self.store.deliver_task(self.task["id"], 2, "第二版成果")
        final = self.store.task(self.task["id"])
        self.assertEqual(final["deliveries"][0]["body"], "第一版成果")
        self.assertEqual(final["deliveries"][0]["status"], "accepted")
        self.assertEqual(final["reviews"], before["reviews"])
        self.assertEqual(final["status"], "awaiting_acceptance")
        self.assertEqual(self.current()["requirements_version"], 1)

    def test_unaccepted_delivery_can_be_revised_without_fake_review(self):
        self.store.claim_task(self.task["id"])
        self.store.deliver_task(self.task["id"], 1, "待验收旧成果")
        before = self.store.task(self.task["id"])
        self.assertEqual(self.apply()["status"], "revision")
        now = self.store.task(self.task["id"])
        self.assertEqual(now["deliveries"], before["deliveries"])
        self.assertFalse(now["reviews"])

    def test_needs_input_resumes_but_permission_wait_does_not_grant_permission(self):
        with self.store.db(True) as db:
            db.execute("UPDATE tasks SET status='needs_input',error='请补充材料' WHERE id=?", (self.task["id"],))
        self.assertEqual(self.apply()["status"], "queued")
        self.assertIsNone(self.store.task(self.task["id"])["error"])
        with self.store.db(True) as db:
            db.execute("UPDATE tasks SET status='permission_required',error='需要额外授权' WHERE id=?", (self.task["id"],))
        self.assertEqual(self.apply({"inputs": "补充材料"}, expected=1)["status"], "permission_required")
        self.assertEqual(self.store.task(self.task["id"])["error"], "需要额外授权")
        self.assertIsNone(self.store.claim_task(self.task["id"]))

    def test_recovery_hold_or_unknown_state_cannot_be_bypassed(self):
        for state in ("recovery_hold", "unexpected_future_state"):
            with self.subTest(state=state):
                with self.store.db(True) as db:
                    db.execute("UPDATE tasks SET status=? WHERE id=?", (state, self.task["id"]))
                self.assert_error("task_blocked", self.apply)
                self.assertFalse(self.history())

    def test_forbidden_fields_invalid_types_and_empty_requirements_are_atomic(self):
        patches = [[], {}, {"goal": "新目标", "owner": "Agent"}, {"run_at": self.store.stamp()},
            {"interval_days": 1}, {"status": "accepted"}, {"authorization": "授权"},
            {"version": 9}, {"goal": None}, {"inputs": {}}, {"goal": True},
            {"goal": " "}, {"deliverable": ""}, {"acceptance": "\n"},
            {"inputs": "x" * (requirements.MAX_TEXT_LENGTH + 1)}]
        before = self.current()
        for patch in patches:
            with self.subTest(patch=repr(patch)[:80]):
                self.assert_error("invalid_patch", lambda: self.apply(patch))
                self.assertEqual(self.current(), before)
        self.assertFalse(self.history())

    def test_no_op_does_not_restart_completed_work(self):
        self.store.claim_task(self.task["id"])
        self.store.deliver_task(self.task["id"], 1, "现有成果")
        self.store.review_task(self.task["id"], 1, "accept", "通过")
        before = self.store.task(self.task["id"])
        self.assert_error("no_changes", lambda: self.apply({"goal": "原目标"}))
        self.assertEqual(self.store.task(self.task["id"]), before)
        self.assertFalse(self.history())

    def test_invalid_revision_and_missing_task(self):
        for expected in (-1, True, "0", None):
            with self.subTest(expected=expected):
                self.assert_error("invalid_revision", lambda: self.apply(expected=expected))
        with self.store.db() as db:
            self.assert_error("task_missing", lambda: requirements.snapshot(db, "missing-task"))

    def test_only_live_user_message_is_a_valid_source(self):
        with self.store.db() as db:
            assistant = db.execute("SELECT id FROM messages WHERE role='assistant' LIMIT 1").fetchone()[0]
        for source in ("", "missing-message", assistant, None):
            with self.subTest(source=source):
                self.assert_error("invalid_source", lambda: self.apply(source=source))
        with self.store.db(True) as db:
            db.execute("UPDATE messages SET deleted=1 WHERE id=?", (self.source,))
        self.assert_error("invalid_source", self.apply)
        self.assertFalse(self.history())

    def test_model_rewording_cannot_replace_original_user_input(self):
        self.assert_error("invalid_source", lambda: self.apply(user_input="模型改写后的要求"))
        self.assertFalse(self.history())
        self.assertEqual(self.current()["requirements_version"], 0)

    def test_bad_provenance_or_stamp_makes_no_changes(self):
        self.assert_error("invalid_patch", lambda: self.apply(user_input=" "))
        for stamp in (None, "invalid", "2026-10-23T12:00:00"):
            with self.subTest(stamp=stamp):
                self.assert_error("invalid_stamp", lambda: self.apply(stamp=stamp))
        self.assertFalse(self.history())

    def test_apply_requires_transaction_and_outer_rollback_undoes_both_tables(self):
        with self.store.db() as db:
            self.assert_error("transaction_required", lambda: requirements.apply(
                db, self.task["id"], 0, {"goal": "新目标"}, self.request, self.source, self.store.stamp()))
        before = self.store.task(self.task["id"])
        with self.assertRaisesRegex(RuntimeError, "rollback fixture"):
            with self.store.db(True) as db:
                requirements.apply(db, self.task["id"], 0, {"goal": "新目标"}, self.request, self.source, self.store.stamp())
                self.assertTrue(db.in_transaction)
                raise RuntimeError("rollback fixture")
        self.assertEqual(self.store.task(self.task["id"]), before)
        self.assertFalse(self.history())

    def test_initialize_never_commits_callers_transaction(self):
        with sqlite3.connect(":memory:", isolation_level=None) as db:
            db.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY)")
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO tasks VALUES ('uncommitted')")
            requirements.initialize(db)
            requirements.initialize(db)
            self.assertTrue(db.in_transaction)
            db.rollback()
            self.assertIsNone(db.execute("SELECT id FROM tasks").fetchone())
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='task_requirement_revisions'").fetchone())

    def test_simultaneous_edits_with_same_expected_version_have_one_winner(self):
        barrier = threading.Barrier(2)
        def edit(index):
            barrier.wait(timeout=5)
            try:
                return self.apply({"goal": f"并发目标{index}"})["requirements_version"]
            except requirements.TaskRequirementsError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(edit, range(2)))
        self.assertCountEqual(results, [1, "requirements_conflict"])
        self.assertEqual(len(self.history()), 1)
        self.assertEqual(self.current()["requirements_version"], 1)


if __name__ == "__main__":
    unittest.main()
