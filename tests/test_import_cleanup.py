import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from dsh_personal.server import Application
from dsh_personal.import_cleanup import plan_import_removal, remove_import_batch
from dsh_personal.operations import export_bundle, recovery_ledger, restore_bundle


class ImportCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = Application(self.root / "data")

    def fixture(self, known=True):
        content = json.dumps({"id": "withdraw-fixture", "messages": [{"id": "source", "role": "user", "content": "SYNTHETIC-IMPORTED-DELETE"}]})
        batch = self.app.records.import_apply("kimi", content, "fixture.json")
        imported = self.app.records.imported_messages()[0]
        profile = self.app.records.add_profile("SYNTHETIC-IMPORTED-DELETE", source_refs=["import:" + imported["message_id"]], confirmed=True, user_input="Synthetic confirmation")
        activity = self.app.store.ensure_activity("A2")
        self.app.store.submit(activity["id"], "independent-original", "Independent live user original")
        sid = str(uuid.uuid4())
        with self.app.store.db(True) as db:
            db.execute("UPDATE activities SET session_id=?,session_state='ready' WHERE id=?", (sid, activity["id"]))
            db.execute("INSERT INTO messages VALUES(?,?,NULL,'assistant',?,?,'native',NULL,NULL,0)", ("derived-quote", activity["id"], "SYNTHETIC-IMPORTED-DELETE", self.app.store.stamp()))
            if known:
                db.execute("INSERT INTO context_sources VALUES(?,?)", (sid, "profile:" + profile["item_id"]))
        path = self.app.data / "dsh/sessions/_no-cwd" / sid / "session.v4.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"type": "session", "version": 4, "id": sid}) + "\n" + json.dumps({"fixture": "SYNTHETIC-IMPORTED-DELETE"}) + "\n")
        return batch, imported, profile, activity, sid, path, content

    def test_batch_withdrawal_cleans_consumed_context_and_old_restore(self):
        batch, imported, profile, activity, sid, path, _ = self.fixture()
        old = export_bundle(self.app)
        plan = plan_import_removal(self.app, batch["batch_id"])
        self.assertIn("profile:" + profile["item_id"], plan["source_refs"])
        self.assertEqual(plan["native_session_ids"], [sid])
        result = remove_import_batch(self.app, {"batch_id": batch["batch_id"], "plan_token": plan["token"]})
        self.assertEqual(result["removed_new_messages"], 1)
        self.assertFalse(result["native_cleanup_pending"]);self.assertFalse(path.exists())
        self.assertFalse(self.app.records.imported_messages());self.assertFalse(self.app.records.context())
        self.assertIn("Independent live user original", json.dumps(self.app.store.journal()))
        self.assertNotIn("SYNTHETIC-IMPORTED-DELETE", json.dumps(self.app.store.journal()))
        latest = recovery_ledger(self.app)
        self.assertTrue(any(row["target_type"] == "context_ref" for row in latest["core"]["tombstones"]))
        target = self.root / "restore"
        restore_bundle(old, target, latest)
        restored = Application(target)
        self.assertFalse(restored.records.imported_messages(include_quarantined=True))
        self.assertNotIn("SYNTHETIC-IMPORTED-DELETE", json.dumps(restored.store.journal()))
        self.assertFalse((target / path.relative_to(self.app.data)).exists())
        self.assertEqual(restored.store.meta("isolated"), "true")
        self.assertTrue(restored.records.sync_activity(activity["id"], "A2", activity["business_date"], ["message:new"], 10))
        # An idempotent retry must not touch a later conversation generation.
        with self.app.store.db(True) as db:
            db.execute("INSERT INTO messages VALUES(?,?,NULL,'assistant','New generation reply',?,'native',NULL,NULL,0)", ("new-reply", activity["id"], self.app.store.stamp()))
        again = remove_import_batch(self.app, {"batch_id": batch["batch_id"]})
        self.assertFalse(again["native_cleanup_pending"])
        self.assertIn("New generation reply", json.dumps(self.app.store.journal()))

    def test_duplicate_only_batch_preserves_preexisting_import_and_context(self):
        first, imported, profile, activity, sid, path, content = self.fixture()
        duplicate = self.app.records.import_apply("kimi", content, "fixture.json")
        plan = plan_import_removal(self.app, duplicate["batch_id"])
        self.assertEqual(plan["source_refs"], []);self.assertEqual(plan["native_session_ids"], [])
        result = remove_import_batch(self.app, {"batch_id": duplicate["batch_id"], "plan_token": plan["token"]})
        self.assertEqual(result["removed_new_messages"], 0);self.assertTrue(path.exists())
        self.assertEqual(self.app.records.context()[0]["item_id"], profile["item_id"])
        self.assertEqual(self.app.records.imported_messages()[0]["message_id"], imported["message_id"])

    def test_stale_plan_and_injected_sql_failure_do_not_partially_delete(self):
        batch, imported, profile, activity, sid, path, _ = self.fixture()
        plan = plan_import_removal(self.app, batch["batch_id"])
        self.app.records.add_profile("Additional derived", source_refs=["import:" + imported["message_id"]])
        with self.assertRaisesRegex(ValueError, "范围"):
            remove_import_batch(self.app, {"batch_id": batch["batch_id"], "plan_token": plan["token"]})
        with patch("dsh_personal.import_cleanup.apply_source_invalidation", side_effect=RuntimeError("synthetic transaction failure")):
            with self.assertRaises(RuntimeError):
                remove_import_batch(self.app, {"batch_id": batch["batch_id"]})
        self.assertTrue(self.app.records.imported_messages());self.assertEqual(len(self.app.records.list_profile()), 2)
        self.assertEqual(self.app.records.list_batches()[0]["status"], "active");self.assertTrue(path.exists())
        self.assertIsNone(self.app.store.meta("import_removal_plan:" + batch["batch_id"]))

    def test_legacy_context_failure_can_retry_after_batch_already_removed(self):
        from runtime.native_cleanup import NativeCleanupError
        batch, imported, profile, activity, sid, path, _ = self.fixture(known=False)
        with self.app.store.db(True) as db:
            db.execute("INSERT INTO context_sources VALUES(?,?)", (sid, "legacy_profile_context"))
            # Profile rotation already detached this archived generation from the
            # activity; its explicit legacy marker must retain deletion ownership.
            db.execute("UPDATE activities SET session_id=NULL,session_state='pending' WHERE id=?", (activity["id"],))
        plan = plan_import_removal(self.app, batch["batch_id"])
        self.assertEqual(plan["legacy_session_ids"], [sid])
        self.assertEqual(plan["activity_ids"], [])
        with patch("runtime.native_cleanup.purge_sessions", side_effect=NativeCleanupError("synthetic stopped-host failure")):
            failed = remove_import_batch(self.app, {"batch_id": batch["batch_id"], "plan_token": plan["token"]})
        self.assertTrue(failed["native_cleanup_pending"]);self.assertTrue(path.exists())
        self.assertEqual(self.app.records.list_batches()[0]["status"], "removed")
        retry_plan = plan_import_removal(self.app, batch["batch_id"])
        self.assertIn("profile:" + profile["item_id"], retry_plan["source_refs"])
        retried = remove_import_batch(self.app, {"batch_id": batch["batch_id"], "plan_token": retry_plan["token"]})
        self.assertFalse(retried["native_cleanup_pending"]);self.assertFalse(path.exists())
        self.assertIsNotNone(self.app.store.meta("import_removal_done:" + batch["batch_id"]))


if __name__ == "__main__":
    unittest.main()
