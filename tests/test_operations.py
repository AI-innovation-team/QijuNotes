from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import patch
from hashlib import sha256
import io
import json
import os
import os.path
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import zipfile
import uuid

from dsh_personal.core import Store
from dsh_personal.records import RecordsStore
from dsh_personal.operations import (backup, create_key, decrypt, encrypt, export_bundle,
    maintenance_lock, markdown_export, protect_ledgers, recovery_ledger,
    restore_bundle, release_restore, verify_bundle, _key, _retention)


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "instance"
        self.data.mkdir()
        self.clock = lambda: datetime(2026, 10, 22, 15, 40, tzinfo=timezone.utc)
        store = Store(self.data / "personal.sqlite", self.clock)
        records = RecordsStore(self.data / "personal.sqlite", self.clock)
        conf = {"public_url": "http://127.0.0.1:18780", "model": {"name": "fixture", "api_key": "secret-test-key", "max_tokens": 8192}, "notification": {"url": "https://example.invalid/private-webhook-secret"}, "backup": {}}
        (self.data / "config.json").write_text(json.dumps(conf))
        self.app = SimpleNamespace(data=self.data, store=store, records=records, config=lambda: conf)
        self.key_path = self.root / "private" / "backup.key"
        create_key(self.key_path)

    def mounted_target(self, remote):
        remote.mkdir(parents=True)
        mount = {"mountpoint": str(remote), "source": "fixture-remote", "fstype": "fixturefs"}
        self.app.config()["backup"]["offsite_mount"] = mount
        device = remote.stat().st_dev
        info = f"42 1 {os.major(device)}:{os.minor(device)} / {remote} rw - fixturefs fixture-remote rw\n"
        return patch("dsh_personal.backup_targets._read_mountinfo", return_value=info)

    def fill(self):
        a = self.app.store.ensure_activity("A3", "2026-10-21")
        received = self.app.store.submit(a["id"], "submission-fixture-1", "睡得一般")
        self.app.store.summarize_literal(a["id"])
        self.app.store.correct(a["id"], {"quality": "睡得较好"}, "测试用户更正")
        folder = self.data / "attachments"
        folder.mkdir()
        (folder / "attachment-test").write_bytes(b"fixture attachment bytes")
        with self.app.store.db(True) as db:
            db.execute("INSERT INTO attachments VALUES (?,?,?,?,?,?,?,?,?)", ("asset-test", a["id"], None, received["receipt"], "test.txt", "attachments/attachment-test", sha256(b"fixture attachment bytes").hexdigest(), len(b"fixture attachment bytes"), self.app.store.stamp()))
        native = self.data / "dsh" / "sessions"
        native.mkdir(parents=True)
        (native / "fixture-session.jsonl").write_text('{"type":"fixture","question":"pending"}\n')
        (self.data / "dsh" / "bridge-auth.json").write_text('{"launch_url":"http://private-secret"}')
        self.app.records.add_profile("旧偏好", "preference", confirmed=True, user_input="测试用户确认旧偏好")
        goal = self.app.records.put_growth("doctorate", "goal", "测试目标")
        self.app.records.put_growth("doctorate", "plan", "测试计划", parent_id=goal["item_id"])
        self.app.records.sync_activity(a["id"], "A3", "2026-10-21", ["message:" + received["receipt"]], 1, 1)
        return a, received

    def test_export_manifest_plain_readable_and_no_credentials(self):
        a, received = self.fill()
        (self.data / "account-auth.json").write_text('PRIVATE-ACCOUNT-AUTH-DATA')
        (self.data / "dsh" / "account-auth.json").write_text('PRIVATE-ACCOUNT-AUTH-DATA')
        (self.data / "dsh" / ".account-auth.interrupted").write_text('PRIVATE-ACCOUNT-TEMP-DATA')
        content = export_bundle(self.app)
        manifest = verify_bundle(content)
        self.assertEqual(manifest["format"], "dsh-personal-bundle")
        self.assertIn("SQLite backup API", manifest["native_consistency"])
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            self.assertIn("dsh/sessions/fixture-session.jsonl", archive.namelist())
            self.assertNotIn("dsh/bridge-auth.json", archive.namelist())
            self.assertFalse(any("account-auth" in name for name in archive.namelist()))
            self.assertNotIn(b"PRIVATE-ACCOUNT-AUTH-DATA", b"".join(archive.read(name) for name in archive.namelist()))
            config = archive.read("config.json").decode()
            self.assertNotIn("secret-test-key", config)
            self.assertNotIn("private-webhook-secret", config)
            self.assertEqual(json.loads(config)["model"]["max_tokens"], 8192)
            text = archive.read("diary.md").decode()
            self.assertIn("睡得一般", text)
            self.assertIn("睡得较好", text)
            self.assertIn(received["receipt"], text)
            self.assertIn("2026-10-21", text)
            self.assertIn("2026-10-22", text)
            self.assertIn("周索引", text)
            self.assertIn("attachments/attachment-test", text)
        self.assertIn("睡得较好", markdown_export(self.app))

    def test_native_writer_lock_refuses_unsafe_snapshot(self):
        native = self.data / "dsh"
        native.mkdir()
        with (native / "web-host.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaisesRegex(RuntimeError, "宿主仍在写入"):
                export_bundle(self.app)
            # A text-only export does not claim to copy the native active log.
            self.assertIn("对话原文与资料归档", markdown_export(self.app))

    def test_cross_process_style_maintenance_lock(self):
        with maintenance_lock(self.data):
            with self.assertRaisesRegex(RuntimeError, "等待超时"):
                with maintenance_lock(self.data, exclusive=True, timeout=0.05):
                    pass
        with maintenance_lock(self.data, exclusive=True, timeout=0.05):
            pass

    def test_encrypted_backup_restore_retains_pending_and_attachments(self):
        a, received = self.fill()
        result = backup(self.app, self.key_path)
        encrypted = Path(result["path"]).read_bytes()
        self.assertNotIn("睡得一般".encode(), encrypted)
        self.assertEqual(result["encrypted"], "AES-256-GCM")
        content = decrypt(encrypted, _key(self.key_path))
        ledger = json.loads(decrypt((self.data / "recovery-ledger/latest.ledger.enc").read_bytes(), _key(self.key_path)))
        report = restore_bundle(content, self.root / "restored", latest_ledger=ledger)
        self.assertTrue(report["isolated"])
        self.assertEqual(report["verified_attachments"], ["asset-test"])
        restored = Store(self.root / "restored/personal.sqlite", self.clock)
        self.assertEqual(restored.meta("isolated"), "true")
        self.assertEqual(restored.activity(a["id"])["messages"][1]["body"], "睡得一般")
        self.assertFalse(restored.tick())
        with restored.db() as db:
            self.assertFalse(db.execute("SELECT 1 FROM jobs WHERE status='pending'").fetchone())
        self.assertLess(report["duration_seconds"], 30)
        self.assertEqual((self.root / "restored/attachments/attachment-test").read_bytes(), b"fixture attachment bytes")
        self.assertEqual(json.loads((self.root / "restored/config.json").read_text())["model"], {})

    def test_backup_then_delete_then_restore_latest_ledger_blocks_resurrection(self):
        a, received = self.fill()
        self.app.records.add_profile("源自该睡眠原文的派生项", source_refs=["message:" + received["receipt"]])
        old = export_bundle(self.app)
        self.app.store.delete_message(received["receipt"], "测试用户删除原文")
        self.app.records.delete_source("message:" + received["receipt"])
        p = self.app.records.list_profile()[0]
        self.app.records.revise_profile(p["item_id"], p["version"], "更正偏好", content="新偏好")
        latest = recovery_ledger(self.app)
        report = restore_bundle(old, self.root / "after-delete", latest)
        restored = Store(self.root / "after-delete/personal.sqlite", self.clock)
        records = RecordsStore(self.root / "after-delete/personal.sqlite", self.clock)
        self.assertFalse(any(m["body"] == "睡得一般" for m in restored.journal()))
        self.assertEqual(restored.activity(a["id"])["activity"]["session_state"], "pending")
        self.assertIsNone(restored.activity(a["id"])["activity"]["session_id"])
        self.assertEqual(restored.meta("isolated"), "true")
        self.assertFalse(records.context())
        self.assertNotIn("源自该睡眠原文的派生项", records.render_markdown())
        self.assertNotIn("睡得一般", (self.root / "after-delete/diary.md").read_text())
        self.assertEqual(records.list_profile()[0]["content"], "新偏好")
        self.assertTrue(report["latest_ledger_supplied"])

    def test_latest_ledger_preserves_completed_usage_details_after_old_snapshot(self):
        rid = self.app.store.reserve(1000)
        started = self.app.store.stamp()
        with self.app.store.db(True) as db:
            db.execute("INSERT INTO usage_details VALUES(?,?,?,?,?,?,?,?,?,?,?)", (rid, "fixture-model", "max_completion_tokens", 512, 1, started, None, None, "{}", "started", "not_received"))
        old = export_bundle(self.app)
        self.app.store.settle(rid, 120, "synthetic-response")
        usage = {"prompt_tokens": 80, "completion_tokens": 40, "completion_tokens_details": {"reasoning_tokens": 10}}
        with self.app.store.db(True) as db:
            db.execute("UPDATE usage_details SET finished_at=?,reported_total=120,usage_json=?,response_state='completed',usage_state='valid' WHERE reservation_id=?", (started, json.dumps(usage), rid))
        latest = recovery_ledger(self.app)
        expected = latest["core"]["usage_details"][0]
        target = self.root / "usage-restored"
        restore_bundle(old, target, latest)
        restored = Store(target / "personal.sqlite", self.clock)
        with restored.db() as db:
            actual = dict(db.execute("SELECT * FROM usage_details WHERE reservation_id=?", (rid,)).fetchone())
            self.assertEqual(actual, expected)
            self.assertEqual(db.execute("SELECT actual FROM usage WHERE id=?", (rid,)).fetchone()[0], 120)
        self.assertEqual(json.loads(actual["usage_json"]), usage)
        self.assertEqual(actual["reported_total"], 120)

    def test_restore_deletion_cleans_context_native_and_exports_preserving_other_originals(self):
        marker = "SYNTHETIC-DELETED-DERIVATIVE"
        s, r = self.app.store, self.app.records
        a = s.natural("补记昨天的睡眠：" + marker, "restore-deletion-natural")["activity"]
        selected = next(m for m in s.activity(a["id"])["messages"] if m["role"] == "user")
        s.submit(a["id"], "independent-original", "Independent user original")
        s.correct(a["id"], {"quality": "Independent correction"}, "Independent user correction")
        other = s.ensure_activity("A2")
        s.submit(other["id"], "independent-other", "Other activity user original")
        profile = r.add_profile(marker, source_refs=["message:" + selected["id"]])
        sids = [str(uuid.uuid4()) for _ in range(3)]
        for sid, text in zip(sids, [marker, marker, "Independent native session"]):
            path = self.data / "dsh" / "sessions" / "_no-cwd" / sid / "session.v4.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"type": "session", "version": 4, "id": sid}) + "\n" + json.dumps({"type": "fixture", "text": text}) + "\n")
        kept_native = self.data / "dsh" / "sessions" / "_no-cwd" / sids[2] / "session.v4.jsonl"
        kept_bytes = kept_native.read_bytes()
        with s.db(True) as db:
            for activity, sid in ((a, sids[0]), (other, sids[1])):
                db.execute("UPDATE activities SET session_id=?,session_state='ready' WHERE id=?", (sid, activity["id"]))
                db.execute("INSERT INTO messages VALUES(?,?,NULL,'assistant',?,?,'native',NULL,NULL,0)", ("derived-" + sid, activity["id"], marker, s.stamp()))
            db.execute("INSERT INTO context_sources VALUES(?,?)", (sids[1], "profile:" + profile["item_id"]))
        task = s.create_task("Independent user task authorization", source_activity_id=a["id"])
        run = s.claim_task(task["id"]);s.deliver_task(task["id"], run["version"], marker)
        s.review_task(task["id"], run["version"], "accept", "Independent user review")
        r.put_growth("doctorate", "outcome", marker, source_refs=["task:" + task["id"]])
        attachment = self.data / "attachments" / "deleted.txt"
        attachment.parent.mkdir();attachment.write_text(marker)
        with s.db(True) as db:
            db.execute("INSERT INTO attachments VALUES(?,?,?,?,?,?,?,?,?)", ("deleted-asset", a["id"], None, selected["id"], "deleted.txt", "attachments/deleted.txt", sha256(marker.encode()).hexdigest(), len(marker), s.stamp()))
        old = export_bundle(self.app)
        with s.db(True) as db:
            db.execute("INSERT INTO deletion_context_refs VALUES(?,?)", (selected["id"], "profile:" + profile["item_id"]))
        affected = s.delete_message(selected["id"], "synthetic deletion")["affected"]
        for kind, key in (("message", "message_ids"), ("activity", "activity_ids"), ("task", "task_ids")):
            for ident in affected[key]:
                r.delete_source(kind + ":" + ident, preserve_source=kind in ("activity", "task"))
        protected = protect_ledgers(self.app, self.key_path)
        latest = json.loads(decrypt(Path(protected["path"]).read_bytes(), _key(self.key_path)))
        self.assertTrue(latest["core"]["deletion_context_refs"])
        target = self.root / "deep-deletion-restore"
        report = restore_bundle(old, target, latest)
        restored = Store(target / "personal.sqlite", self.clock)
        self.assertEqual(report["deletion_cleanup"]["attachments_removed"], ["deleted-asset"])
        self.assertFalse((target / "attachments/deleted.txt").exists())
        for sid in sids[:2]:
            self.assertFalse((target / "dsh/sessions/_no-cwd" / sid).exists())
            self.assertTrue(report["deletion_cleanup"]["native"]["sessions"][sid]["verified_absent"])
        self.assertEqual((target / kept_native.relative_to(self.data)).read_bytes(), kept_bytes)
        journal = json.dumps(restored.journal())
        self.assertIn("Independent user original", journal);self.assertIn("Other activity user original", journal)
        self.assertEqual(restored.activity(a["id"])["corrections"][0]["fields"]["quality"], "Independent correction")
        self.assertEqual(restored.task(task["id"])["authorization"], "Independent user task authorization")
        self.assertEqual(restored.task(task["id"])["reviews"][0]["input"], "Independent user review")
        self.assertEqual(restored.task(task["id"])["status"], "recovery_hold")
        self.assertFalse(any(marker.encode() in p.read_bytes() for p in target.rglob("*") if p.is_file()))

    def test_export_refuses_reintroduced_deleted_native_namespace(self):
        from runtime.native_cleanup import purge_sessions
        s = self.app.store
        a = s.ensure_activity("A3")
        message = s.submit(a["id"], "native-export-deletion", "SYNTHETIC-NATIVE-DELETE")
        sid = str(uuid.uuid4())
        path = self.data / "dsh/sessions/_no-cwd" / sid / "session.v4.jsonl"
        path.parent.mkdir(parents=True)
        raw = (json.dumps({"type": "session", "version": 4, "id": sid}) + "\n" + json.dumps({"fixture": "SYNTHETIC-NATIVE-DELETE"}) + "\n").encode()
        path.write_bytes(raw)
        with s.db(True) as db:db.execute("UPDATE activities SET session_id=?,session_state='ready' WHERE id=?", (sid, a["id"]))
        s.delete_message(message["receipt"], "synthetic deletion")
        with self.assertRaisesRegex(RuntimeError, "原生删除副本仍存在"):
            export_bundle(self.app)
        purge_sessions(self.data / "dsh", [sid])
        export_bundle(self.app)
        path.parent.mkdir(parents=True);path.write_bytes(raw)
        with self.assertRaisesRegex(RuntimeError, "原生删除副本仍存在"):
            export_bundle(self.app)

    def test_restore_without_latest_ledger_stays_isolated_and_requires_confirmation(self):
        self.fill()
        report = restore_bundle(export_bundle(self.app), self.root / "unknown")
        self.assertFalse(report["latest_ledger_supplied"])
        with self.assertRaises(ValueError):
            release_restore(self.root / "unknown", "确认")
        with self.assertRaises(ValueError):
            release_restore(self.root / "unknown", "", True, True)
        self.assertTrue(release_restore(self.root / "unknown", "测试核对：账本覆盖充分，动作已逐项核对", True, True)["released"])
        store = Store(self.root / "unknown/personal.sqlite", self.clock)
        self.assertEqual(store.meta("isolated"), "false")
        with store.db() as db:
            self.assertFalse(db.execute("SELECT 1 FROM jobs WHERE status='pending'").fetchone())

    def test_unknown_external_effects_do_not_replay(self):
        a, _ = self.fill()
        with self.app.store.db(True) as db:
            db.execute("INSERT INTO notifications VALUES (?,?,?,?,?,0,NULL,NULL,?)", ("notification-test", a["id"], "first", "pending", self.app.store.stamp(), self.app.store.stamp()))
        old = export_bundle(self.app)
        with self.app.store.db(True) as db:
            db.execute("UPDATE notifications SET state='accepted',receipt='202' WHERE id='notification-test'")
        latest = recovery_ledger(self.app)
        restore_bundle(old, self.root / "effects", latest)
        store = Store(self.root / "effects/personal.sqlite", self.clock)
        with store.db() as db:
            row = db.execute("SELECT * FROM notifications WHERE id='notification-test'").fetchone()
        self.assertEqual(row["state"], "accepted")
        self.assertEqual(row["receipt"], "202")
        self.assertEqual(store.meta("isolated"), "true")
        restore_bundle(old, self.root / "effects-unknown")
        unknown = Store(self.root / "effects-unknown/personal.sqlite", self.clock)
        with unknown.db() as db:
            row = db.execute("SELECT * FROM notifications WHERE id='notification-test'").fetchone()
        self.assertEqual(row["state"], "recovery_unknown")
        release_restore(self.root / "effects-unknown", "测试核对：缺少该回执，保持未知而不重放", True, True)
        with unknown.db() as db:
            self.assertFalse(db.execute("SELECT 1 FROM notifications WHERE state='pending'").fetchone())

    def test_verify_tamper_wrong_key_zip_slip_and_nonempty_destination(self):
        self.fill()
        payload = export_bundle(self.app)
        encrypted = encrypt(payload, _key(self.key_path))
        with self.assertRaises(ValueError):
            decrypt(encrypted[:-1] + bytes([encrypted[-1] ^ 1]), _key(self.key_path))
        with self.assertRaises(ValueError):
            decrypt(encrypted, b"x" * 32)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("../outside", "bad")
            archive.writestr("manifest.json", "{}")
        with self.assertRaises(ValueError):
            verify_bundle(stream.getvalue())
        with self.assertRaises(ValueError):
            restore_bundle(payload, self.data)
        with zipfile.ZipFile(io.BytesIO(payload)) as old:
            bad = io.BytesIO()
            with zipfile.ZipFile(bad, "w") as new:
                for name in old.namelist():
                    new.writestr(name, b"changed" if name == "diary.md" else old.read(name))
        with self.assertRaises(ValueError):
            verify_bundle(bad.getvalue())

    def test_retention_is_union_of_seven_days_four_weeks_six_months(self):
        records = []
        for month in range(1, 11):
            for day in range(1, 29):
                stamp = f"2026-{month:02}-{day:02}T00:00:00+00:00"
                records.append({"created_at": stamp, "filename": stamp + ".dshbackup"})
        retained = _retention(records)
        self.assertTrue(any("2026-05" in name for name in retained))
        self.assertFalse(any("2026-04" in name for name in retained))
        self.assertTrue(all(f"2026-10-{day}T00:00:00+00:00.dshbackup" in retained for day in range(22, 29)))
        self.assertLessEqual(len(retained), 17)

    def test_restore_failure_never_publishes_unisolated_snapshot(self):
        self.fill()
        payload = export_bundle(self.app)
        bad_ledger = {"format": "invalid", "as_of": "2099-01-01T00:00:00+00:00"}
        target = self.root / "failed-restore"
        with self.assertRaises(ValueError):
            restore_bundle(payload, target, bad_ledger)
        self.assertFalse(target.exists())

    def test_backup_key_cannot_be_archived_with_data(self):
        unsafe_key = self.data / "workspace" / "backup.key"
        create_key(unsafe_key)
        with self.assertRaisesRegex(ValueError, "密钥必须"):
            backup(self.app, unsafe_key)

    def test_configured_credential_file_inside_workspace_is_not_exported(self):
        folder = self.data / "workspace"
        folder.mkdir()
        secret = folder / "provider.key"
        secret.write_text("SYNTHETIC-CONFIGURED-CREDENTIAL")
        self.app.config()["model"]["api_key_file"] = str(secret)
        with zipfile.ZipFile(io.BytesIO(export_bundle(self.app))) as archive:
            self.assertNotIn("workspace/provider.key", archive.namelist())
            self.assertFalse(any(b"SYNTHETIC-CONFIGURED-CREDENTIAL" in archive.read(name) for name in archive.namelist()))

    def test_notification_token_file_excluded_without_redacting_token_budget(self):
        folder = self.data / "workspace"
        folder.mkdir()
        secret = folder / "notify-value.dat"
        secret.write_text("SYNTHETIC-NTFY-PUBLISHER-TOKEN")
        self.app.config()["notification"] = {"backend": "ntfy", "url": "http://192.0.2.10:18783",
            "topic": "dsh-activities", "token_file": str(secret)}
        with zipfile.ZipFile(io.BytesIO(export_bundle(self.app))) as archive:
            self.assertNotIn("workspace/notify-value.dat", archive.namelist())
            self.assertFalse(any(b"SYNTHETIC-NTFY-PUBLISHER-TOKEN" in archive.read(name) for name in archive.namelist()))
            config = json.loads(archive.read("config.json"))
            self.assertEqual(config["notification"]["token_file"], "<configure-separately>")
            self.assertEqual(config["model"]["max_tokens"], 8192)

    def test_slow_old_ledger_cannot_overwrite_newer_deletion(self):
        from dsh_personal import operations
        self.fill()
        paused, resume = threading.Event(), threading.Event()
        original_encrypt = operations.encrypt
        count_lock = threading.Lock()
        calls = [0]
        def slow_first(content, key):
            with count_lock:
                calls[0] += 1
                first = calls[0] == 1
            if first:
                paused.set()
                self.assertTrue(resume.wait(3))
            return original_encrypt(content, key)
        with patch.object(operations, "encrypt", side_effect=slow_first), ThreadPoolExecutor(max_workers=2) as pool:
            old = pool.submit(protect_ledgers, self.app, self.key_path)
            self.assertTrue(paused.wait(3))
            # Simulate a just-committed deletion using the same DB; the new
            # protection waits until the previous ordered publication ends.
            profile = self.app.records.list_profile()[0]
            self.app.records.delete_source("profile:" + profile["item_id"])
            newer = pool.submit(protect_ledgers, self.app, self.key_path)
            self.assertFalse(newer.done())
            resume.set()
            old.result(timeout=3); newer.result(timeout=3)
        ledger = json.loads(decrypt((self.data / "recovery-ledger/latest.ledger.enc").read_bytes(), _key(self.key_path)))
        self.assertTrue(ledger["records"]["tombstones"])

    def test_restored_current_ledger_scrubs_deleted_profile_text(self):
        self.app.records.add_profile("OLD-PROFILE-TO-DELETE", source_refs=["message:source-delete"])
        old = export_bundle(self.app)
        self.app.records.delete_source("message:source-delete")
        report = restore_bundle(old, self.root / "scrubbed-ledger", recovery_ledger(self.app))
        restored = Path(report["restored"])
        self.assertNotIn("OLD-PROFILE-TO-DELETE", (restored / "ledger.json").read_text())
        self.assertTrue((restored / "source-manifest.json").is_file())
        current = json.loads((restored / "manifest.json").read_text())
        self.assertEqual(current["files"]["ledger.json"]["sha256"], sha256((restored / "ledger.json").read_bytes()).hexdigest())

    def test_configured_backup_and_offsite_defaults_apply_to_timer_calls(self):
        self.fill()
        local = self.root / "configured-backups"
        remote = self.root / "configured-offsite-fixture"
        ledger_dir = self.root / "configured-ledger"
        self.app.config()["backup"].update({"directory": str(local), "offsite_dir": str(remote), "ledger_directory": str(ledger_dir)})
        with self.mounted_target(remote):
            result = backup(self.app, self.key_path)
        self.assertEqual(Path(result["path"]).parent, local)
        self.assertEqual((remote / Path(result["path"]).name).read_bytes(), Path(result["path"]).read_bytes())
        self.assertEqual((remote / "latest.ledger.enc").read_bytes(), (ledger_dir / "latest.ledger.enc").read_bytes())
        self.assertFalse(result["offsite_verified"])

    def test_manual_and_timer_backup_publish_one_consistent_retention_index(self):
        self.fill()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: backup(self.app, self.key_path), range(2)))
        self.assertNotEqual(results[0]["path"], results[1]["path"])
        index = json.loads((self.data / "backups/index.json").read_text())
        on_disk = {p.name for p in (self.data / "backups").glob("*.dshbackup")}
        self.assertEqual(on_disk, {item["filename"] for item in index})
        self.assertTrue(index)
        for item in index:
            self.assertEqual(sha256((self.data / "backups" / item["filename"]).read_bytes()).hexdigest(), item["sha256"])

    def test_ledger_protection_independent_of_full_backup(self):
        self.fill()
        with self.mounted_target(self.root / "second-target"):
            report = protect_ledgers(self.app, self.key_path, offsite_dir=self.root / "second-target")
        self.assertTrue(report["offsite_copy"])
        self.assertFalse(report["offsite_verified"])
        self.assertEqual((self.root / "second-target/latest.ledger.enc").read_bytes(), Path(report["path"]).read_bytes())
        self.assertFalse((self.data / "backups").exists())
        ledger = json.loads(decrypt(Path(report["path"]).read_bytes(), _key(self.key_path)))
        self.assertNotIn("睡得一般", json.dumps(ledger, ensure_ascii=False))
        self.assertIn("corrections", ledger["core"])

    def test_missing_offsite_preserves_local_snapshot_and_ledger_without_creating_target(self):
        remote = self.root / "missing-mount"
        self.app.config()["backup"]["offsite_dir"] = str(remote)
        result = backup(self.app, self.key_path)
        self.assertTrue(Path(result["path"]).is_file())
        self.assertTrue(Path(result["ledger"]["path"]).is_file())
        self.assertFalse(result["offsite_copy"])
        self.assertIsNotNone(result["offsite_error"])
        self.assertFalse(remote.exists())
        self.assertIsNone(self.app.store.meta("ledger_offsite_at"))

    def test_lost_mount_after_write_never_records_remote_success(self):
        remote = self.root / "remote"
        self.app.config()["backup"]["offsite_dir"] = str(remote)
        with self.mounted_target(remote) as reader:
            reader.side_effect = [reader.return_value, ""]
            result = protect_ledgers(self.app, self.key_path)
        self.assertTrue(Path(result["path"]).is_file())
        self.assertIsNotNone(result["offsite_error"])
        self.assertFalse(result["offsite_copy"])
        self.assertIsNone(self.app.store.meta("ledger_offsite_at"))
        # Loss discovered after publication can leave an encrypted artifact;
        # its existence must never be reported as verified remote protection.
        self.assertTrue((remote / "latest.ledger.enc").exists())

    def test_cli_signals_remote_failure_after_preserving_local_backup(self):
        self.app.config()["backup"]["offsite_dir"] = str(self.root / "missing-mount")
        from dsh_personal.operations import main
        with patch('sys.argv', ['operations', 'backup', '--data', str(self.data), '--key', str(self.key_path)]), \
                patch('dsh_personal.operations._app', return_value=self.app), patch('builtins.print'):
            with self.assertRaises(SystemExit) as caught:
                main()
        self.assertEqual(caught.exception.code, 1)
        self.assertTrue(list((self.data / 'backups').glob('*.dshbackup')))
        self.assertTrue((self.data / 'recovery-ledger/latest.ledger.enc').is_file())

    def test_ledger_and_backup_reject_keys_in_any_output_root(self):
        self.app.config()["backup"]["ledger_directory"] = str(self.key_path.parent)
        for operation in (backup, protect_ledgers):
            with self.subTest(operation=operation.__name__), self.assertRaisesRegex(ValueError, "密钥必须"):
                operation(self.app, self.key_path)
        self.assertFalse((self.key_path.parent / "latest.ledger.enc").exists())

    def test_mount_identity_is_not_exported_even_if_source_contains_secret(self):
        self.app.config()["backup"]["offsite_mount"] = {
            "mountpoint": "/fixture/remote", "source": "synthetic-private-mount-source", "fstype": "fixturefs"}
        with zipfile.ZipFile(io.BytesIO(export_bundle(self.app))) as archive:
            config = archive.read("config.json").decode()
            self.assertNotIn("synthetic-private-mount-source", config)
            self.assertIn("<configure-separately>", config)

    def test_checked_expanded_paths_are_the_actual_publication_paths(self):
        fake_home = self.root / "userhome"
        fake_home.mkdir()
        remote = fake_home / "remote"
        self.app.config()["backup"].update({"directory": "~/backups", "ledger_directory": "~/ledger", "offsite_dir": "~/remote"})
        original_expand = os.path.expanduser
        expand = lambda value: str(fake_home) if value == '~' else str(fake_home / value[2:]) if value.startswith('~/') else original_expand(value)
        with self.mounted_target(remote), patch('os.path.expanduser', side_effect=expand):
            report = backup(self.app, self.key_path)
        self.assertEqual(Path(report['path']).parent, fake_home / 'backups')
        self.assertEqual(Path(report['ledger']['path']).parent, fake_home / 'ledger')
        self.assertTrue(report['offsite_copy'])
        self.assertEqual((remote / Path(report['path']).name).read_bytes(), Path(report['path']).read_bytes())

    def test_both_local_artifacts_exist_before_first_remote_publication(self):
        remote = self.root / 'remote'
        self.app.config()['backup']['offsite_dir'] = str(remote)
        from dsh_personal.operations import _atomic_write
        remote_writes = []
        def publish(path, content, *args, **kwargs):
            if Path(path).parent == remote:
                self.assertTrue(list((self.data / 'backups').glob('*.dshbackup')))
                self.assertTrue((self.data / 'recovery-ledger/latest.ledger.enc').is_file())
                remote_writes.append(Path(path).name)
            return _atomic_write(path, content, *args, **kwargs)
        with self.mounted_target(remote), patch('dsh_personal.operations._atomic_write', side_effect=publish):
            result = backup(self.app, self.key_path)
        self.assertTrue(result['offsite_copy'])
        self.assertEqual(len(remote_writes), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
