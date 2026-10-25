#!/usr/bin/env python3
"""Prepare an authenticated, pinned private ntfy service. Never start services.

Creates only the requested project-owned paths. Existing nonempty paths or unit
files are refused; it never rotates an existing password or overwrites config.
The application config is left for the coordinating deployer to merge.
"""
import argparse
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import secrets
import subprocess
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PIN = json.loads((ROOT / 'runtime/ntfy-version.json').read_text())


def _write(path, value, mode=0o600):
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(value if isinstance(value, bytes) else value.encode())
        handle.flush()
        os.fsync(handle.fileno())


def _command(binary, config, args, password=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith('NTFY_')}
    env['NTFY_CONFIG_FILE'] = str(config)
    if password is not None:
        env['NTFY_PASSWORD'] = password
    result = subprocess.run([str(binary), *args], env=env, capture_output=True, text=True, timeout=30)
    if result.returncode:
        # ntfy CLI output can include credentials. Do not echo it on errors.
        raise RuntimeError('ntfy 管理命令失败（输出未写日志）：' + ' '.join(args[:2]))
    return result.stdout


def _quote_unit(value):
    return json.dumps(str(value).replace('%', '%%'))


def prepare(root, secret_dir, unit, host, port=18783, topic='dsh-activities', archive=None):
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64'):
        raise ValueError('当前固定包只支持 Linux amd64')
    address = ipaddress.ip_address(host)
    if not address.is_loopback:
        # Do not let an innocent typo bind a public/LAN/wildcard address. Read
        # only the local Tailscale identity; no routes or daemon settings change.
        tailnet = json.loads(subprocess.check_output(['tailscale', 'status', '--json'], text=True, timeout=10))
        if host not in tailnet.get('TailscaleIPs', []):
            raise ValueError('监听地址须是本机 Tailscale IP 或隔离测试回环地址')
    if not 1024 <= port <= 65535 or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', topic):
        raise ValueError('端口或主题无效')
    root, secret_dir, unit = (Path(x).expanduser().absolute() for x in (root, secret_dir, unit))
    for path in (root, secret_dir, unit):
        if any(p.is_symlink() for p in [path, *path.parents]):
            raise ValueError('安装目标不能经过符号链接')
    if root == secret_dir or root in secret_dir.parents or secret_dir in root.parents:
        raise ValueError('通知数据与凭据必须使用独立目录')
    for directory in (root, secret_dir):
        if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
            raise ValueError('目标已有配置或凭据，拒绝覆盖：' + str(directory))
    if unit.exists():
        raise ValueError('用户服务文件已存在，拒绝覆盖：' + str(unit))
    if archive:
        raw = Path(archive).read_bytes()
    else:
        with urllib.request.urlopen(PIN['archive_url'], timeout=60) as response:
            raw = response.read(40 * 1024 * 1024 + 1)
    if hashlib.sha256(raw).hexdigest() != PIN['archive_sha256']:
        raise ValueError('ntfy 固定安装包校验失败')
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as package:
        name = 'ntfy_' + PIN['version'] + '_linux_amd64/ntfy'
        member = package.getmember(name)
        if not member.isfile() or member.size > 100 * 1024 * 1024:
            raise ValueError('ntfy 包中的可执行文件无效')
        binary_bytes = package.extractfile(member).read()
    if hashlib.sha256(binary_bytes).hexdigest() != PIN['binary_sha256']:
        raise ValueError('ntfy 二进制校验失败')
    previous_umask = os.umask(0o077)
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        secret_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700);os.chmod(secret_dir, 0o700)
        unit.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        binary, config = root / 'ntfy', secret_dir / 'server.yml'
        _write(binary, binary_bytes, 0o700)
        netloc = ('[' + host + ']' if address.version == 6 else host) + ':' + str(port)
        base_url = 'http://' + netloc
        settings = {'base-url': base_url, 'listen-http': netloc,
                    'cache-file': str(root / 'cache.sqlite'), 'cache-duration': '24h',
                    'auth-file': str(secret_dir / 'users.sqlite'), 'auth-default-access': 'deny-all',
                    'enable-signup': False, 'enable-login': False, 'enable-reservations': False,
                    'web-root': 'disable', 'log-level': 'WARN'}
        # JSON is a YAML subset, avoiding shell quoting or another dependency.
        _write(config, json.dumps(settings, indent=2) + '\n')
        # CLI initializes an existing empty SQLite file. This creates the auth
        # schema without temporarily starting an unauthenticated HTTP listener.
        _write(secret_dir / 'users.sqlite', b'')
        phone_password = secrets.token_urlsafe(24)
        _command(binary, config, ['user', 'add', 'dsh-publisher'], secrets.token_urlsafe(32))
        _command(binary, config, ['user', 'add', 'dsh-phone'], phone_password)
        _command(binary, config, ['access', 'dsh-publisher', topic, 'write-only'])
        _command(binary, config, ['access', 'dsh-phone', topic, 'read-only'])
        token_output = _command(binary, config, ['token', 'add', '--label', 'DSH publisher', 'dsh-publisher'])
        tokens = re.findall(r'\btk_[A-Za-z0-9]{29}\b', token_output)
        if len(set(tokens)) != 1:
            raise RuntimeError('ntfy 令牌回执格式无法核对；未输出原回执')
        token_file = secret_dir / 'publisher.token'
        _write(token_file, tokens[0] + '\n')
        phone_file = secret_dir / 'phone.json'
        _write(phone_file, json.dumps({'server': base_url, 'topic': topic, 'username': 'dsh-phone', 'password': phone_password}, indent=2) + '\n')
        # No secrets in unit arguments/environment; secrets remain in owned files.
        service = '\n'.join([
            '[Unit]', 'Description=DSH Personal private Android notification service',
            'After=network-online.target', 'Wants=network-online.target', '', '[Service]',
            'Type=simple', 'ExecStart=' + _quote_unit(binary) + ' serve --config ' + _quote_unit(config),
            'Restart=on-failure', 'RestartSec=10', 'UMask=0077', 'NoNewPrivileges=true',
            'PrivateTmp=true', 'ProtectSystem=strict', 'ProtectHome=read-only',
            'ReadWritePaths=' + _quote_unit(root) + ' ' + _quote_unit(secret_dir),
            'RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX', '', '[Install]', 'WantedBy=default.target', ''
        ])
        _write(unit, service)
        result = {'prepared': True, 'service_started': False, 'application_config_changed': False,
                  'version': PIN['version'], 'binary': str(binary), 'binary_sha256': PIN['binary_sha256'],
                  'server_config': str(config), 'unit_file': str(unit), 'phone_credentials_file': str(phone_file),
                  'notification': {'backend': 'ntfy', 'url': base_url, 'topic': topic, 'token_file': str(token_file)},
                  'anonymous_access': 'deny-all', 'publisher_access': 'write-only', 'phone_access': 'read-only'}
        _write(root / 'installation.json', json.dumps(result, indent=2) + '\n')
        return result
    finally:
        os.umask(previous_umask)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=str(Path.home() / '.local/share/dsh-personal-ntfy'))
    parser.add_argument('--secrets-dir', default=str(Path.home() / '.config/dsh-personal/ntfy'))
    parser.add_argument('--unit-file', default=str(Path.home() / '.config/systemd/user/dsh-personal-notify.service'))
    parser.add_argument('--listen-host', required=True)
    parser.add_argument('--port', type=int, default=18783)
    parser.add_argument('--topic', default='dsh-activities')
    parser.add_argument('--archive', help='Already downloaded archive; the pinned checksum is still required')
    args = parser.parse_args()
    try:
        result = prepare(args.root, args.secrets_dir, args.unit_file, args.listen_host, args.port, args.topic, args.archive)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(str(exc)) from None
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
