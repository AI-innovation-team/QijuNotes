#!/usr/bin/env python3
"""Publish committed web assets, leaving working edits outside the running site."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def snapshot(repo, ref, destination):
    commit = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', '--verify', ref + '^{commit}'], text=True).strip()
    entries = subprocess.check_output(['git', '-C', str(repo), 'ls-tree', '-rz', commit, '--', 'web']).split(b'\0')
    files = {}
    for entry in filter(None, entries):
        meta, name = entry.split(b'\t', 1)
        mode, kind, blob = meta.split()
        path = Path(name.decode()).relative_to('web')
        if mode not in (b'100644', b'100755') or kind != b'blob' or '..' in path.parts:
            raise ValueError('Release web assets must be regular files')
        files[path] = subprocess.check_output(['git', '-C', str(repo), 'cat-file', 'blob', blob.decode()])
    if not {'index.html', 'app.js', 'style.css'}.issubset({str(p) for p in files}):
        raise ValueError('Release lacks required web entry assets')
    destination = Path(destination).expanduser().resolve(); destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = destination / commit
    if target.exists():
        if target.is_symlink() or {p.relative_to(target) for p in target.rglob('*') if p.is_file()} != set(files):
            raise ValueError('Existing release differs; refusing to overwrite')
        if any((target/p).is_symlink() or (target/p).read_bytes() != data for p, data in files.items()):
            raise ValueError('Existing release differs; refusing to overwrite')
    else:
        temporary = Path(tempfile.mkdtemp(prefix='.web-release-', dir=destination))
        try:
            for path, data in files.items():
                output = temporary/path; output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                output.write_bytes(data); output.chmod(0o600)
            os.rename(temporary, target)
        finally:
            if temporary.exists(): shutil.rmtree(temporary)
    return target, {str(p): hashlib.sha256(data).hexdigest() for p, data in files.items()}


def release_override(content, path):
    if not Path(path).is_absolute() or any(c in str(path) for c in ['"', '\n', '\r', '%', '$', '\\']):
        raise ValueError('Unsupported release path')
    if ' -m dsh_personal.server ' not in content or content.count('--web-root ') != 1:
        raise ValueError('Unexpected project service override; inspect before activation')
    updated, count = re.subn(r'--web-root "[^"\n]+"', lambda _: '--web-root "'+str(path)+'"', content)
    if count != 1: raise ValueError('Unexpected web release argument')
    return updated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit', default='HEAD')
    parser.add_argument('--output', default=str(Path.home()/'.local/share/dsh-personal-web-releases'))
    parser.add_argument('--activate', action='store_true', help='Update existing project-only web release drop-in and restart app')
    args = parser.parse_args()
    path, hashes = snapshot(ROOT, args.commit, args.output)
    if args.activate:
        head = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
        if path.name != head: raise ValueError('Activation requires the checked-out commit; source and assets must agree')
        override = Path.home()/'.config/systemd/user/dsh-personal.service.d/20-web-release.conf'
        updated = release_override(override.read_text(), path)
        fd, tmp = tempfile.mkstemp(prefix='.web-release-', dir=override.parent)
        try:
            with os.fdopen(fd, 'w') as f: f.write(updated); f.flush(); os.fsync(f.fileno())
            os.replace(tmp, override)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', '--user', 'restart', 'dsh-personal.service'], check=True)
    print(json.dumps({'source_commit': path.name, 'web_root': str(path), 'activated': args.activate, 'sha256': hashes}, indent=2))


if __name__ == '__main__': main()
