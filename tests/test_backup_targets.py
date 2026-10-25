import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dsh_personal.backup_targets import validate_backup_targets


class BackupTargetsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dsh-backup-target-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.key = self.root / "private" / "key"
        self.key.parent.mkdir()
        self.key.write_text("synthetic key bytes must not be read")
        self.backup = self.data / "backups"
        self.ledger = self.data / "ledger"
        self.mount = self.root / "mounted target"
        self.mount.mkdir()
        self.remote = self.mount / "backups"
        self.remote.mkdir()
        self.source = "user:SECRET-MOUNT-CREDENTIAL@example.invalid:/vault"
        self.expected = {"mountpoint": str(self.mount), "source": self.source,
                         "fstype": "fuse.sshfs"}

    @staticmethod
    def escape(value):
        return str(value).replace("\\", "\\134").replace(" ", "\\040").replace("\t", "\\011")

    def mountinfo(self, mountpoint=None, source=None, fstype=None, device=None):
        dev = self.remote.stat().st_dev
        device = device or f"{os.major(dev)}:{os.minor(dev)}"
        return ("1 0 0:1 / / rw - ext4 /dev/fixture rw\n"
                f"42 1 {device} / {self.escape(mountpoint or self.mount)} rw shared:7 - "
                f"{fstype or self.expected['fstype']} {self.escape(source or self.source)} rw\n")

    def check(self, **changes):
        args = dict(data=self.data, key_path=self.key, local_backup=self.backup,
                    local_ledger=self.ledger, offsite_dir=self.remote,
                    expected_mount=self.expected)
        args.update(changes)
        return validate_backup_targets(**args)

    def test_local_only_does_not_create_outputs_read_keys_or_require_mountinfo(self):
        with patch("dsh_personal.backup_targets._read_mountinfo", side_effect=AssertionError("unused")), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("no secret reads")):
            result = self.check(offsite_dir=None, expected_mount=None)
        self.assertTrue(result["key_separated"])
        self.assertFalse(result["offsite_configured"])
        self.assertFalse(self.backup.exists())
        self.assertFalse(self.ledger.exists())

    def test_key_inside_data_or_each_output_root_is_rejected(self):
        for root in (self.data, self.backup, self.ledger, self.remote):
            with self.subTest(root=root), self.assertRaisesRegex(ValueError, "密钥必须"):
                self.check(key_path=root / "key")

    def test_key_separation_resolves_symlink_aliases(self):
        inside = self.data / "nested"
        inside.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(inside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "密钥必须"):
            self.check(key_path=alias / "key", offsite_dir=None)
        output_alias = self.root / "output-alias"
        output_alias.symlink_to(self.key.parent, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "密钥必须"):
            self.check(local_ledger=output_alias, offsite_dir=None)

    def test_offsite_overlaps_data_and_local_roots_in_both_directions(self):
        for root in (self.data, self.backup, self.ledger):
            root.mkdir(parents=True, exist_ok=True)
            for remote in (root, root / "nested", root.parent):
                with self.subTest(root=root, remote=remote), self.assertRaises(ValueError):
                    self.check(offsite_dir=remote)

    def test_missing_target_is_not_created(self):
        absent = self.mount / "missing"
        with self.assertRaisesRegex(ValueError, "预先存在"):
            self.check(offsite_dir=absent)
        self.assertFalse(absent.exists())

    def test_existing_local_directory_without_expected_identity_is_rejected(self):
        for expected in (None, {}, {"mountpoint": str(self.mount)},
                         {**self.expected, "source": ""}, {**self.expected, "fstype": True}):
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, "预期挂载"):
                self.check(expected_mount=expected)

    def test_valid_declared_mount_accepts_escaped_paths_without_disclosing_source(self):
        with patch("dsh_personal.backup_targets._read_mountinfo", return_value=self.mountinfo()), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("no secret reads")):
            result = self.check()
        self.assertTrue(result["mount_matches_expected"])
        self.assertFalse(result["offsite_verified"])
        self.assertTrue(result["checks_read_only"])
        self.assertNotIn(self.source, json.dumps(result))
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertFalse(self.backup.exists())

    def test_root_mount_cannot_relabel_an_ordinary_directory_as_offsite(self):
        dev = self.remote.stat().st_dev
        info = f"1 0 {os.major(dev)}:{os.minor(dev)} / / rw - ext4 /dev/fixture rw\n"
        expected = {"mountpoint": "/", "source": "/dev/fixture", "fstype": "ext4"}
        with patch("dsh_personal.backup_targets._read_mountinfo", return_value=info):
            with self.assertRaisesRegex(ValueError, "系统根挂载"):
                self.check(expected_mount=expected)

    def test_unmounted_existing_mountpoint_fails_instead_of_falling_back_locally(self):
        with patch("dsh_personal.backup_targets._read_mountinfo", return_value="1 0 0:1 / / rw - ext4 /dev/fixture rw\n"):
            with self.assertRaisesRegex(ValueError, "不匹配"):
                self.check()
        self.assertEqual(list(self.remote.iterdir()), [])

    def test_changed_mount_source_or_type_fails_with_redacted_errors(self):
        for content in (self.mountinfo(source="different-SECRET-source"),
                        self.mountinfo(fstype="different-fs")):
            with self.subTest(content=content), patch("dsh_personal.backup_targets._read_mountinfo", return_value=content):
                with self.assertRaises(ValueError) as caught:
                    self.check()
                self.assertNotIn("SECRET", str(caught.exception))
                self.assertNotIn(self.source, str(caught.exception))

    def test_nested_mount_cannot_hide_beneath_expected_parent(self):
        content = self.mountinfo() + (f"43 42 0:99 / {self.escape(self.remote)} rw - tmpfs other rw\n")
        with patch("dsh_personal.backup_targets._read_mountinfo", return_value=content):
            with self.assertRaisesRegex(ValueError, "不匹配"):
                self.check()

    def test_symlink_target_or_mountpoint_is_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.mount, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            self.check(offsite_dir=alias / "backups")
        with self.assertRaisesRegex(ValueError, "符号链接"):
            self.check(expected_mount={**self.expected, "mountpoint": str(alias)})

    def test_target_outside_declared_mount_and_device_mismatch_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "不在声明"):
            self.check(expected_mount={**self.expected, "mountpoint": str(self.data)})
        with patch("dsh_personal.backup_targets._read_mountinfo", return_value=self.mountinfo(device="999:999")):
            with self.assertRaisesRegex(ValueError, "设备"):
                self.check()

    def test_mount_table_failure_or_ambiguous_mount_fails_closed(self):
        for value in ("", "malformed\n", self.mountinfo() + self.mountinfo()):
            with self.subTest(value=value), patch("dsh_personal.backup_targets._read_mountinfo", return_value=value):
                with self.assertRaises(ValueError):
                    self.check()
        with patch("dsh_personal.backup_targets._read_mountinfo", side_effect=OSError("SECRET-detail")):
            with self.assertRaises(ValueError) as caught:
                self.check()
            self.assertNotIn("SECRET", str(caught.exception))

    def test_rechecking_detects_mount_disappearance_between_publication_steps(self):
        with patch("dsh_personal.backup_targets._read_mountinfo", side_effect=[self.mountinfo(), ""]):
            self.assertTrue(self.check()["mount_matches_expected"])
            with self.assertRaisesRegex(ValueError, "挂载身份"):
                self.check()


if __name__ == "__main__":
    unittest.main()
