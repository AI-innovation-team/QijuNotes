#!/usr/bin/env python3
"""Export committed source plus fresh templates, without private deployment history."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PREFIXES = ('dsh_personal/', 'runtime/', 'scripts/', 'tests/', 'web/', 'docs/', 'plans/', '.github/')
ROOT_FILES = {'.gitignore', 'README.md', 'requirements.txt', 'THIRD_PARTY.md'}
EXCLUDED = {'docs/USER_INPUTS.md'}
DENIED_PARTS = {'.git', '.runtime', '.data', 'node_modules', '__pycache__', '.venv'}
DENIED_NAMES = {'config.json', 'account-auth.json', 'phone.json', 'credentials.json',
                'credentials.txt', 'access-token', 'gateway-key', 'runtime.env'}
DENIED_SUFFIXES = ('.pyc', '.sqlite', '.sqlite3', '.db', '.key', '.pem', '.enc', '.dshbackup', '.apk', '.env', '.p12', '.pfx', '.db-wal', '.db-shm')
AGENTS = '''# QijuNotes development

Product baseline: plans/dsh_codex_taskpack_v1.1/SPEC_v1.1.md.
Keep A1–A3 daily unless the user explicitly changes their schedule. Preserve original business dates, actual submissions, ongoing conversations, durable receipts and user-only versioned acceptance.
Use project-owned isolated runtime and data. Never alter unrelated services or user-global configuration. Never commit credentials, personal conversations, raw model output or real deployment screenshots.
One DSH Web writer and one personal daily scheduler. SQLite transactions own persistence and invariants; prompts do not grant permissions.
Test in temporary directories with synthetic fixtures. Distinguish code tests, actual model calls, real devices, elapsed observation and user acceptance.
The public progress directory starts with unexecuted templates. Keep actual private deployment records locally; do not publish them through this source snapshot.
'''

def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args])

def allowed(path):
    p = Path(path)
    return (path not in EXCLUDED and (path in ROOT_FILES or path.startswith(PREFIXES))
            and not any(part in DENIED_PARTS for part in p.parts)
            and p.name not in DENIED_NAMES and not p.name.startswith('.env') and '.sqlite' not in p.name
            and not path.endswith(DENIED_SUFFIXES))

def export(destination, revision='HEAD'):
    destination = Path(destination).expanduser().absolute()
    if destination.exists():
        raise ValueError('Destination must not exist; choose a fresh source-only directory.')
    commit = git('rev-parse', '--verify', revision + '^{commit}').decode().strip()
    entries = git('ls-tree', '-rz', commit).split(b'\0')
    selected = []
    for raw in entries:
        if not raw:
            continue
        header, name = raw.split(b'\t', 1)
        mode, kind, oid = header.decode().split()
        path = name.decode()
        if not allowed(path):
            continue
        if kind != 'blob' or mode not in ('100644', '100755') or '..' in Path(path).parts:
            raise ValueError('Unsupported public source entry: ' + path)
        selected.append((mode, oid, path))
    required = {'README.md', 'dsh_personal/server.py', 'web/app.js', 'runtime/npm/package-lock.json',
                'plans/dsh_codex_taskpack_v1.1/acceptance/ACCEPTANCE_MATRIX.json'}
    if not required.issubset({p for _, _, p in selected}):
        raise ValueError('Committed source is incomplete.')
    destination.mkdir(parents=True, mode=0o700)
    for mode, oid, path in selected:
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(git('cat-file', 'blob', oid))
        target.chmod(0o755 if mode == '100755' else 0o644)
    (destination / 'AGENTS.md').write_text(AGENTS)
    baseline = destination / 'plans/dsh_codex_taskpack_v1.1'
    progress = destination / 'progress'
    progress.mkdir()
    for name in ('PROGRESS.md', 'HANDOFF.md', 'ENVIRONMENT.md', 'DECISIONS.md'):
        (progress / name).write_text((baseline / 'templates' / name).read_text())
    ledger = json.loads((baseline / 'acceptance/ACCEPTANCE_MATRIX.json').read_text())
    ledger.update(artifact_kind='public_unexecuted_acceptance_template', gate_results={'CORE': False, 'FULL': False})
    for case in ledger['cases']:
        case['status'] = 'NOT_RUN'
        for check in case['subchecks']:
            check.update(status='NOT_RUN', evidence=[], actual_result='',
                         remaining_conditions=['Run and record this check against your own isolated or deployed instance.'])
    (progress / 'ACCEPTANCE.json').write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + '\n')
    (progress / 'UX_ACCEPTANCE.json').write_text(json.dumps({'kind': 'public_unexecuted_change_acceptance_template',
        'checks': [], 'note': 'Record new requirements separately; do not inherit another instance acceptance.'}, indent=2) + '\n')
    for group in ('P0', 'P1', 'P2', 'P3', 'P4', 'reviews'):
        folder = destination / 'evidence' / group
        folder.mkdir(parents=True)
        (folder / '.gitkeep').touch()
    (destination / 'evidence/README.md').write_text('# Local verification output\n\nOnly empty output directories are distributed. Keep real instance reports and screenshots local.\n')
    with (destination / '.gitignore').open('a') as f:
        f.write('\n# Local verification output is never part of the public source snapshot.\n/evidence/*/*\n!/evidence/*/.gitkeep\n')
    files = [p for p in destination.rglob('*') if p.is_file()]
    manifest = [{'path': p.relative_to(destination).as_posix(), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                for p in sorted(files)]
    return {'source_commit': commit, 'destination': str(destination), 'files': len(files),
            'manifest_sha256': hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
            'private_history_exported': False, 'actual_acceptance_exported': False}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', required=True)
    parser.add_argument('--revision', default='HEAD')
    args = parser.parse_args()
    try:
        print(json.dumps(export(args.destination, args.revision), ensure_ascii=False, indent=2))
    except (ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, str(exc) + '\n')

if __name__ == '__main__':
    main()
