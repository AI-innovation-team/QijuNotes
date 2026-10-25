"""Small outbound notification adapters; a server receipt is not device delivery."""
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import urllib.error
import urllib.parse
import urllib.request


class NotificationConfigError(ValueError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward authentication or activity links to a redirect target.
        return None


def _private_http(host):
    try:
        address = ipaddress.ip_address(host)
        return address.is_loopback or address in ipaddress.ip_network('100.64.0.0/10') or address in ipaddress.ip_network('fd7a:115c:a1e0::/48')
    except ValueError:
        return False


def _token(path):
    if not path:
        raise NotificationConfigError('通知缺少 token_file 配置')
    path = Path(path).expanduser()
    if not path.is_absolute() or path.is_symlink():
        raise NotificationConfigError('通知令牌须使用专用绝对路径普通文件')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
                raise NotificationConfigError('通知令牌文件应由服务账户持有且权限为 0600')
            value = handle.read(257).strip()
    except OSError:
        raise NotificationConfigError('通知令牌文件无法读取') from None
    if not re.fullmatch(r'tk_[a-zA-Z0-9]{29}', value):
        raise NotificationConfigError('通知令牌文件格式无效')
    return value


def publish_notification(config, payload, timeout=15):
    """Return only a bounded provider receipt, with no credentials or raw body."""
    backend = config.get('backend', 'webhook')
    endpoint = config.get('url', '')
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise NotificationConfigError('通知 URL 无效；认证不得放在 URL 中')
    headers = {'Content-Type': 'application/json'}
    # Retain the existing four-field generic webhook contract.
    message = {key: payload[key] for key in ('title', 'message', 'url', 'idempotency_key')}
    if backend == 'ntfy':
        if parsed.path not in ('', '/') or parsed.query:
            raise NotificationConfigError('ntfy URL 须为服务根地址，主题用 topic 配置')
        if parsed.scheme == 'http' and not _private_http(parsed.hostname):
            raise NotificationConfigError('ntfy HTTP 仅允许回环或 Tailscale IP')
        topic = config.get('topic', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', topic):
            raise NotificationConfigError('ntfy topic 格式无效')
        sequence = str(payload['idempotency_key'])
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', sequence):
            raise NotificationConfigError('通知效果编号格式无效')
        headers['Authorization'] = 'Bearer ' + _token(config.get('token_file'))
        # Sequence identity lets supported clients replace a bounded retry's
        # card. It is not a claim of exactly-once sound/vibration on a phone.
        message = {'topic': topic, 'title': payload['title'], 'message': payload['message'],
                   'click': payload['url'], 'sequence_id': sequence, 'priority': 3}
        endpoint = endpoint.rstrip('/') + '/'
    elif backend != 'webhook':
        raise NotificationConfigError('未支持的通知适配器')
    request = urllib.request.Request(endpoint, json.dumps(message, ensure_ascii=False).encode(), headers, method='POST')
    # Direct private requests must not leak tokens through inherited HTTP proxy
    # settings. Generic webhooks retain normal system proxy support.
    handlers = [NoRedirect()]
    if backend == 'ntfy':
        handlers.append(urllib.request.ProxyHandler({}))
    with urllib.request.build_opener(*handlers).open(request, timeout=timeout) as response:
        if not 200 <= response.status < 300:
            raise urllib.error.HTTPError(endpoint, response.status, '通知未接受', response.headers, None)
        if backend == 'webhook':
            return str(response.status)
        raw = response.read(65537)
        if len(raw) > 65536:
            raise RuntimeError('ntfy 回执超限；发送结果待核对')
        try:
            value = json.loads(raw)
            valid = (value.get('event') == 'message' and value.get('topic') == topic
                     and value.get('sequence_id') == sequence
                     and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value.get('id', '')))
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise RuntimeError('ntfy 未返回可核对的接受回执')
        return json.dumps({'backend': 'ntfy', 'http_status': response.status, 'message_id': value['id'],
                           'sequence_id': sequence, 'state': 'accepted'}, separators=(',', ':'))
