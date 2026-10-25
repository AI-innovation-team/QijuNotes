"""Exercise the publication boundary with committed private fixture files."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('qiju_export', ROOT / 'scripts/export-public.py')
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


class PublicExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'private'
        self.root.mkdir()
        self.out = Path(self.tmp.name) / 'public'
        self.git('init', '-q')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        baseline = 'plans/dsh_codex_taskpack_v1.1'
        shutil.copytree(ROOT / baseline, self.root / baseline)
        self.files = {
            'README.md': '# Fixture', 'dsh_personal/server.py': '# Source',
            'web/app.js': '// Source', 'runtime/npm/package-lock.json': '{}',
            '.gitignore': '', 'AGENTS.md': 'PRIVATE BACKGROUND',
            'progress/PROGRESS.md': 'PRIVATE DEPLOYMENT', 'evidence/raw.json': 'PRIVATE MODEL OUTPUT',
            'docs/USER_INPUTS.md': 'PRIVATE MATERIAL', 'runtime/config.json': 'PRIVATE CONFIG',
            'runtime/phone.json': 'PRIVATE PASSWORD', 'runtime/backup.key': 'PRIVATE KEY',
            'runtime/.env': 'PRIVATE ENV', 'runtime/local.sqlite-wal': 'PRIVATE WAL',
            'runtime/certificate.p12': 'PRIVATE CERTIFICATE', 'runtime/node_modules/pkg/index.js': 'DEPENDENCY',
            'output/photo.png': 'PRIVATE IMAGE', 'runtime/study_sources.json': '{"public":true}',
        }
        for name, value in self.files.items():
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(value)
        self.git('add', '.')
        self.git('commit', '-qm', 'Fixture source and private records')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.DEVNULL)

    def test_source_export_omits_private_records_and_git_history(self):
        (self.root / 'dsh_personal/server.py').write_text('# Uncommitted change')
        with patch.object(exporter, 'ROOT', self.root):
            report = exporter.export(self.out)
        self.assertFalse((self.out / '.git').exists())
        self.assertEqual((self.out / 'dsh_personal/server.py').read_text(), '# Source')
        self.assertTrue((self.out / 'runtime/study_sources.json').exists())
        for name, value in self.files.items():
            if value.startswith(('PRIVATE', 'DEPENDENCY')):
                self.assertFalse((self.out / name).exists() and value in (self.out / name).read_text(), name)
        ledger = json.loads((self.out / 'progress/ACCEPTANCE.json').read_text())
        checks = [s for c in ledger['cases'] for s in c['subchecks']]
        self.assertEqual(len(checks), 78)
        self.assertTrue(all(s['status'] == 'NOT_RUN' and not s['evidence'] and s['remaining_conditions'] for s in checks))
        self.assertEqual(ledger['gate_results'], {'CORE': False, 'FULL': False})
        self.assertFalse(report['private_history_exported'])

    def test_existing_destination_is_never_overwritten(self):
        self.out.mkdir()
        marker = self.out / 'keep.txt'
        marker.write_text('keep')
        with patch.object(exporter, 'ROOT', self.root), self.assertRaises(ValueError):
            exporter.export(self.out)
        self.assertEqual(marker.read_text(), 'keep')

    def test_tracked_symlink_is_rejected_before_copy(self):
        (self.root / 'web/outside.txt').symlink_to('/tmp/private-source')
        self.git('add', 'web/outside.txt')
        self.git('commit', '-qm', 'Fixture unsafe symlink')
        with patch.object(exporter, 'ROOT', self.root), self.assertRaises(ValueError):
            exporter.export(self.out)
        self.assertFalse(self.out.exists())
