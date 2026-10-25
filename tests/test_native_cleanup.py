from pathlib import Path
import fcntl
import json
import os
import tempfile
import unittest
import uuid

from runtime.native_cleanup import NativeCleanupError, purge_sessions


class NativeCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'dsh'
        self.home.mkdir()
        self.target = str(uuid.uuid4())
        self.other = str(uuid.uuid4())
        self.directory(self.target)
        self.directory(self.other)

    def directory(self, sid, parent=None, generations=(4,)):
        directory = self.home / 'sessions' / '--fixture--' / sid
        directory.mkdir(parents=True, exist_ok=True)
        for version in generations:
            header = {'type': 'session', 'version': version, 'id': sid,
                      'createdAt': 1, 'isSeeded': bool(parent), 'delegationDepth': 0}
            if parent:
                header['parentSession'] = parent
            (directory / f'session.v{version}.jsonl').write_text(json.dumps(header) + '\n' + json.dumps({'fixture': sid}) + '\n')
        (directory / 'session.lock').touch()
        return directory

    def files(self):
        return {p.relative_to(self.home).as_posix(): p.read_bytes() for p in self.home.rglob('*') if p.is_file() and p.name != 'web-host.lock'}

    def test_explicit_target_generations_and_cache_only_and_idempotency(self):
        directory = self.directory(self.target, generations=(3, 4))
        cache = self.home / 'storages/session_projcache/sessions'
        cache.mkdir(parents=True)
        for name in [self.target + '.json', self.target + '.json.bak.123', self.other + '.json']:
            (cache / name).write_text('fixture cache ' + name)
        legacy = self.home / 'storages/session_projcache.json'
        legacy.write_text(json.dumps({'unit': {'name': 'session_projcache', 'version': 6}, 'global': None,
                                     'tables': {'sessions': {self.target: {'fixture': 1}, self.other: {'fixture': 2}}}}))
        before = self.files()
        result = purge_sessions(self.home, [self.target])
        self.assertTrue(result['sessions'][self.target]['verified_absent'])
        self.assertEqual(result['sessions'][self.target]['generations_removed'], 2)
        self.assertFalse(directory.exists())
        for name, data in before.items():
            if self.other in name:
                self.assertEqual((self.home / name).read_bytes(), data)
        self.assertEqual(json.loads(legacy.read_text())['tables']['sessions'], {self.other: {'fixture': 2}})
        self.assertEqual(purge_sessions(self.home, [self.target])['sessions'][self.target]['files_removed'], 0)

    def test_live_host_and_direct_session_writer_refused_without_changes(self):
        for path in [self.home / 'web-host.lock', self.home / 'sessions' / '--fixture--' / self.target / 'session.lock']:
            with path.open('a') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                before = self.files()
                with self.assertRaisesRegex(NativeCleanupError, '持锁|仍在写入'):
                    purge_sessions(self.home, [self.target])
                self.assertEqual(self.files(), before)

    def test_dry_run_detects_restored_namespace_without_mutating(self):
        before = self.files()
        result = purge_sessions(self.home, [self.target], dry_run=True)
        self.assertEqual(self.files(), before)
        self.assertFalse(result['sessions'][self.target]['verified_absent'])
        self.assertEqual(result['sessions'][self.target]['would_remove_files'], 2)
        self.assertEqual(result['sessions'][self.target]['files_removed'], 0)
        purge_sessions(self.home, [self.target])
        self.assertTrue(purge_sessions(self.home, [self.target], dry_run=True)['sessions'][self.target]['verified_absent'])
        self.directory(self.target)
        self.assertFalse(purge_sessions(self.home, [self.target], dry_run=True)['sessions'][self.target]['verified_absent'])

    def test_borrowed_lock_is_not_closed_or_unlocked(self):
        path = self.home / 'web-host.lock'
        with path.open('a') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            purge_sessions(self.home, [self.target], lock_fd=held.fileno())
            os.fstat(held.fileno())
            with path.open('a') as competing:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (self.home / 'wrong.lock').open('a') as wrong:
            with self.assertRaisesRegex(NativeCleanupError, '锁不属于'):
                purge_sessions(self.home, [self.other], lock_fd=wrong.fileno())

    def test_uuid_list_and_no_writer_bypass(self):
        before = self.files()
        for bad in ['../other', str(uuid.uuid4()).upper(), '']:
            with self.assertRaises(NativeCleanupError):
                purge_sessions(self.home, [bad])
        with self.assertRaises(NativeCleanupError):
            purge_sessions(self.home, self.target)
        with self.assertRaises(NativeCleanupError):
            purge_sessions(self.home, [self.target], writer_must_be_stopped=False)
        self.assertEqual(self.files(), before)

    def test_unknown_layout_header_or_generation_fails_before_any_deletion(self):
        directory = self.directory(self.target)
        invalid = directory / 'session.v5.jsonl'
        invalid.write_text('unknown future version')
        before = self.files()
        with self.assertRaisesRegex(NativeCleanupError, '未知'):
            purge_sessions(self.home, [self.target])
        self.assertEqual(self.files(), before)
        invalid.unlink()
        (directory / 'session.v4.jsonl').write_text(json.dumps({'type': 'session', 'version': 4, 'id': self.other}) + '\n')
        before = self.files()
        with self.assertRaisesRegex(NativeCleanupError, '身份'):
            purge_sessions(self.home, [self.target])
        self.assertEqual(self.files(), before)

    def test_symlink_or_hardlink_never_followed(self):
        directory = self.home / 'sessions' / '--fixture--' / self.target
        outside = Path(self.temp.name) / 'outside'
        outside.write_text('do not touch')
        link = directory / 'external'
        link.symlink_to(outside)
        with self.assertRaisesRegex(NativeCleanupError, '符号链接'):
            purge_sessions(self.home, [self.target])
        self.assertEqual(outside.read_text(), 'do not touch')
        link.unlink()
        os.link(outside, link)
        with self.assertRaisesRegex(NativeCleanupError, '硬链接'):
            purge_sessions(self.home, [self.target])
        self.assertEqual(outside.read_text(), 'do not touch')

    def test_unowned_fork_prevents_partial_erase(self):
        self.directory(self.other, parent=self.target)
        before = self.files()
        with self.assertRaisesRegex(NativeCleanupError, '未授权子会话'):
            purge_sessions(self.home, [self.target])
        self.assertEqual(self.files(), before)
        result = purge_sessions(self.home, [self.target, self.other])
        self.assertTrue(all(v['verified_absent'] for v in result['sessions'].values()))

    def test_unknown_shared_media_and_domain_fail_closed(self):
        media = self.home / 'attachments/v1/unknown'
        media.parent.mkdir(parents=True)
        media.write_bytes(b'shared original')
        before = self.files()
        with self.assertRaisesRegex(NativeCleanupError, '共享原生附件'):
            purge_sessions(self.home, [self.target])
        self.assertEqual(self.files(), before)
        media.unlink()
        store = self.home / 'storages/unknown.json'
        store.parent.mkdir(exist_ok=True)
        store.write_text('{}')
        before = self.files()
        with self.assertRaisesRegex(NativeCleanupError, '未知原生 storage'):
            purge_sessions(self.home, [self.target])
        self.assertEqual(self.files(), before)


if __name__ == '__main__':
    unittest.main()
