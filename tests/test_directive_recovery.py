"""Directive provenance deletion and cross-generation ledger recovery fixtures."""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
import uuid
import zipfile

from dsh_personal.core import Store
from dsh_personal.records import RecordsStore
from dsh_personal.task_requirements import initialize, apply, snapshot
from dsh_personal.deletion import delete_owned_message, deletion_plan, apply_message_deletion
from dsh_personal.operations import export_bundle, restore_bundle, recovery_ledger, _merge_core_ledger
from dsh_personal.directives import process


class DirectiveRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.clock = lambda: datetime(2026, 10, 23, 16, 0, tzinfo=timezone.utc)
        self.store = Store(self.data / "personal.sqlite", self.clock)
        self.records = RecordsStore(self.store.path, self.clock)
        self.config = {"model": {}, "notification": {}, "backup": {}}
        (self.data / "config.json").write_text(json.dumps(self.config))
        self.app = SimpleNamespace(data=self.data, store=self.store, records=self.records, config=lambda: self.config)
        with self.store.db(True) as db:
            initialize(db)
            # Same schema as the directive handler; lets these fixtures isolate
            # deletion/recovery from model parsing and message-routing behavior.
            db.execute("""CREATE TABLE IF NOT EXISTS directive_requests (
                message_id TEXT PRIMARY KEY REFERENCES messages(id), activity_id TEXT NOT NULL,
                status TEXT NOT NULL,decision TEXT,receipt_id TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""")
        self.chat = self.store.ensure_activity("A2", "2026-10-21")
        self.other = self.store.ensure_activity("A3", "2026-10-20")

    def message(self, body):
        return self.store.submit(self.chat["id"], str(uuid.uuid4()), body)["receipt"]

    def task(self):
        return self.store.create_task("Independent initial task", inputs="Independent input", deliverable="Independent artifact",
                                      acceptance="Independent criterion", source_activity_id=self.other["id"])

    def change(self, task_id, revision, body, patch, *, mid=None):
        mid = mid or self.message(body)
        receipt = str(uuid.uuid4())
        stamp = self.store.stamp()
        with self.store.db(True) as db:
            after = apply(db, task_id, revision, patch, body, mid, stamp)
            db.execute("INSERT INTO messages VALUES(?,?,NULL,'assistant',?,?,'local_control',NULL,NULL,0)",
                       (receipt, self.chat["id"], "Saved: " + body, stamp))
            db.execute("INSERT INTO directive_requests VALUES(?,?,'applied',?,?,?,?)",
                       (mid, self.chat["id"], json.dumps({"patch": patch}), receipt, stamp, stamp))
        return mid, receipt, after

    def assert_scrubbed(self, store, tid, marker):
        task = store.task(tid)
        self.assertEqual([task[k] for k in ("goal", "inputs", "deliverable", "acceptance")], [""] * 4)
        self.assertEqual(task["status"], "recovery_hold")
        with store.db() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM task_requirement_revisions WHERE task_id=?", (tid,))]
            self.assertTrue(rows)
            self.assertTrue(all(r["before_json"] == "{}" and r["after_json"] == "{}" and not r["user_input"] for r in rows))
            self.assertNotIn(marker, json.dumps([dict(r) for r in db.execute("SELECT * FROM directive_requests")]))
        self.assertNotIn(marker, json.dumps(store.journal()))

    def legacy_bundle(self):
        """Represent a genuine pre-feature schema/ledger, with valid hashes."""
        with zipfile.ZipFile(io.BytesIO(export_bundle(self.app))) as source:
            entries = {name: source.read(name) for name in source.namelist()}
        path = self.root / "legacy.sqlite"
        path.write_bytes(entries["personal.sqlite"])
        with sqlite3.connect(path) as db:
            for table in ("directive_requests", "task_requirement_revisions"):
                db.execute("DROP TABLE " + table)
            db.execute("VACUUM")
        entries["personal.sqlite"] = path.read_bytes()
        data = json.loads(entries["data.json"])
        for table in ("directive_requests", "task_requirement_revisions"):
            data.pop(table)
        entries["data.json"] = json.dumps(data).encode()
        ledger = json.loads(entries["ledger.json"])
        for table in ("directive_requests", "task_requirement_revisions"):
            ledger["core"].pop(table)
        for task in ledger["core"]["tasks"]:
            for field in ("requirements_version", "goal", "inputs", "deliverable", "acceptance"):
                task.pop(field)
        entries["ledger.json"] = json.dumps(ledger).encode()
        manifest = json.loads(entries["manifest.json"])
        manifest["application_version"] = "v1.1.0-preview.14"
        for name in manifest["files"]:
            manifest["files"][name] = {"sha256": sha256(entries[name]).hexdigest(), "size": len(entries[name])}
        entries["manifest.json"] = json.dumps(manifest).encode()
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w", zipfile.ZIP_DEFLATED) as output:
            for name, value in entries.items():
                output.writestr(name, value)
        return content.getvalue()

    def test_live_delete_clears_copied_later_history_and_cross_activity_task(self):
        marker = "SYNTHETIC_PRIVATE_REQUIREMENT_7f91"
        task = self.task()
        run = self.store.claim_task(task["id"])
        self.store.deliver_task(task["id"], run["version"], "Independent earlier artifact")
        self.store.review_task(task["id"], run["version"], "accept", "Independent user acceptance")
        first, receipt, _ = self.change(task["id"], 0, marker, {"goal": marker})
        second, second_receipt, _ = self.change(task["id"], 1, "Independent later request", {"inputs": "Independent newer input"})
        profile = self.records.add_profile(marker, "preference", "routine:A1", ["message:" + first], True, user_input=marker)
        self.records.revise_profile(profile["item_id"], 1, "Later profile edit", content="Revised derived preference")
        with self.store.db() as db:
            self.assertIn(marker, db.execute("SELECT before_json FROM task_requirement_revisions WHERE requirements_version=2").fetchone()[0])
        plan = deletion_plan(self.app, first)
        self.assertIn(task["id"], plan["task_ids"])
        self.assertEqual(plan["task_versions"][task["id"]]["requirements_version"], 2)
        deleted = delete_owned_message(self.app, {"message_id": first, "plan_token": plan["token"]})
        self.assertFalse(deleted["sqlite_cleanup_pending"])
        self.assert_scrubbed(self.store, task["id"], marker)
        task_now = self.store.task(task["id"])
        self.assertEqual(task_now["authorization"], "Independent initial task")
        self.assertEqual(task_now["reviews"][0]["input"], "Independent user acceptance")
        self.assertEqual(task_now["version"], run["version"])
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT body FROM messages WHERE id=?", (second,)).fetchone()[0], "Independent later request")
            for rid in (receipt, second_receipt):
                self.assertEqual(db.execute("SELECT body,deleted FROM messages WHERE id=?", (rid,)).fetchone()[:], ("", 1))
            self.assertEqual(snapshot(db, task["id"])["requirements_version"], 2)
        self.assertFalse(self.records.list_profile(history=True, include_inactive=True))
        self.assertNotIn(marker, json.dumps(recovery_ledger(self.app)))
        self.assertNotIn(marker.encode(), self.store.path.read_bytes())

    def test_requirements_revision_changes_deletion_plan_without_delivery_change(self):
        task = self.task()
        first, _, _ = self.change(task["id"], 0, "First request", {"goal": "New first goal"})
        second = self.message("Second request")
        plan = deletion_plan(self.app, first)
        with self.store.db(True) as db:
            apply(db, task["id"], 1, {"inputs": "Second input"}, "Second request", second, self.store.stamp())
        self.assertEqual(self.store.task(task["id"])["version"], 0)
        with self.assertRaisesRegex(ValueError, "重新核对"):
            delete_owned_message(self.app, {"message_id": first, "plan_token": plan["token"]})
        self.assertIn(first, {m["id"] for m in self.store.activity(self.chat["id"])["messages"]})

    def test_old_snapshot_latest_ledger_restores_new_requirements_without_replay(self):
        task = self.task()
        old = self.legacy_bundle()
        source, _, _ = self.change(task["id"], 0, "Later explicit adjustment", {"goal": "Later goal", "acceptance": "Later criterion"})
        latest = recovery_ledger(self.app)
        destination = self.root / "old-snapshot"
        restore_bundle(old, destination, latest)
        restored = Store(destination / "personal.sqlite", self.clock)
        with restored.db() as db:
            current = snapshot(db, task["id"])
            self.assertEqual(current["requirements_version"], 1)
            self.assertEqual(current["goal"], "Later goal")
            self.assertEqual(current["acceptance"], "Later criterion")
            self.assertFalse(db.execute("SELECT 1 FROM messages WHERE id=?", (source,)).fetchone())
            self.assertFalse(db.execute("SELECT 1 FROM directive_requests WHERE message_id=?", (source,)).fetchone())
            self.assertFalse(db.execute("SELECT 1 FROM task_runs").fetchone())
        self.assertEqual(restored.task(task["id"])["status"], "needs_input")
        self.assertEqual(restored.meta("isolated"), "true")
        self.assertEqual(restored.tick(), [])
        self.assertEqual(json.loads((destination / "config.json").read_text())["model"], {})

    def test_new_snapshot_old_ledger_optional_keys_do_not_erase_requirements(self):
        task = self.task()
        self.change(task["id"], 0, "Keep newer requirements", {"goal": "Current revision goal"})
        latest = recovery_ledger(self.app)
        latest["core"].pop("directive_requests")
        latest["core"].pop("task_requirement_revisions")
        for row in latest["core"]["tasks"]:
            for key in ("requirements_version", "goal", "inputs", "deliverable", "acceptance"):
                row.pop(key)
        destination = self.root / "legacy-ledger"
        restore_bundle(export_bundle(self.app), destination, latest)
        restored = Store(destination / "personal.sqlite", self.clock)
        with restored.db() as db:
            self.assertEqual(snapshot(db, task["id"])["requirements_version"], 1)
            self.assertEqual(snapshot(db, task["id"])["goal"], "Current revision goal")
        self.assertEqual(restored.meta("isolated"), "true")

    def test_delete_latest_ledger_scrubs_old_snapshot_and_all_portable_copies(self):
        marker = "SYNTHETIC_DELETED_DIRECTIVE_e428"
        task = self.task()
        first, _, _ = self.change(task["id"], 0, marker, {"goal": marker})
        self.change(task["id"], 1, "Independent following requirement", {"inputs": "New input"})
        old = export_bundle(self.app)
        delete_owned_message(self.app, {"message_id": first})
        destination = self.root / "deletion-restored"
        restore_bundle(old, destination, recovery_ledger(self.app))
        restored = Store(destination / "personal.sqlite", self.clock)
        self.assert_scrubbed(restored, task["id"], marker)
        self.assertTrue(all(marker.encode() not in path.read_bytes() for path in destination.rglob("*") if path.is_file()))
        self.assertIn("Independent following requirement", json.dumps(restored.journal()))

    def test_missing_source_and_already_deleted_source_cannot_bypass_cleanup(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                task = self.task()
                marker = "SYNTHETIC_EARLY_RETURN_" + str(missing)
                mid, receipt, _ = self.change(task["id"], 0, marker, {"goal": marker})
                ledger = recovery_ledger(self.app)
                with self.store.db(True) as db:
                    if missing:
                        db.execute("DELETE FROM directive_requests WHERE message_id=?", (mid,))
                        db.execute("DELETE FROM messages WHERE id=?", (mid,))
                    else:
                        db.execute("UPDATE messages SET body='',deleted=1 WHERE id=?", (mid,))
                        db.execute("UPDATE activities SET session_state='pending' WHERE id=?", (self.chat["id"],))
                    db.execute("INSERT INTO tombstones VALUES(?, 'message',?,NULL,?)", (str(uuid.uuid4()), mid, self.store.stamp()))
                ledger["core"]["tombstones"].append({"id": str(uuid.uuid4()), "target_type": "message", "target_id": mid,
                                                      "fingerprint": None, "created_at": self.store.stamp()})
                _merge_core_ledger(self.store, ledger)
                self.assert_scrubbed(self.store, task["id"], marker)
                with self.store.db() as db:
                    self.assertEqual(db.execute("SELECT body,deleted FROM messages WHERE id=?", (receipt,)).fetchone()[:], ("", 1))

    def test_restore_pending_or_missing_receipt_directives_are_held(self):
        task = self.task()
        mid, receipt, _ = self.change(task["id"], 0, "Explicit request", {"goal": "Saved goal"})
        pending = self.message("Pending decision")
        with self.store.db(True) as db:
            db.execute("INSERT INTO directive_requests VALUES(?,?,'pending',NULL,NULL,?,?)",
                       (pending, self.chat["id"], self.store.stamp(), self.store.stamp()))
            db.execute("DELETE FROM messages WHERE id=?", (receipt,))
        destination = self.root / "uncertain-receipts"
        restore_bundle(export_bundle(self.app), destination)
        restored = Store(destination / "personal.sqlite", self.clock)
        with restored.db() as db:
            for source in (mid, pending):
                row = db.execute("SELECT status,receipt_id FROM directive_requests WHERE message_id=?", (source,)).fetchone()
                self.assertEqual(row[:], ("recovery_hold", None))
        self.assertEqual(restored.meta("isolated"), "true")

    def test_stale_requirement_ledger_does_not_override_newer_revision(self):
        task = self.task()
        self.change(task["id"], 0, "First", {"goal": "First goal"})
        old = deepcopy(recovery_ledger(self.app))
        self.change(task["id"], 1, "Second", {"goal": "Second goal"})
        _merge_core_ledger(self.store, old)
        with self.store.db() as db:
            self.assertEqual(snapshot(db, task["id"])["requirements_version"], 2)
            self.assertEqual(snapshot(db, task["id"])["goal"], "Second goal")

    def test_old_acceptance_cannot_accept_a_new_requirement_revision(self):
        task = self.task()
        run = self.store.claim_task(task["id"])
        self.store.deliver_task(task["id"], run["version"], "Original delivered artifact")
        self.store.review_task(task["id"], run["version"], "accept", "Only the original artifact is accepted")
        old = recovery_ledger(self.app)
        self.change(task["id"], 0, "Request a different result", {"goal": "New unaccepted goal"})
        self.assertEqual(self.store.task(task["id"])["status"], "revision")
        _merge_core_ledger(self.store, old)
        current = self.store.task(task["id"])
        self.assertEqual(current["goal"], "New unaccepted goal")
        self.assertEqual(current["status"], "needs_input")
        self.assertEqual(current["reviews"][0]["input"], "Only the original artifact is accepted")
        self.assertEqual(current["deliveries"][0]["status"], "accepted")

    def test_cross_activity_show_and_replace_receipts_follow_preference_source(self):
        marker = "SYNTHETIC_PREFERENCE_TO_ERASE_58cd"
        body = "以后回答默认附上 " + marker
        original = self.message(body)
        process(self.store, self.records, original, classifier=lambda _: {
            "intent": "apply", "changes": [{"kind": "preference", "action": "add", "scope": "global",
                                             "item_id": None, "content": marker, "quote": body}]})
        profile = self.records.list_profile()[0]
        show = self.store.submit(self.other["id"], "show-cross-activity", "查看已保存的回复偏好")["receipt"]
        process(self.store, self.records, show, classifier=lambda _: {"intent": "show", "changes": []})
        replacement_text = "以后把回复偏好改成先列结论"
        replacement = self.store.submit(self.other["id"], "replace-cross-activity", replacement_text)["receipt"]
        process(self.store, self.records, replacement, classifier=lambda _: {
            "intent": "apply", "changes": [{"kind": "preference", "action": "replace", "scope": "global",
                                             "item_id": profile["item_id"], "content": "先列结论", "quote": replacement_text}]})
        with self.store.db() as db:
            for mid in (show, replacement):
                request = db.execute("SELECT * FROM directive_requests WHERE message_id=?", (mid,)).fetchone()
                self.assertIn("profile:" + profile["item_id"], json.loads(request["decision"])["source_refs"])
                self.assertIn(marker, db.execute("SELECT body FROM messages WHERE id=?", (request["receipt_id"],)).fetchone()[0])
        old = export_bundle(self.app)
        delete_owned_message(self.app, {"message_id": original})
        destination = self.root / "cross-preference"
        restore_bundle(old, destination, recovery_ledger(self.app))
        restored = Store(destination / "personal.sqlite", self.clock)
        for store in (self.store, restored):
            with store.db() as db:
                for mid in (show, replacement):
                    self.assertEqual(db.execute("SELECT status,decision,receipt_id FROM directive_requests WHERE message_id=?", (mid,)).fetchone()[:],
                                     ("deleted", None, None))
                    self.assertEqual(db.execute("SELECT deleted FROM messages WHERE id=?", (mid,)).fetchone()[0], 0)
            self.assertNotIn(marker, json.dumps(store.journal()))
        self.assertFalse(RecordsStore(restored.path).list_profile(history=True, include_inactive=True))


if __name__ == "__main__":
    unittest.main()
