import importlib.util
from pathlib import Path
import subprocess
import tempfile
import os
import unittest

spec = importlib.util.spec_from_file_location('publish_web', Path(__file__).resolve().parents[1]/'scripts/publish-web.py')
publish = importlib.util.module_from_spec(spec); spec.loader.exec_module(publish)


class WebReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.repo = self.root/'repo'; self.repo.mkdir()
        self.git('init', '-q')
        (self.repo/'web').mkdir()
        for name in ['index.html', 'app.js', 'style.css']:
            (self.repo/'web'/name).write_text('committed '+name)
        self.commit()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.DEVNULL)

    def commit(self):
        self.git('add', 'web')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')

    def test_only_committed_assets_are_published_and_repeat_is_unchanged(self):
        (self.repo/'web/app.js').write_text('unreviewed working edit')
        target, hashes = publish.snapshot(self.repo, 'HEAD', self.root/'releases')
        self.assertEqual((target/'app.js').read_text(), 'committed app.js')
        again, same = publish.snapshot(self.repo, 'HEAD', self.root/'releases')
        self.assertEqual((again, same), (target, hashes))
        self.assertEqual((target/'app.js').stat().st_mode & 0o777, 0o600)

    def test_changed_existing_release_is_never_overwritten(self):
        target, _ = publish.snapshot(self.repo, 'HEAD', self.root/'releases')
        (target/'app.js').write_text('changed')
        with self.assertRaises(ValueError): publish.snapshot(self.repo, 'HEAD', self.root/'releases')
        self.assertEqual((target/'app.js').read_text(), 'changed')

    def test_symlink_asset_is_rejected_before_publication(self):
        (self.repo/'web/link').symlink_to('/etc/passwd'); self.commit()
        with self.assertRaises(ValueError): publish.snapshot(self.repo, 'HEAD', self.root/'releases')
        self.assertFalse((self.root/'releases').exists())

    def test_relative_output_is_resolved_before_service_activation(self):
        original = Path.cwd()
        try:
            os.chdir(self.root)
            target, _ = publish.snapshot(self.repo, 'HEAD', 'relative-release')
        finally:
            os.chdir(original)
        self.assertTrue(target.is_absolute())
        self.assertEqual(target.parent, self.root/'relative-release')

    def test_service_override_changes_only_web_path_and_rejects_expansion(self):
        source = '[Service]\nExecStart=\nExecStart=/usr/bin/python3 -m dsh_personal.server --data "/data unchanged" --private-host 100.0.0.1 --web-root "/prior"\n'
        self.assertEqual(publish.release_override(source, '/new release/sha'), source.replace('/prior', '/new release/sha'))
        for path in ['relative', '/bad$HOME', '/bad%h', '/bad\\n', '/bad\rpath', '/bad\npath', '/bad"path']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                publish.release_override(source, path)
        with self.assertRaises(ValueError):
            publish.release_override('different service', '/new')
