import os
from pathlib import Path
import stat
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from runtime.node_runtime import NodeRuntimeError, node_version, remember_node, resolve_node


class NodeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def executable(self, name='node', version='v24.21.0'):
        binary = self.root / name
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text('#!/bin/sh\nprintf "%s\\n" "' + version + '"\n')
        binary.chmod(0o700)
        return binary

    def test_stable_version_boundaries(self):
        for version, supported in [('v22.18.0', False), ('v22.19.0', True), ('v22.22.1', True),
                                   ('v23.9.0', False), ('v24.0.0', True), ('v25.0.0', True),
                                   ('v24.0.0-rc.1', False), ('not-node', False)]:
            with self.subTest(version=version):
                binary = self.executable(version=version)
                if supported:
                    self.assertEqual(node_version(binary), version)
                else:
                    with self.assertRaises(NodeRuntimeError):
                        node_version(binary)

    def test_selection_precedence_and_noninteractive_launch(self):
        explicit = self.executable('explicit/node')
        persisted = self.executable('persisted/node')
        self.executable('path/node')
        remember_node(persisted, self.root)
        env = {'DSH_NODE': str(explicit), 'PATH': str(self.root / 'path')}
        self.assertEqual(resolve_node(self.root, env), explicit)
        self.assertEqual(resolve_node(self.root, {'PATH': ''}), persisted)

    def test_path_fallback_and_named_explicit_node(self):
        binary = self.executable('bin/node')
        self.assertEqual(resolve_node(self.root, {'PATH': str(binary.parent)}), binary)
        self.assertEqual(resolve_node(self.root, {'PATH': str(binary.parent), 'DSH_NODE': 'node'}), binary)

    def test_invalid_explicit_or_persisted_selection_does_not_fallback(self):
        binary = self.executable('bin/node')
        remember_node(self.root / 'missing-node', self.root)
        env = {'PATH': str(binary.parent)}
        with self.assertRaises(NodeRuntimeError):
            resolve_node(self.root, env)
        remember_node(binary, self.root)
        with self.assertRaises(NodeRuntimeError):
            resolve_node(self.root, dict(env, DSH_NODE=str(self.root / 'absent')))

    def test_persisted_selection_is_private_atomic_and_replaced(self):
        first = self.executable('first/node')
        second = self.executable('second/node')
        remember_node(first, self.root)
        remember_node(second, self.root)
        selection = self.root / '.runtime/node-path'
        self.assertEqual(selection.read_text(), str(second) + '\n')
        self.assertEqual(stat.S_IMODE(selection.stat().st_mode), 0o600)
        self.assertEqual(list(selection.parent.iterdir()), [selection])

    def test_empty_or_multiline_selection_fails(self):
        selection = self.root / '.runtime/node-path'
        selection.parent.mkdir()
        for content in ['', '/one\n/two\n']:
            with self.subTest(content=content):
                selection.write_text(content)
                with self.assertRaisesRegex(NodeRuntimeError, 'Invalid project'):
                    resolve_node(self.root, {'PATH': ''})

    def test_version_probe_failure_is_bounded_and_safe(self):
        with patch('runtime.node_runtime.subprocess.run', side_effect=subprocess.TimeoutExpired('node', 10)):
            with self.assertRaisesRegex(NodeRuntimeError, 'Cannot run selected Node'):
                node_version(Path('/fixture/node'))

    def test_installer_remembers_only_after_successful_project_install(self):
        repo = Path(__file__).resolve().parents[1]
        for exit_code in (1, 0):
            with self.subTest(exit_code=exit_code):
                project = self.root / ('project-' + str(exit_code))
                runtime = project / 'runtime'
                (runtime / 'npm').mkdir(parents=True)
                for name in ('install.sh', 'node_runtime.py'):
                    shutil.copyfile(repo / 'runtime' / name, runtime / name)
                binary = self.executable('selected-' + str(exit_code) + '/node')
                npm = binary.parent / 'npm'
                npm.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" > "$INSTALL_LOG"\nexit ' + str(exit_code) + '\n')
                npm.chmod(0o700)
                log = project / 'install-log'
                env = dict(os.environ, DSH_NODE=str(binary), INSTALL_LOG=str(log))
                result = subprocess.run(['bash', str(runtime / 'install.sh')], env=env,
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, exit_code, result.stderr)
                self.assertEqual(log.read_text().strip(), str(runtime / 'npm'))
                selection = project / '.runtime/node-path'
                self.assertEqual(selection.exists(), exit_code == 0)
                if exit_code == 0:
                    self.assertEqual(selection.read_text().strip(), str(binary))

    def test_compatibility_probe_requires_explicit_old_installation(self):
        repo = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env.pop('DSH_OLD_RUNTIME', None)
        result = subprocess.run([sys.executable, str(repo / 'runtime/native_version_probe.py')],
                                capture_output=True, text=True, env=env, timeout=15)
        self.assertEqual(result.returncode, 2)
        self.assertIn('Supply --old-runtime or DSH_OLD_RUNTIME', result.stderr)


if __name__ == '__main__':
    unittest.main()
