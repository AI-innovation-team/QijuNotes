from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import shutil
import os
import tempfile
import unittest
from unittest.mock import patch

from dsh_personal.core import Store
from dsh_personal.records import RecordsStore
from dsh_personal.release import (SCHEMA0_DDL, ROOT, activate_release, inspect_schema,
    install_release, migrate, rollback, verify_release, _source_files)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()

    def source_snapshot(self):
        source = self.root / "source-snapshot"
        source.mkdir()
        for file, relative in _source_files(ROOT):
            (source / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, source / relative)
        runtime = ROOT / "runtime/npm/node_modules"
        if runtime.exists():
            (source / "runtime/npm").mkdir(parents=True, exist_ok=True)
            os.symlink(runtime, source / "runtime/npm/node_modules", target_is_directory=True)
        return source

    def schema0(self, bad=False):
        with sqlite3.connect(self.data / "personal.sqlite") as db:
            db.executescript(SCHEMA0_DDL)
            db.execute("INSERT INTO legacy_entries VALUES(?,?,?,?,?,?,?)", ("original-id-1", "A3", "2026-10-20", "2026-10-22T10:00:00+00:00", "user", "开发期测试夹具：睡得一般", "original-submission-1"))
            db.execute("INSERT INTO legacy_entries VALUES(?,?,?,?,?,?,?)", ("original-id-2", "A3", "2026-10-20", "2026-10-22T10:01:00+00:00" if not bad else "2026-10-22 10:01:00", "assistant", "开发期测试夹具回复", None))

    def test_real_sql_migration_preserves_ids_dates_body_and_is_isolated(self):
        self.schema0()
        with sqlite3.connect(self.data / "personal.sqlite") as db:
            original_dump = list(db.iterdump())
        result = migrate(self.data)
        self.assertTrue(result["changed"])
        self.assertEqual(result["details"]["messages_migrated"], 2)
        self.assertEqual(result["details"]["activities"], 1)
        checkpoint = Path(result["checkpoint"])
        with sqlite3.connect(checkpoint / "before.sqlite") as db:
            self.assertEqual(list(db.iterdump()), original_dump)
        recorded = json.loads((checkpoint / "checkpoint.json").read_text())
        self.assertEqual(sha256((checkpoint / "before.sqlite").read_bytes()).hexdigest(), recorded["database_sha256"])
        self.assertEqual(inspect_schema(self.data / "personal.sqlite")["schema_version"], 1)
        store = Store(self.data / "personal.sqlite")
        self.assertEqual(store.meta("isolated"), "true")
        rows = store.journal()
        self.assertEqual(rows[0]["id"], "original-id-1")
        self.assertEqual(rows[0]["submission_id"], "original-submission-1")
        self.assertEqual(rows[0]["business_date"], "2026-10-20")
        self.assertEqual(rows[0]["received_at"], "2026-10-22T10:00:00+00:00")
        self.assertEqual(rows[0]["body"], "开发期测试夹具：睡得一般")
        self.assertFalse(store.tick())
        self.assertFalse(migrate(self.data)["changed"])

    def test_failed_candidate_migration_leaves_source_database_unchanged(self):
        self.schema0(bad=True)
        before = (self.data / "personal.sqlite").read_bytes()
        with self.assertRaisesRegex(ValueError, "缺少时区"):
            migrate(self.data)
        self.assertEqual((self.data / "personal.sqlite").read_bytes(), before)
        self.assertEqual(inspect_schema(self.data / "personal.sqlite")["schema_version"], 0)
        self.assertTrue(list((self.data / "upgrade-checkpoints").glob("*/before.sqlite")))

    def test_migration_and_rollback_refuse_running_application(self):
        self.schema0()
        with (self.data / "application.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaisesRegex(RuntimeError, "停止本项目"):
                migrate(self.data)
        result = migrate(self.data)
        with (self.data / "dsh/web-host.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaises(RuntimeError):
                rollback(self.data, result["checkpoint"])

    def test_database_rollback_preserves_newer_state_and_does_not_invent_old_app(self):
        self.schema0()
        migration = migrate(self.data)
        store = Store(self.data / "personal.sqlite")
        store.set_meta("fixture_after_migration", "newer state")
        result = rollback(self.data, migration["checkpoint"])
        self.assertTrue(result["database_rolled_back"])
        self.assertFalse(result["application_rolled_back"])
        self.assertTrue(result["remaining"])
        self.assertEqual(inspect_schema(self.data / "personal.sqlite")["schema_version"], 0)
        with sqlite3.connect(self.data / "personal.sqlite") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM legacy_entries").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='isolated'").fetchone()[0], "true")
        with sqlite3.connect(result["newer_state_preserved_at"]) as db:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='fixture_after_migration'").fetchone()[0], "newer state")
        self.assertFalse((self.data / "active-release.json").exists())

    def test_checkpoint_tamper_and_unknown_schema_are_rejected(self):
        self.schema0()
        migration = migrate(self.data)
        previous = Path(migration["checkpoint"]) / "before.sqlite"
        with previous.open("ab") as handle:
            handle.write(b"tampered")
        with self.assertRaisesRegex(ValueError, "验证失败"):
            rollback(self.data, migration["checkpoint"])
        with sqlite3.connect(self.data / "personal.sqlite") as db:
            db.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
        with self.assertRaisesRegex(ValueError, "没有该源"):
            migrate(self.data)

    def test_current_application_install_is_idempotent_and_manifest_verified(self):
        source = self.source_snapshot()
        first = install_release(source, self.root / "releases")
        second = install_release(source, self.root / "releases")
        self.assertEqual(first["release"], second["release"])
        self.assertTrue(second["reused_identical_installation"])
        manifest = verify_release(first["release"])
        self.assertEqual(manifest["supported_database_schemas"], [1])
        self.assertIn("dsh_personal/server.py", manifest["files"])
        Store(self.data / "personal.sqlite")
        RecordsStore(self.data / "personal.sqlite")
        activation = activate_release(self.data, first["release"])
        self.assertEqual((self.data / "current-release").resolve(), Path(first["release"]))
        self.assertEqual(activation["release_id"], first["release_id"])
        (Path(first["release"]) / "web/manifest.json").write_text("tampered")
        with self.assertRaisesRegex(ValueError, "完整性"):
            verify_release(first["release"])

    def test_interrupted_activation_leaves_recovery_marker_until_rollback(self):
        self.schema0()
        candidate = install_release(self.source_snapshot(), self.root / "releases")
        with patch("dsh_personal.release._activate_unlocked", side_effect=RuntimeError("injected activation failure")):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                migrate(self.data, candidate_release=candidate["release"])
        marker = json.loads((self.data / "upgrade-in-progress.json").read_text())
        self.assertEqual(inspect_schema(self.data / "personal.sqlite")["schema_version"], 1)
        self.assertEqual(marker["action"], "migrate")
        rollback(self.data, marker["checkpoint"])
        self.assertFalse((self.data / "upgrade-in-progress.json").exists())
        self.assertEqual(inspect_schema(self.data / "personal.sqlite")["schema_version"], 0)

    def test_activation_refuses_schema_mismatch(self):
        current = install_release(self.source_snapshot(), self.root / "releases")
        self.schema0()
        with self.assertRaisesRegex(ValueError, "不兼容"):
            activate_release(self.data, current["release"])
        self.assertFalse((self.data / "current-release").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
