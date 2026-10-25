"""Offline, version-limited removal of explicitly owned DSH session namespaces.

DSH 0.2.1-rc.2 exposes no persisted-session erase operation. This adapter removes
whole namespaces without rewriting append-only logs. It is logical filesystem
cleanup, not forensic erasure, and does not change old encrypted backups. Callers
must persist native-session tombstones, retain other business originals, and use
a fresh native session identity after cleanup. Never infer authorization from a
directory scan: session_ids is the caller's explicit, already-authorized list.
"""
from __future__ import annotations

from contextlib import ExitStack
import ctypes
import ctypes.util
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import uuid


class NativeCleanupError(RuntimeError):
    """A writer, unowned related session, or unsupported layout prevents cleanup."""


GENERATION = re.compile(r'session\.v([34])\.jsonl(\.zstd)?$')
TEMP_GENERATION = re.compile(r'(?:session\.v[34]\.jsonl(?:\.zstd)?\.[0-9a-f]{12}|session\.migration\.[0-9a-f]{16}(?:\.zstd)?)\.tmp$')
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
FTS_TABLES = {'search_state', 'persisted_sessions', 'persisted_docs',
              'persisted_docs_data', 'persisted_docs_idx', 'persisted_docs_content',
              'persisted_docs_docsize', 'persisted_docs_config'}
FTS_APPLICATION_ID = 0x44534851
HEADER_BOUND = 1024 * 1024


def _error(message):
    raise NativeCleanupError(message)


def _safe_path(path):
    """Refuse links in every existing component, including the supplied home."""
    path = Path(path).absolute()
    for item in [*reversed(path.parents), path]:
        if item.is_symlink():
            _error('原生清理拒绝符号链接路径')
    return path


def _identity(path, *, directory=False):
    _safe_path(path)
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or (not directory and info.st_nlink != 1):
        _error('原生清理遇到非普通文件、硬链接或未知目录布局')
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _walk_files(root):
    _safe_path(root)
    if not root.exists():
        return []
    _identity(root, directory=True)
    out = []
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            _error('原生数据树含符号链接，未执行清理')
        if path.is_dir():
            _identity(path, directory=True)
        else:
            _identity(path)
            out.append(path)
    return out


def _read_header(path):
    """Read only the bounded first header frame; never return or rewrite events."""
    _identity(path)
    with path.open('rb') as handle:
        prefix = handle.read(HEADER_BOUND)
    if path.name.endswith('.zstd'):
        name = ctypes.util.find_library('zstd')
        if not name:
            _error('清理验证需要本机 libzstd，未执行清理')
        lib = ctypes.CDLL(name)
        lib.ZSTD_findFrameCompressedSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        lib.ZSTD_findFrameCompressedSize.restype = ctypes.c_size_t
        lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
        lib.ZSTD_isError.restype = ctypes.c_uint
        lib.ZSTD_getFrameContentSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        lib.ZSTD_getFrameContentSize.restype = ctypes.c_ulonglong
        lib.ZSTD_decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
        lib.ZSTD_decompress.restype = ctypes.c_size_t
        source = ctypes.create_string_buffer(prefix)
        length = lib.ZSTD_findFrameCompressedSize(source, len(prefix))
        if lib.ZSTD_isError(length) or length > len(prefix):
            _error('原生日志首帧损坏或超出验证上限')
        size = lib.ZSTD_getFrameContentSize(source, length)
        # Node's streaming encoder normally omits the decompressed-size field.
        # ZSTD_CONTENTSIZE_UNKNOWN is allowed with a fixed destination bound;
        # ZSTD_CONTENTSIZE_ERROR and oversized declared frames fail closed.
        unknown_size = size == (1 << 64) - 1
        if size == 0 or (size > HEADER_BOUND and not unknown_size):
            _error('原生日志首帧大小未知或超出验证上限')
        capacity = HEADER_BOUND if unknown_size else size
        target = ctypes.create_string_buffer(capacity)
        actual = lib.ZSTD_decompress(target, capacity, source, length)
        if lib.ZSTD_isError(actual) or (not unknown_size and actual != size):
            _error('原生日志首帧验证失败')
        raw = target.raw[:actual]
        if raw.count(b'\n') != 1 or not raw.endswith(b'\n'):
            _error('原生日志首帧并非独立 header')
    else:
        raw = prefix.split(b'\n', 1)[0]
        if b'\n' not in prefix:
            _error('原生日志 header 缺少边界')
    try:
        header = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        _error('原生日志 header 无法解析')
    match = GENERATION.fullmatch(path.name)
    if (not match or not isinstance(header, dict) or header.get('type') != 'session'
            or header.get('version') != int(match[1]) or header.get('id') != path.parent.name):
        _error('原生日志身份或 generation 与路径不符')
    return header


def _read_json(path):
    _identity(path)
    try:
        return json.loads(path.read_text())
    except (ValueError, UnicodeDecodeError):
        _error('原生派生存储无法解析，未执行清理')


def _atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        _sync_dir(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _query_path(home):
    """The shipped profile is memory-only. Recognize only explicit local overrides."""
    index = None
    profile_patch = home / 'profiles/web/cordis.patch.yml'
    if profile_patch.exists():
        _identity(profile_patch)
        active = ''.join(line.split('#', 1)[0].strip() for line in profile_patch.read_text().splitlines())
        if active not in ('', '[]'):
            _error('自定义原生 profile patch 须先核对存储路由')
    patch = home / 'personal-effective.patch.json'
    if patch.exists():
        rows = _read_json(patch)
        if not isinstance(rows, list):
            _error('未知 DSH patch 布局')
        for row in rows:
            config = row.get('config', {})
            ident = row.get('id')
            if ident in ('session-persistence-jsonl', 'storage-json'):
                expected = home / ('sessions' if ident == 'session-persistence-jsonl' else 'storages')
                if config.get('root') and Path(config['root']).absolute() != expected:
                    _error('外置原生存储不在清理适配范围')
            if ident == 'session-query-sqlite':
                value = config.get('path', ':memory:')
                if value == ':memory:':
                    index = None
                else:
                    index = _safe_path(value)
                    if not index.is_relative_to(home) or index == home:
                        _error('外置原生搜索索引不在清理适配范围')
                    if not index.is_relative_to(home / 'cache'):
                        _error('持久搜索索引必须位于当前 home/cache 专用目录')
    return index


def purge_sessions(dsh_home, session_ids, *, writer_must_be_stopped=True, lock_fd=None, dry_run=False):
    """Remove explicitly owned UUID namespaces from a stopped project DSH home.

    If lock_fd is supplied it must be the caller's open web-host.lock descriptor.
    Its inode and exclusive flock are revalidated, and this function never closes
    or unlocks it. All layouts and descendants are checked before any removal.
    dry_run validates and reports remaining namespaces without changing their data.
    The function is idempotent after partial filesystem failure. A caller must
    retain its deletion tombstones and block reuse until this returns success.
    """
    if writer_must_be_stopped is not True:
        _error('不允许绕过停止宿主要求')
    if isinstance(session_ids, (str, bytes)):
        _error('必须传入明确的项目会话 UUID 清单')
    ids = set(session_ids)
    if any(not isinstance(s, str) or not UUID.fullmatch(s) or str(uuid.UUID(s)) != s for s in ids):
        _error('清理清单只接受 canonical UUID')
    report = {'version': '0.2.1-rc.2', 'scope': 'offline_session_namespace_logical_removal', 'dry_run': bool(dry_run),
              'sessions': {s: {'files_removed': 0, 'generations_removed': 0,
                               'derived_records_removed': 0, 'would_remove_files': 0,
                               'would_remove_derived_records': 0, 'verified_absent': False} for s in sorted(ids)},
              'historical_encrypted_backups_erased': False, 'forensic_erasure': False}
    if not ids:
        return report
    home = _safe_path(dsh_home)
    if not home.exists():
        for detail in report['sessions'].values():
            detail['verified_absent'] = True
        return report
    _identity(home, directory=True)
    lock_path = home / 'web-host.lock'
    with ExitStack() as stack:
        if lock_fd is None:
            _safe_path(lock_path)
            own_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            stack.callback(os.close, own_fd)
            actual_fd = own_fd
        else:
            actual_fd = lock_fd
        try:
            current = lock_path.lstat()
            held = os.fstat(actual_fd)
            if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
                _error('传入的锁不属于当前 DSH home')
            fcntl.flock(actual_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, FileNotFoundError):
            _error('DSH 宿主仍在写入；未执行原生清理')

        # Stage a complete plan first. Profiles may contain package-manager links;
        # those are executable dependencies, not session storage, and stay untouched.
        index = _query_path(home)
        deletes = {}  # file -> (session_id, generation)
        planned_records = {sid: 0 for sid in ids}
        directories = []
        rewrites = {}
        fingerprints = {}
        sessions = _safe_path(home / 'sessions')
        if sessions.exists():
            _identity(sessions, directory=True)
            seen = set()
            for project in sorted(sessions.iterdir()):
                _identity(project, directory=True)
                if not (project.name == '_no-cwd' or (project.name.startswith('--') and project.name.endswith('--'))):
                    _error('未知原生 project 路径布局')
                for directory in sorted(project.iterdir()):
                    _identity(directory, directory=True)
                    sid = directory.name
                    if sid in seen:
                        _error('同一会话 UUID 出现在多个命名空间')
                    seen.add(sid)
                    headers = []
                    for path in sorted(directory.iterdir()):
                        fingerprints[path] = _identity(path)
                        generation = GENERATION.fullmatch(path.name)
                        if generation:
                            header = _read_header(path)
                            headers.append(header)
                            if header.get('parentSession') in ids and sid not in ids:
                                _error('未授权子会话仍继承待删会话；需核对来源再清理')
                        elif path.name != 'session.lock' and not TEMP_GENERATION.fullmatch(path.name):
                            _error('未知原生 session 文件布局')
                        if sid in ids:
                            deletes[path] = (sid, bool(generation))
                    if not headers and any(p.name != 'session.lock' for p in directory.iterdir()):
                        _error('原生会话缺少可验证的 committed generation')
                    if sid in ids:
                        directories.append(directory)
                        # Also reject a direct upstream writer bypassing our launcher.
                        lease = directory / 'session.lock'
                        if lease.exists():
                            lease_fd = os.open(lease, os.O_RDWR | os.O_NOFOLLOW)
                            stack.callback(os.close, lease_fd)
                            try:
                                fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except BlockingIOError:
                                _error('原生 session writer 仍持锁；未执行清理')

        storages = _safe_path(home / 'storages')
        if storages.exists():
            _walk_files(storages)
            for entry in sorted(storages.iterdir()):
                if entry.name == 'session_projcache':
                    for path in _walk_files(entry):
                        if path.parent != entry / 'sessions':
                            _error('未知 projection cache 布局')
                        match = re.fullmatch(r'([0-9a-f-]{36})\.json(?:\.bak\..+)?', path.name)
                        if not match:
                            _error('未知 projection cache 文件命名')
                        if match[1] in ids:
                            deletes[path] = (match[1], False)
                elif entry.name == 'session_projcache.json':
                    doc = _read_json(entry)
                    if doc.get('unit', {}).get('name') != 'session_projcache' or doc['unit'].get('version') not in (3, 4, 5, 6, 7):
                        _error('未知 legacy projection cache schema')
                    table = doc.get('tables', {}).get('sessions')
                    if not isinstance(table, dict):
                        _error('未知 legacy projection cache tables')
                    removed = ids.intersection(table)
                    if removed:
                        for sid in removed:
                            del table[sid]
                            planned_records[sid] += 1
                        rewrites[entry] = doc
                elif entry.name == 'workspace.json':
                    doc = _read_json(entry)
                    if doc.get('unit') != {'name': 'workspace', 'version': 2} or not isinstance(doc.get('global'), dict) or set(doc.get('tables', {})) != {'workspaces'}:
                        _error('未知 workspace schema')
                    changed = False
                    for owner in [doc['global'], *doc['tables']['workspaces'].values()]:
                        for key in ('sessionIds', 'archivedSessionIds', 'pinnedSessionIds'):
                            if key in owner:
                                old = owner[key]
                                if not isinstance(old, list):
                                    _error('未知 workspace session references')
                                for sid in set(old).intersection(ids):
                                    planned_records[sid] += old.count(sid)
                                owner[key] = [s for s in old if s not in ids]
                                changed |= owner[key] != old
                    if changed:
                        rewrites[entry] = doc
                elif entry.name == 'schedule.json':
                    doc = _read_json(entry)
                    if doc.get('unit') != {'name': 'schedule', 'version': 1} or set(doc.get('tables', {})) != {'tasks'}:
                        _error('未知 native schedule schema')
                    # Native schedules are separate user-authored instructions, not
                    # disposable derivations; do not silently erase them with chat.
                    if any(v.get('sessionId') in ids for v in doc['tables']['tasks'].values()):
                        _error('会话有关联原生日程，须先显式处理其原始指令')
                else:
                    _error('未知原生 storage 域，未执行清理')

        # The personal text preset does not create native image/file attachments.
        # They are content-addressed and may be shared; unsupported roots must be
        # explicitly reconciled rather than deleted as if session-owned.
        if _walk_files(home / 'attachments'):
            _error('存在共享原生附件，缺少安全归属映射，未执行清理')
        allowed_index_files = set()
        if index is not None:
            allowed_index_files = {index, Path(str(index) + '-wal'), Path(str(index) + '-shm'), Path(str(index) + '-journal')}
        for path in _walk_files(home / 'cache'):
            if path not in allowed_index_files:
                _error('未知原生 cache 文件，未执行清理')
        connection = None
        if index is not None and index.exists():
            _identity(index)
            connection = sqlite3.connect(index.as_uri() + ('?mode=ro' if dry_run else '?mode=rw'), uri=True)
            stack.callback(connection.close)
            appid = connection.execute('PRAGMA application_id').fetchone()[0]
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'")}
            if appid != FTS_APPLICATION_ID or version != 8 or tables != FTS_TABLES:
                _error('搜索索引不是已核实的 DSH v8 派生 schema')
            for sidecar in allowed_index_files:
                if sidecar.exists():
                    _identity(sidecar)
            for sid in ids:
                planned_records[sid] += connection.execute('SELECT COUNT(*) FROM persisted_docs WHERE session_id=?', (sid,)).fetchone()[0]
                planned_records[sid] += connection.execute('SELECT COUNT(*) FROM persisted_sessions WHERE id=?', (sid,)).fetchone()[0]
        elif index is not None and any(path.exists() for path in allowed_index_files):
            _error('原生索引主文件缺失但存在 journal 副本，未执行清理')

        for path in set(deletes) | set(rewrites):
            fingerprints.setdefault(path, _identity(path))
        # Verify the complete scanned log set again before the first mutation.
        for path, expected in fingerprints.items():
            if _identity(path) != expected:
                _error('原生文件在预检后改变；未执行清理')
        current_lock = lock_path.lstat()
        if current_lock.st_nlink != 1 or (current_lock.st_dev, current_lock.st_ino) != (held.st_dev, held.st_ino):
            _error('原生宿主锁文件在预检后改变，未执行清理')

        for sid in ids:
            report['sessions'][sid]['would_remove_files'] = sum(1 for owner, _ in deletes.values() if owner == sid)
            report['sessions'][sid]['would_remove_derived_records'] = planned_records[sid]
            report['sessions'][sid]['verified_absent'] = (not report['sessions'][sid]['would_remove_files']
                                                         and not planned_records[sid]
                                                         and not any(p.name == sid for p in directories))
        if dry_run:
            return report

        if connection is not None:
            with connection:
                connection.execute('PRAGMA secure_delete=ON')
                for sid in sorted(ids):
                    connection.execute('DELETE FROM persisted_docs WHERE session_id=?', (sid,))
                    connection.execute('DELETE FROM persisted_sessions WHERE id=?', (sid,))
                connection.execute("INSERT INTO persisted_docs(persisted_docs) VALUES('optimize')")
            connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            connection.execute('VACUUM')
            connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        for path, doc in rewrites.items():
            _atomic_json(path, doc)
        for path, (sid, generation) in deletes.items():
            if _identity(path) != fingerprints[path]:
                _error('清理时文件身份改变，须保留删除待核对状态')
            path.unlink()
            _sync_dir(path.parent)
            report['sessions'][sid]['files_removed'] += 1
            report['sessions'][sid]['generations_removed'] += int(generation)
        for directory in directories:
            directory.rmdir()
            _sync_dir(directory.parent)
        if any(path.exists() for path in deletes) or any(path.exists() for path in directories):
            _error('原生文件残留，须保留删除待核对状态')
        if connection is not None:
            for sid in ids:
                if connection.execute('SELECT 1 FROM persisted_docs WHERE session_id=?', (sid,)).fetchone() or connection.execute('SELECT 1 FROM persisted_sessions WHERE id=?', (sid,)).fetchone():
                    _error('原生搜索索引仍有残留')
        for sid, detail in report['sessions'].items():
            detail['derived_records_removed'] = planned_records[sid]
            detail['verified_absent'] = True
        return report
