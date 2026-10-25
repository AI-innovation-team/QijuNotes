#!/usr/bin/env python3
"""Select a compatible Node without modifying the user's global installation."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENT = 'Node ^22.19 or >=24'


class NodeRuntimeError(RuntimeError):
    pass


def node_version(binary: Path) -> str:
    try:
        result = subprocess.run([str(binary), '--version'], check=True,
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise NodeRuntimeError(f'Cannot run selected Node ({type(exc).__name__}); set DSH_NODE to {REQUIREMENT}.') from exc
    version = result.stdout.strip()
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', version)
    if not match:
        raise NodeRuntimeError(f'Selected executable did not return a stable Node version; requires {REQUIREMENT}.')
    major, minor, _ = map(int, match.groups())
    if not ((major == 22 and minor >= 19) or major >= 24):
        raise NodeRuntimeError(f'Node {version} is unsupported; requires {REQUIREMENT}.')
    return version


def resolve_node(root: Path = ROOT, environ: dict[str, str] | None = None) -> Path:
    """Explicit env, then project selection, then PATH; invalid choices fail closed."""
    env = os.environ if environ is None else environ
    selection = Path(root) / '.runtime/node-path'
    candidate = env.get('DSH_NODE', '').strip()
    if not candidate and selection.exists():
        try:
            candidate = selection.read_text().strip()
        except OSError as exc:
            raise NodeRuntimeError('Cannot read project .runtime/node-path; set DSH_NODE explicitly.') from exc
        if not candidate or '\n' in candidate or '\r' in candidate:
            raise NodeRuntimeError('Invalid project .runtime/node-path; rerun runtime/install.sh with DSH_NODE.')
    if not candidate:
        candidate = shutil.which('node', path=env.get('PATH', os.defpath)) or ''
    elif not Path(candidate).is_absolute():
        candidate = shutil.which(candidate, path=env.get('PATH', os.defpath)) or ''
    if not candidate:
        raise NodeRuntimeError(f'Node was not found; set DSH_NODE to {REQUIREMENT}.')
    binary = Path(candidate).expanduser().resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise NodeRuntimeError(f'Selected Node is not executable; set DSH_NODE to {REQUIREMENT}.')
    node_version(binary)
    return binary


def remember_node(binary: Path, root: Path = ROOT) -> None:
    """Atomically persist a selection in the project's ignored private directory."""
    directory = Path(root) / '.runtime'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.node-path-', dir=directory)
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(str(binary) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, directory / 'node-path')
    finally:
        Path(temporary).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--remember', action='store_true', help='Save selection for noninteractive service launches.')
    args = parser.parse_args()
    try:
        binary = resolve_node()
        if args.remember:
            remember_node(binary)
    except NodeRuntimeError as exc:
        parser.exit(1, str(exc) + '\n')
    print(binary)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
