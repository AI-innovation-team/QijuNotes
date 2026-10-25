#!/usr/bin/env python3
"""Run one isolated DSH Web host; capture its launch secret into a private file."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runtime.node_runtime import NodeRuntimeError, resolve_node

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', default=str(Path.home() / '.local/share/dsh-personal/dsh'))
    parser.add_argument('--port', type=int, default=18781)
    parser.add_argument('--patch', default=str(ROOT / 'runtime/personal.patch.json'))
    args = parser.parse_args()
    try:
        node = resolve_node(ROOT)
    except NodeRuntimeError as exc:
        parser.exit(1, str(exc) + '\n')
    dsh_home = Path(args.home).resolve()
    dsh_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(dsh_home, 0o700)
    lock = (dsh_home / 'web-host.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('A DSH Web writer already owns this data root')
    env = dict(os.environ, DSH_HOME=str(dsh_home), DSH_AGENTS_HOME=str(dsh_home / 'agents'),
               DSH_TELEMETRY_DISABLED='1', NODE_NO_WARNINGS='1')
    gateway_key_file = dsh_home.parent / 'gateway-key'
    if not env.get('DSH_GATEWAY_KEY') and gateway_key_file.exists():
        env['DSH_GATEWAY_KEY'] = gateway_key_file.read_text().strip()
    patch = json.loads(Path(args.patch).read_text())
    patch.append({'insert': [{'id': 'personal-control', 'name': (ROOT / 'runtime/personal-control.mjs').as_uri()}]})
    effective_patch = dsh_home / 'personal-effective.patch.json'
    effective_patch.write_text(json.dumps(patch, ensure_ascii=False))
    os.chmod(effective_patch, 0o600)
    command = [str(node), str(ROOT / 'runtime/npm/node_modules/@deepseek-ai/dsh/lib/bin.js'),
               '--profile', 'web', '--patch', str(effective_patch), '--host', '127.0.0.1', '--port', str(args.port), '--no-open']
    child = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1, pass_fds=(lock.fileno(),))
    def stop(signum, frame):
        child.send_signal(signum)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    for line in child.stdout:
        match = re.search(r'dsh web: (http://[^\s]+)', line)
        if match and '?token=' in match[1]:
            path = dsh_home / 'bridge-auth.json'
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump({'launch_url': match[1], 'pid': child.pid}, f)
                f.flush(); os.fsync(f.fileno())
        print(re.sub(r'([?&]token=)[^\s)]+', r'\1<redacted>', line), end='', flush=True)
    return child.wait()

if __name__ == '__main__':
    sys.exit(main())
