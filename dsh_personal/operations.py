"""Consistent portable exports, encrypted recovery points, and isolated restores.

Every mutation in the live application must hold maintenance_lock(data) while a
snapshot holds its exclusive form. Native DSH files are copied only after the
single host writer has released its lock, optionally by stopping the configured
project user service. This module never changes an unrelated service.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
from hashlib import sha256
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import subprocess
import tempfile
import time
from types import SimpleNamespace
import zipfile
import urllib.parse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import __version__
from .core import Store, TZ
from .records import RecordsStore
from .backup_targets import validate_backup_targets

ROOT = Path(__file__).resolve().parents[1]
FORMAT = "dsh-personal-bundle"
FORMAT_VERSION = 1
MAGIC = b"DSHBACKUP1\n"
SECRET_KEYS = ("secret", "password", "api_key", "apikey", "credential", "authorization", "access_key", "gateway_key", "key_file", "access_token", "bearer", "token_file", "token_path", "offsite_mount")
PRIVATE_NAMES = {"access-token", "gateway-key", "bridge-auth.json", "credentials.json", "auth.json", "provider-credentials.json", "account-auth.json", "account-auth.lock"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path, content, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


@contextmanager
def maintenance_lock(data, exclusive=False, timeout=30):
    """Cross-thread/process checkpoint lock; callers use a fresh descriptor."""
    path = Path(data) / "checkpoint.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as handle:
        os.chmod(path, 0o600)
        deadline = time.monotonic() + timeout
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        while True:
            try:
                fcntl.flock(handle, operation | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("备份检查点等待超时；当前写操作尚未完成")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _systemctl(*args, timeout=30):
    result = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Do not include service output which may accidentally contain credentials.
        raise RuntimeError("项目 DSH 服务控制失败：systemctl --user " + " ".join(args))
    return result.stdout.strip()


@contextmanager
def _native_checkpoint(app, *, yield_lock=False):
    """Hold the same lock as launch_dsh.py, never snapshot an active writer."""
    native = Path(app.data) / "dsh"
    native.mkdir(parents=True, exist_ok=True, mode=0o700)
    unit = app.config().get("backup", {}).get("host_unit")
    if unit and (not unit.startswith("dsh-personal") or not unit.endswith(".service") or "/" in unit):
        raise ValueError("备份仅允许管理 dsh-personal*.service 项目服务")
    stopped = False
    with (native / "web-host.lock").open("a") as handle:
        try:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if not unit:
                    raise RuntimeError("DSH 宿主仍在写入，未配置 backup.host_unit；请停止本项目宿主后导出，不能复制运行中的原生日志")
                _systemctl("stop", unit)
                stopped = True
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield {"native": native, "lock_fd": handle.fileno()} if yield_lock else native
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            if stopped:
                _systemctl("start", unit)


def _redact(value):
    if isinstance(value, dict):
        return {k: "<configure-separately>" if (k.lower() in {"key", "token"} or any(s in k.lower() for s in SECRET_KEYS)) else _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(x) for x in value]
    if isinstance(value, str):
        # Model and notification URLs may embed credentials; endpoints are
        # deliberately reconfigured during migration rather than guessed safe.
        if value.startswith(("http://", "https://")):
            parts = urllib.parse.urlsplit(value)
            host = parts.hostname or ""
            if parts.port:
                host += ":" + str(parts.port)
            return urllib.parse.urlunsplit((parts.scheme, host, parts.path, "<configure-separately>" if parts.query else "", ""))
    return value


def _database_snapshot(source, destination):
    with sqlite3.connect(source, timeout=30) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)
        # A backup may otherwise carry old free pages from a deletion awaiting
        # live readers. Compact only this isolated snapshot, never kill readers.
        dst.execute("PRAGMA secure_delete=ON")
        dst.execute("VACUUM")
        result = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError("SQLite 一致性校验失败")
    os.chmod(destination, 0o600)


def _rows(db, table):
    return [dict(row) for row in db.execute("SELECT * FROM " + table)]


def _readable_rows(db):
    """Portable text excludes unpublished lessons and their derived material.

    This is deliberately separate from the complete, private migration SQLite
    and native store. It never edits the live database or discards backup work.
    """
    from .server import _hidden_row, _hidden_ref
    from .learning_guard import ledger as learning_review_ledger
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    hidden = {r[0] for r in db.execute('SELECT activity_id FROM learning_releases WHERE activated_at IS NULL')} if 'learning_releases' in tables else set()
    private_ids = set(hidden)
    private_sessions = set()
    for row in db.execute('SELECT id,session_id FROM activities'):
        if row['id'] in hidden and row['session_id']:
            private_ids.add(row['session_id']);private_sessions.add(row['session_id'])
    for table,ref in (('messages','message'),('tasks','task'),('attachments','attachment'),('records_profile','profile'),('records_growth','growth')):
        if table not in tables:continue
        key='item_id' if table.startswith('records_') else 'id'
        private_ids.update(row[key] for row in db.execute('SELECT * FROM '+table) if _hidden_ref(db,ref+':'+row[key]))
    result={}
    for table in tables:
        rows=learning_review_ledger(db) if table=='learning_reviews' else _rows(db,table)
        visible=[]
        for row in rows:
            if table=='learning_releases' and row['activity_id'] in hidden:continue
            if table=='activities' and row['id'] in hidden:continue
            if _hidden_row(db,row):continue
            if row.get('session_id') in private_sessions or row.get('target') in private_ids:continue
            if table in {'meta','records_meta','observations','natural_requests'}:
                # Selection snapshots and diagnostic receipts may name a
                # preparation without a relational activity_id column.
                encoded=_json(row)
                if any(ident in encoded for ident in private_ids):continue
            if table=='learning_reviews' and row.get('activity_id') in hidden:continue
            visible.append(row)
        result[table]=visible
    return result


def _readable_snapshot(source, destination):
    _database_snapshot(source,destination)
    with sqlite3.connect(destination) as db:
        db.row_factory=sqlite3.Row
        visible=_readable_rows(db)
        # Only a disposable snapshot is filtered. Keep the complete private
        # candidate schema intact; JSON separately substitutes its safe ledger.
        for table,rows in visible.items():
            if table=='learning_reviews':
                if 'learning_releases' in visible:
                    db.execute('DELETE FROM learning_reviews WHERE activity_id NOT IN (SELECT id FROM activities)')
                continue
            db.execute('DELETE FROM '+table)
            if rows:
                columns=list(rows[0])
                db.executemany('INSERT INTO '+table+' ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',
                               [tuple(row[key] for key in columns) for row in rows])
        db.commit()
        db.execute('VACUUM')


def _database_json(path):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return _readable_rows(db)


def recovery_ledger(app):
    """Corrections, directives, deletion IDs and receipts, not a full transcript."""
    from .directive_cleanup import requirement_version, REQUIREMENT_FIELDS
    from .learning_guard import ledger as learning_review_ledger
    with app.store.db() as db:
        from .feedback import ledger as feedback_ledger
        from .study import ledger as study_ledger
        from .routine_settings import ledger as routine_settings_ledger
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        core = {
            "tombstones": _rows(db, "tombstones"),
            "corrections": _rows(db, "corrections"),
            "notifications": _rows(db, "notifications"),
            "task_reviews": _rows(db, "task_reviews"),
            "context_sources": _rows(db, "context_sources"),
            "deletion_context_refs": _rows(db, "deletion_context_refs"),
            "native_generations": [dict(row) for row in db.execute("SELECT key,value FROM meta WHERE key LIKE 'native_generation:%'")],
            "task_runs": [{k: row[k] for k in ("id", "task_id", "version", "status", "session_id", "created_at", "finished_at")} for row in _rows(db, "task_runs")],
            "tasks": [{**{k: row[k] for k in ("id", "status", "version", "run_at", "interval_days", "authorization", *REQUIREMENT_FIELDS)},
                       "requirements_version": requirement_version(db, row["id"])} for row in _rows(db, "tasks")],
            "task_requirement_revisions": _rows(db, "task_requirement_revisions") if "task_requirement_revisions" in tables else [],
            "directive_requests": _rows(db, "directive_requests") if "directive_requests" in tables else [],
            "learning_reviews": learning_review_ledger(db),
            "feedback": feedback_ledger(db),
            "study": study_ledger(db),
            "routine_settings": routine_settings_ledger(db),
            "schedules": _rows(db, "schedules"),
            "schedule_events": _rows(db, "schedule_events"),
            "learning_releases": _rows(db,"learning_releases") if "learning_releases" in tables else [],
            "usage": _rows(db, "usage"),
            "usage_details": _rows(db, "usage_details") if "usage_details" in tables else [],
            "calendar_events": _rows(db, "calendar_events") if "calendar_events" in tables else [],
            "calendar_requests": _rows(db, "calendar_requests") if "calendar_requests" in tables else [],
            "calendar_changes": _rows(db, "calendar_changes") if "calendar_changes" in tables else [],
            "chronicle_settings": [dict(row) for row in db.execute("SELECT key,value FROM meta WHERE key='chronicle_settings'")],
        }
    return {"format": "dsh-personal-recovery-ledger", "version": FORMAT_VERSION, "as_of": app.store.stamp(),
            "records": app.records.export_ledger(), "core": core}


def _markdown_from(db_path, records):
    # records.render_markdown() also reads profiles and growth provenance. Use
    # one filtered snapshot for every readable section rather than filtering
    # only conversation messages and leaking selections through a derived row.
    with tempfile.TemporaryDirectory(prefix='dsh-visible-markdown-') as temp:
        visible=Path(temp)/'personal.sqlite'
        _readable_snapshot(db_path,visible)
        return _markdown_from_visible(visible,RecordsStore(visible,clock=records.clock))


def _markdown_from_visible(db_path, records):
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT m.*,a.business_date,a.routine FROM messages m JOIN activities a ON a.id=m.activity_id WHERE m.deleted=0 ORDER BY m.received_at,m.rowid").fetchall()
        summaries = db.execute("SELECT s.*,a.business_date,a.routine FROM summaries s JOIN activities a ON a.id=s.activity_id ORDER BY a.business_date,a.routine").fetchall()
        attachments = _rows(db, "attachments")
        deliveries = db.execute("SELECT r.*,t.goal FROM task_runs r JOIN tasks t ON t.id=r.task_id ORDER BY r.created_at").fetchall()
    days, weeks = {}, {}
    for row in rows:
        day = datetime.fromisoformat(row["received_at"]).astimezone(TZ).date()
        ds = day.isoformat()
        days.setdefault(ds, []).append(row)
        monday = (day - timedelta(days=day.weekday())).isoformat()
        weeks.setdefault(monday, set()).add(ds)
    lines = ["# DSH 对话原文与资料归档", "", "本文件保留完整对话用于追溯，不等同以用户事件为核心的个人史。权威来源：持久入站/DSH原生事件；修改请回到应用保存修订。", "", "## 周索引", ""]
    for week, dates in sorted(weeks.items()):
        lines.append(f"- {week} 周：" + "、".join(sorted(dates)))
    for day, values in sorted(days.items()):
        lines += ["", f"## {day}", ""]
        for row in values:
            lines += [f"### {row['role']} · {row['routine']} · {row['received_at']}", "", f"消息 `{row['id']}`；活动 `{row['activity_id']}`；业务日期 {row['business_date']}；处理状态 {row['processing_state']}", "", row["body"], ""]
    lines += ["## 定向摘要与修订", ""]
    for row in summaries:
        lines += [f"### {row['business_date']} · {row['routine']}", "", f"活动 `{row['activity_id']}`；输入版本 {row['input_version']}；人工更正版本 {row['correction_version']}；状态 {row['state']}", "", "```json", _json(json.loads(row["content"])), "```", ""]
    lines += ["## 委托成果", ""]
    for row in deliveries:
        lines += [f"### {row['goal']} · v{row['version']}", "", f"任务 `{row['task_id']}`；执行 `{row['id']}`；状态 {row['status']}", "", row["body"] or "尚无成果正文", ""]
    lines += ["## 附件", ""]
    for attachment in attachments:
        lines.append(f"- [{attachment['name']}]({attachment['path']}) · SHA256 {attachment['sha256']} · 消息 {attachment['message_id'] or '未关联消息'} · 活动 {attachment['activity_id'] or '无'} · 任务 {attachment['task_id'] or '无'}")
    lines += ["", records.render_markdown()]
    return "\n".join(lines)


def markdown_export(app):
    """A SQLite snapshot is sufficient for a readable text export; no DSH copy."""
    with maintenance_lock(app.data, exclusive=True), tempfile.TemporaryDirectory(prefix="dsh-readable-") as temp:
        path = Path(temp) / "personal.sqlite"
        _database_snapshot(app.store.path, path)
        return _markdown_from(path, RecordsStore(path, clock=app.store.clock))


def _configured_secret_paths(config, data):
    paths = set()
    def visit(value, trail=""):
        if isinstance(value, dict):
            for key, child in value.items():
                label = (trail + "." + key).lower()
                if isinstance(child, str) and child and (key.lower().endswith(("_file", "_path")) or key.lower() in {"credentials", "credential_file"}) and any(word in label for word in (*SECRET_KEYS, "key")):
                    path = Path(child).expanduser()
                    paths.add((path if path.is_absolute() else Path(data) / path).resolve())
                visit(child, label)
        elif isinstance(value, list):
            for child in value:
                visit(child, trail)
    visit(config)
    return paths


def _iter_safe_files(root, excluded_paths=()):
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        if any(path.resolve() == excluded or path.resolve().is_relative_to(excluded) for excluded in excluded_paths):
            continue
        if path.is_symlink():
            raise ValueError("导出不跟随符号链接：" + str(path.relative_to(root)))
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if path.name in PRIVATE_NAMES or path.name.startswith(".account-auth.") or path.suffix == ".lock" or any(part in {"bridge-locks", "node_modules", ".git", "cache"} for part in rel.parts):
            continue
        yield path, rel


def export_bundle(app):
    """Portable ZIP with consistent DB, original DSH store, files and plaintext JSON."""
    with maintenance_lock(app.data, exclusive=True), _native_checkpoint(app, yield_lock=True) as checkpoint, tempfile.TemporaryDirectory(prefix="dsh-export-") as temp:
        from runtime.native_cleanup import purge_sessions
        with app.store.db() as db:
            native_ids = [row[0] for row in db.execute("SELECT target_id FROM tombstones WHERE target_type='native_session'")]
            pending_assets = db.execute("SELECT 1 FROM attachments a JOIN tombstones t ON t.target_type='attachment' AND t.target_id=a.id LIMIT 1").fetchone()
        if pending_assets:
            raise RuntimeError("已删除原文的直接附件清理尚未完成；请完成删除维护后再导出")
        cleanup = purge_sessions(checkpoint["native"], native_ids, lock_fd=checkpoint["lock_fd"], dry_run=True)
        if any(not item["verified_absent"] for item in cleanup["sessions"].values()):
            raise RuntimeError("原生删除副本仍存在；请先完成删除维护，禁止把残存原文打入导出")
        work = Path(temp)
        db_path = work / "personal.sqlite"
        _database_snapshot(app.store.path, db_path)
        snapshot_store = Store(db_path, app.store.clock)
        snapshot_records = RecordsStore(db_path, app.store.clock)
        snapshot_app = SimpleNamespace(store=snapshot_store, records=snapshot_records)
        readable_path=work/'readable.sqlite'
        _readable_snapshot(db_path,readable_path)
        readable_records=RecordsStore(readable_path,app.store.clock)
        entries = {"personal.sqlite": db_path.read_bytes(),
                   "data.json": _json(_database_json(db_path)).encode(),
                   "records.json": _json(readable_records.export_data()).encode(),
                   "ledger.json": _json(recovery_ledger(snapshot_app)).encode(),
                   "diary.md": _markdown_from(db_path, snapshot_records).encode(),
                   "config.json": _json(_redact(app.config())).encode()}
        safe_config = json.loads(entries["config.json"])
        if safe_config.get("notification", {}).get("url"):
            safe_config["notification"]["url"] = "<configure-separately>"
        entries["config.json"] = _json(safe_config).encode()
        excluded_paths = _configured_secret_paths(app.config(), app.data)
        for dirname in ("attachments", "workspace", "dsh"):
            root = Path(app.data) / dirname
            for path, relative in _iter_safe_files(root, excluded_paths):
                # Config files in the DSH tree can contain provider credentials.
                if path.suffix == ".json" and any(x in path.name.lower() for x in ("config", "provider", "settings")):
                    try:
                        content = _json(_redact(json.loads(path.read_text()))).encode()
                    except (ValueError, UnicodeDecodeError):
                        raise ValueError("DSH 配置无法安全脱敏：" + str(relative))
                else:
                    content = path.read_bytes()
                entries[str(PurePosixPath(dirname) / PurePosixPath(relative.as_posix()))] = content
        for relative in ("runtime/personal.patch.json", "runtime/versions.json", "package.json", "requirements.txt", "pyproject.toml"):
            path = ROOT / relative
            if path.is_file():
                entries["release/" + relative] = path.read_bytes()
        manifest = {"format": FORMAT, "format_version": FORMAT_VERSION, "application_version": __version__,
                    "schema_version": snapshot_store.meta("schema_version"), "exported_at": app.store.stamp(),
                    "business_timezone": "Asia/Shanghai", "native_consistency": "single-writer host lock held; SQLite backup API",
                    "credentials": "excluded; configure model, notification and access credentials separately",
                    "privacy": "private complete migration archive: SQLite/native data include unpublished preparation; readable data.json, records.json and diary.md exclude it",
                    "files": {name: {"sha256": sha256(data).hexdigest(), "size": len(data)} for name, data in entries.items()}}
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", _json(manifest))
            for name, data in entries.items():
                archive.writestr(name, data)
        return buffer.getvalue()


def create_key(path):
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(AESGCM.generate_key(bit_length=256))
        out.flush()
        os.fsync(out.fileno())
    return {"created": str(path), "notice": "密钥不打印、不包含在备份；请在独立安全位置保管副本"}


def _key(path):
    path = Path(path).expanduser().resolve()
    if path.stat().st_mode & 0o077:
        raise PermissionError("备份密钥权限须为 0600 或更严格")
    value = path.read_bytes()
    if len(value) != 32:
        try:
            value = base64.b64decode(value, validate=True)
        except ValueError:
            pass
    if len(value) != 32:
        raise ValueError("AES-256 密钥必须为 32 字节")
    return value


def encrypt(content, key):
    nonce = os.urandom(12)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, content, MAGIC)


def decrypt(content, key):
    if not content.startswith(MAGIC) or len(content) < len(MAGIC) + 12 + 16:
        raise ValueError("未知或不完整的加密备份")
    nonce = content[len(MAGIC):len(MAGIC) + 12]
    try:
        return AESGCM(key).decrypt(nonce, content[len(MAGIC) + 12:], MAGIC)
    except Exception as exc:
        raise ValueError("备份认证失败：密钥不符或密文被修改") from exc


def _check_backup_targets(app, key_path, *, backup_dir=None, ledger_dir=None, offsite_dir=None):
    configured = app.config().get("backup", {})
    return validate_backup_targets(
        app.data, key_path,
        backup_dir or configured.get("directory") or Path(app.data) / "backups",
        ledger_dir or configured.get("ledger_directory") or Path(app.data) / "recovery-ledger",
        offsite_dir=offsite_dir, expected_mount=configured.get("offsite_mount"))


def _publish_offsite(app, key_path, offsite_dir, artifacts, *, backup_dir=None, ledger_dir=None):
    if not offsite_dir:
        return False, None
    try:
        for filename, content in artifacts:
            checked = _check_backup_targets(app, key_path, backup_dir=backup_dir, ledger_dir=ledger_dir, offsite_dir=offsite_dir)
            _atomic_write(Path(checked["offsite_dir"]) / filename, content)
            _check_backup_targets(app, key_path, backup_dir=backup_dir, ledger_dir=ledger_dir, offsite_dir=offsite_dir)
    except (ValueError, OSError, RuntimeError) as exc:
        # Paths and mount sources may contain credentials. The local recovery
        # point remains usable; the CLI returns failure for the remote stage.
        return False, "异机校验或复制失败（" + type(exc).__name__ + "）；本机恢复点保留，核对挂载后重试"
    return True, None


def protect_ledgers(app, key_path, destination=None, offsite_dir=None):
    """Serialize collection, encryption, publication, and metadata as one unit.

    Releasing the checkpoint lock before publication would allow a slower older
    call to overwrite a newer deletion ledger. The full operation is ordered.
    """
    key = _key(key_path)
    configured = app.config().get("backup", {})
    destination = destination or configured.get("ledger_directory")
    offsite_dir = offsite_dir or configured.get("offsite_dir")
    with maintenance_lock(app.data, exclusive=True):
        checked = _check_backup_targets(app, key_path, ledger_dir=destination)
        ledger = recovery_ledger(app)
        encoded = encrypt(_json(ledger).encode(), key)
        root = Path(checked["local_ledger"])
        path = root / "latest.ledger.enc"
        _atomic_write(path, encoded)
        copied, offsite_error = _publish_offsite(app, key_path, offsite_dir,
            [("latest.ledger.enc", encoded)], ledger_dir=destination)
        app.store.set_meta("ledger_protected_at", ledger["as_of"])
        if copied:
            app.store.set_meta("ledger_offsite_at", ledger["as_of"])
        elif not offsite_dir:
            app.store.set_meta("ledger_offsite_at", "not_configured")
        app.store.set_meta("ledger_offsite_error", offsite_error or "")
        return {"path": str(path), "as_of": ledger["as_of"], "offsite_copy": copied,
                "offsite_configured": bool(offsite_dir), "offsite_error": offsite_error,
                "offsite_verified": False, "notice": "目标路径复制成功不等于异机故障恢复已验证"}


def _retention(index):
    keep = set()
    for period, maximum in (("day", 7), ("week", 4), ("month", 6)):
        buckets = {}
        for item in sorted(index, key=lambda x: x["created_at"], reverse=True):
            when = datetime.fromisoformat(item["created_at"]).astimezone(TZ)
            key = when.date().isoformat() if period == "day" else (f"{when.isocalendar().year}-W{when.isocalendar().week:02}" if period == "week" else when.strftime("%Y-%m"))
            buckets.setdefault(key, item["filename"])
        keep.update(list(buckets.values())[:maximum])
    return keep


def _backup_serialized(app, key_path, destination=None, offsite_dir=None):
    key = _key(key_path)
    configured = app.config().get("backup", {})
    destination = destination or configured.get("directory")
    offsite_dir = offsite_dir or configured.get("offsite_dir")
    root = Path(destination or Path(app.data) / "backups")
    checked = _check_backup_targets(app, key_path, backup_dir=root)
    root = Path(checked["local_backup"])
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    began = time.monotonic()
    payload = export_bundle(app)
    encoded = encrypt(payload, key)
    now = app.store.stamp()
    filename = "dsh-" + datetime.fromisoformat(now).strftime("%Y%m%dT%H%M%S%fZ") + "-" + sha256(encoded).hexdigest()[:8] + ".dshbackup"
    path = root / filename
    _atomic_write(path, encoded)
    index_path = root / "index.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else []
    index.append({"filename": filename, "created_at": now, "sha256": sha256(encoded).hexdigest(), "size": len(encoded), "version": __version__})
    keep = _retention(index)
    # Only our recorded recovery points are eligible for retention deletion.
    for item in index:
        name = item["filename"]
        if name not in keep and Path(name).name == name and name.endswith(".dshbackup"):
            (root / name).unlink(missing_ok=True)
    index = [item for item in index if item["filename"] in keep]
    _atomic_write(index_path, _json(index).encode())
    ledgers = protect_ledgers(app, key_path, offsite_dir=offsite_dir)
    copied, offsite_error = _publish_offsite(app, key_path, offsite_dir,
        [(filename, encoded), ("index.json", _json(index).encode())], backup_dir=root)
    report = {"path": str(path), "created_at": now, "bytes": len(encoded), "duration_seconds": round(time.monotonic() - began, 3),
              "sha256": sha256(encoded).hexdigest(), "retained_points": len(index), "retention": "7 daily / 4 weekly / 6 monthly",
              "encrypted": "AES-256-GCM", "ledger": ledgers, "offsite_verified": False,
              "offsite_configured": bool(offsite_dir), "offsite_copy": copied and ledgers["offsite_copy"],
              "offsite_error": offsite_error or ledgers.get("offsite_error")}
    app.store.set_meta("last_backup", _json(report))
    app.store.observe("backup", report)
    return report


def backup(app, key_path, destination=None, offsite_dir=None):
    # Separate lock orders full backup/index publication without nesting the
    # snapshot's exclusive checkpoint lock. Manual and timer backups may race.
    path = Path(app.data) / "backup-operation.lock"
    with path.open("a") as handle:
        deadline = time.monotonic() + 30
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("另一备份仍在执行，请稍后重试")
                time.sleep(0.05)
        try:
            return _backup_serialized(app, key_path, destination, offsite_dir)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def verify_bundle(content):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("导出包包含重复路径")
        total = 0
        for info in archive.infolist():
            path = PurePosixPath(info.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in info.filename or (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("导出包包含不安全路径")
            total += info.file_size
            if total > 10 * 1024**3:
                raise ValueError("导出包超出 10 GiB 恢复限制")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("format") != FORMAT or manifest.get("format_version") != FORMAT_VERSION:
            raise ValueError("不支持的导出包版本")
        expected = set(manifest["files"]) | {"manifest.json"}
        if set(names) != expected:
            raise ValueError("导出包文件列表与清单不一致")
        for name, meta in manifest["files"].items():
            raw = archive.read(name)
            if len(raw) != meta["size"] or sha256(raw).hexdigest() != meta["sha256"]:
                raise ValueError("导出包内容校验失败：" + name)
    return manifest


def _replay_derived_tombstones(db, affected):
    """Replay exact identities, preserving later work even at equal timestamps."""
    from .directive_cleanup import replay_directive_tombstones
    cleaned = replay_directive_tombstones(db, _now())
    for key in affected:
        affected[key].update(cleaned[key])
    native_ids = {row[0] for row in db.execute("SELECT target_id FROM tombstones WHERE target_type='native_session'")}
    deleted_messages = {row[0] for row in db.execute("SELECT target_id FROM tombstones WHERE target_type IN ('message','derived_message')")}
    for tomb in db.execute("SELECT * FROM tombstones WHERE target_type!='message'").fetchall():
        kind, ident = tomb["target_type"], tomb["target_id"]
        if kind == "derived_message":
            db.execute("UPDATE messages SET body='',deleted=1,processing_state='deleted',error=NULL WHERE id=? AND role IN ('assistant','tool')", (ident,))
            affected["message_ids"].add(ident)
        elif kind == "natural_request":
            db.execute("UPDATE natural_requests SET body='',result=? WHERE id=?", (_json({"message": "原请求涉及已删除资料；请明确提交新的内容。"}), ident))
        elif kind == "activity_derivatives":
            summary = db.execute("SELECT content FROM summaries WHERE activity_id=?", (ident,)).fetchone()
            if summary and deleted_messages.intersection(json.loads(summary[0]).get("source_ids", [])):
                db.execute("DELETE FROM summaries WHERE activity_id=?", (ident,))
            activity = db.execute("SELECT session_id FROM activities WHERE id=?", (ident,)).fetchone()
            if activity and activity[0] in native_ids:
                db.execute("DELETE FROM summaries WHERE activity_id=?", (ident,))
                db.execute("DELETE FROM native_questions WHERE activity_id=?", (ident,))
                db.execute("UPDATE activities SET session_state='deletion_pending' WHERE id=?", (ident,))
                db.execute("UPDATE jobs SET status='cancelled',error=NULL WHERE target=? OR target IN (SELECT id FROM messages WHERE activity_id=?)", (ident, ident))
                affected["activity_ids"].add(ident)
        elif kind == "task_run":
            db.execute("UPDATE task_runs SET body=NULL,status='source_deleted' WHERE id=?", (ident,))
        elif kind == "task_version":
            task_id, separator, version = ident.rpartition(":")
            if not separator or not version.isdigit():
                raise ValueError("删除账本任务版本身份无效")
            changed = db.execute("UPDATE tasks SET result=NULL,status='recovery_hold',error='关联原文已删除；旧成果失效，需明确重新委托后继续' WHERE id=? AND version=?", (task_id, int(version))).rowcount
            if changed:
                affected["task_ids"].add(task_id)
        elif kind == "attachment":
            affected["attachment_ids"].add(ident)
        elif kind == "native_session":
            affected["native_session_ids"].add(ident)
    # Review candidates are private derivatives too. Replay exact source and
    # native identities so an old backup cannot resurrect their hidden text.
    from .learning_guard import recover as recover_learning_reviews, scrub as scrub_learning_reviews
    scrub_learning_reviews(db, activity_ids=affected['activity_ids'], message_ids=affected['message_ids'],
                           source_refs=['native_session:' + sid for sid in native_ids], stamp=_now())
    recover_learning_reviews(db, stamp=_now())
    from .feedback import scrub as scrub_feedback
    scrub_feedback(db,_now())
    from .learning_editorial import scrub as scrub_learning_editorial
    scrub_learning_editorial(db)


def _merge_directive_ledger(db, core):
    """Merge durable state, never execute a candidate or invent a chat receipt.

    Older ledgers omit these keys. Requirement revisions are independent of task
    delivery versions; snapshot fields may only move to an equal/newer revision.
    Deletion is replayed after this merge, including against absent source rows.
    """
    from .directive_cleanup import requirement_version, REQUIREMENT_FIELDS, scrub_receipt
    from .task_requirements import initialize
    initialize(db)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    before_versions = {r[0]: requirement_version(db, r[0]) for r in db.execute("SELECT id FROM tasks")}
    for row in core.get("task_requirement_revisions", []):
        if row["task_id"] not in before_versions:
            continue
        if type(row.get("requirements_version")) is not int or row["requirements_version"] < 1:
            raise ValueError("恢复账本需求版本无效")
        columns = ("task_id", "requirements_version", "before_json", "after_json", "user_input", "source_message_id", "created_at")
        db.execute("INSERT OR IGNORE INTO task_requirement_revisions (" + ",".join(columns) + ") VALUES(?,?,?,?,?,?,?)",
                   tuple(row[key] for key in columns))
    for row in core.get("tasks", []):
        revision = row.get("requirements_version")
        if row["id"] not in before_versions or revision is None:
            continue
        if type(revision) is not int or revision < 0:
            raise ValueError("恢复账本任务需求版本无效")
        if revision < before_versions[row["id"]]:
            continue
        if requirement_version(db, row["id"]) != revision or any(not isinstance(row.get(k), str) for k in REQUIREMENT_FIELDS):
            raise ValueError("恢复账本任务需求字段或修订历史不完整")
        db.execute("UPDATE tasks SET goal=?,inputs=?,deliverable=?,acceptance=? WHERE id=?",
                   (*[row[key] for key in REQUIREMENT_FIELDS], row["id"]))
    if "directive_requests" not in tables:
        return
    for row in core.get("directive_requests", []):
        source = db.execute("SELECT activity_id,role,deleted FROM messages WHERE id=?", (row["message_id"],)).fetchone()
        # A later ledger cannot manufacture a missing original message. Its
        # task/profile state stays quarantined until the restore is reviewed.
        if source is None or source["role"] != "user" or source["activity_id"] != row["activity_id"]:
            if db.execute("SELECT 1 FROM tombstones WHERE target_type IN ('message','directive_request') AND target_id=?", (row["message_id"],)).fetchone():
                scrub_receipt(db, row.get("receipt_id"), row["updated_at"], {"message_ids": set()})
            continue
        current = db.execute("SELECT updated_at FROM directive_requests WHERE message_id=?", (row["message_id"],)).fetchone()
        if current and current[0] > row["updated_at"]:
            continue
        row = dict(row)
        if row["status"] not in ("none", "applied", "clarify", "rejected", "deleted"):
            row["status"] = "recovery_hold"
        receipt = db.execute("SELECT role,processing_state,deleted FROM messages WHERE id=?", (row.get("receipt_id"),)).fetchone()
        if row["status"] in ("applied", "clarify", "rejected") and (receipt is None or receipt["role"] != "assistant" or receipt["processing_state"] != "local_control" or receipt["deleted"]):
            row["status"] = "recovery_hold"
            row["receipt_id"] = None
        columns = ("message_id", "activity_id", "status", "decision", "receipt_id", "created_at", "updated_at")
        db.execute("INSERT OR REPLACE INTO directive_requests (" + ",".join(columns) + ") VALUES(?,?,?,?,?,?,?)",
                   tuple(row.get(key) for key in columns))


def _merge_core_ledger(store, ledger, original_bodies=None):
    from .deletion import apply_message_deletion, apply_source_invalidation
    from .directive_cleanup import requirement_version, scrub_directives
    if ledger.get("format") != "dsh-personal-recovery-ledger" or ledger.get("version") != FORMAT_VERSION:
        raise ValueError("未知恢复账本版本")
    from .calendar import Calendar
    Calendar(store)
    affected = {key: set() for key in ("activity_ids", "task_ids", "message_ids", "native_session_ids", "attachment_ids")}
    with store.db(True) as db:
        core = ledger["core"]
        from .routine_settings import merge_ledger as merge_routine_settings
        merge_routine_settings(db, core.get('routine_settings', []))
        for row in core.get('schedules', []):
            current=db.execute('SELECT updated_at,effective_at FROM schedules WHERE routine=?',(row['routine'],)).fetchone()
            if row['routine'] in ('A1','A2','A3') and (not current or current['effective_at']=='1970-01-01T00:00:00+00:00' or current['updated_at']<row['updated_at']):
                db.execute('INSERT OR REPLACE INTO schedules VALUES(?,?,?,?,?,?,?)',tuple(row[k] for k in ('routine','time','weekdays','paused_until','updated_at','source','effective_at')))
        for row in core.get('schedule_events', []):
            db.execute('INSERT OR IGNORE INTO schedule_events VALUES(?,?,?,?,?,?)',tuple(row[k] for k in ('id','routine','before_json','after_json','source','created_at')))
        from .learning_schedule import initialize as initialize_learning_schedule
        initialize_learning_schedule(db)
        for row in core.get('learning_releases',[]):
            aid=row['activity_id']
            current=db.execute('SELECT * FROM learning_releases WHERE activity_id=?',(aid,)).fetchone()
            # A ledger contains ordering metadata, never the approved body.
            # Preserve the snapshot's public/private boundary; a newer release
            # receipt alone cannot turn an older prepared draft into a lesson.
            activated=current['activated_at'] if current else None
            if row.get('activated_at') is None:activated=None
            if current and activated is None:
                db.execute('UPDATE learning_releases SET due_at=?,activated_at=NULL,schedule_signature=?,reason=? WHERE activity_id=?',
                           (row['due_at'],row['schedule_signature'],'recovery_hold',aid))
            elif not current:
                db.execute('INSERT INTO learning_releases(activity_id,due_at,activated_at,schedule_signature,created_at,reason) VALUES(?,?,NULL,?,?,?)',
                           (aid,row['due_at'],row['schedule_signature'],row['created_at'],'recovery_hold'))
            # An already visible snapshot stays visible only with its own
            # persisted release; the general recovery isolation still applies.
        columns=('id','title','start_at','end_at','all_day','location','notes','reminder_minutes','status','version','notification_state','reminder_at','notification_receipt','created_at','updated_at','completed_at')
        for row in core.get('calendar_events',[]):
            current=db.execute('SELECT version,notification_state,notification_receipt FROM calendar_events WHERE id=?',(row['id'],)).fetchone()
            if current and current['version']>row['version']:continue
            row=dict(row)
            terminal_rank={'accepted':5,'unknown':4,'expired':3,'recovery_unknown':2,'page_only':1}
            if current and current['version']==row['version'] and terminal_rank.get(current['notification_state'],0)>terminal_rank.get(row['notification_state'],0):
                row['notification_state']=current['notification_state'];row['notification_receipt']=current['notification_receipt']
            elif row['notification_state'] in ('pending','sending'):row['notification_state']='recovery_unknown'
            db.execute('INSERT OR REPLACE INTO calendar_events VALUES('+','.join('?' for _ in columns)+')',tuple(row.get(k) for k in columns))
        for row in core.get('calendar_requests',[]):
            if db.execute('SELECT 1 FROM calendar_events WHERE id=?',(row['event_id'],)).fetchone():
                db.execute('INSERT OR IGNORE INTO calendar_requests VALUES(?,?,?)',tuple(row[k] for k in ('submission_id','fingerprint','event_id')))
        for row in core.get('calendar_changes',[]):
            db.execute('INSERT OR IGNORE INTO calendar_changes VALUES(?,?,?,?,?,?)',tuple(row[k] for k in ('id','event_id','version','action','at','user_input')))
        for row in core.get('chronicle_settings',[]):
            if row.get('key')!='chronicle_settings':continue
            current=db.execute("SELECT value FROM meta WHERE key='chronicle_settings'").fetchone()
            if not current or json.loads(current[0]).get('updated_at','')<json.loads(row['value']).get('updated_at',''):
                db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(row['key'],row['value']))
        for row in core.get("tombstones", []):
            db.execute("INSERT OR IGNORE INTO tombstones VALUES(?,?,?,?,?)", tuple(row[k] for k in ("id", "target_type", "target_id", "fingerprint", "created_at")))
        for row in core.get("context_sources", []):
            db.execute("INSERT OR IGNORE INTO context_sources VALUES(?,?)", (row["session_id"], row["source_ref"]))
        for row in core.get("deletion_context_refs", []):
            db.execute("INSERT OR IGNORE INTO deletion_context_refs VALUES(?,?)", (row["message_id"], row["ref"]))
        for row in core.get("native_generations", []):
            current = db.execute("SELECT value FROM meta WHERE key=?", (row["key"],)).fetchone()
            if row["key"].startswith("native_generation:") and (not current or int(current[0]) < int(row["value"])):
                db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (row["key"], str(int(row["value"]))))
        for row in core.get("corrections", []):
            if not db.execute("SELECT 1 FROM activities WHERE id=?", (row["activity_id"],)).fetchone():
                continue
            db.execute("INSERT OR IGNORE INTO corrections VALUES(?,?,?,?,?,?)", tuple(row[k] for k in ("id", "activity_id", "fields", "source", "created_at", "version")))
            db.execute("UPDATE activities SET correction_version=MAX(correction_version,?) WHERE id=?", (row["version"], row["activity_id"]))
            db.execute("DELETE FROM summaries WHERE activity_id=?", (row["activity_id"],))
        # Known external effects keep their receipt; unknown effects stay unknown.
        for row in core.get("notifications", []):
            current = db.execute("SELECT 1 FROM notifications WHERE id=?", (row["id"],)).fetchone()
            if current:
                state = "recovery_unknown" if row["state"] in ("pending", "sending") else row["state"]
                db.execute("UPDATE notifications SET state=?,attempts=?,receipt=?,error=? WHERE id=?", (state, row["attempts"], row.get("receipt"), row.get("error"), row["id"]))
        for row in core.get("usage", []):
            db.execute("INSERT OR REPLACE INTO usage VALUES(?,?,?,?,?,?,?,?,?)", tuple(row.get(k) for k in ("id", "kind", "day", "month", "reserved", "actual", "state", "created_at", "receipt")))
        for row in core.get("usage_details", []):
            db.execute("INSERT OR REPLACE INTO usage_details VALUES(?,?,?,?,?,?,?,?,?,?,?)", tuple(row.get(key) for key in ("reservation_id", "model", "output_limit_field", "output_limit", "stream", "started_at", "finished_at", "reported_total", "usage_json", "response_state", "usage_state")))
        for row in core.get("tasks", []):
            # A completion after the snapshot must never silently become queued.
            current = db.execute("SELECT version FROM tasks WHERE id=?", (row["id"],)).fetchone()
            if current:
                same_requirements = requirement_version(db, row["id"]) == row.get("requirements_version", 0)
                preserved = row["status"] in ("accepted", "cancelled") and current["version"] == row["version"] and same_requirements
                db.execute("UPDATE tasks SET status=?,version=MAX(version,?),run_at=?,error=? WHERE id=?", (row["status"] if preserved else "needs_input", row["version"], row["run_at"], None if preserved else "恢复后需核对最新任务成果与动作回执，禁止自动重放", row["id"]))
        for row in core.get("task_reviews", []):
            if db.execute("SELECT 1 FROM tasks WHERE id=?", (row["task_id"],)).fetchone():
                db.execute("INSERT OR IGNORE INTO task_reviews VALUES(?,?,?,?,?,?,?)", tuple(row[k] for k in ("id", "task_id", "version", "action", "input", "actor", "created_at")))
        _merge_directive_ledger(db, core)
        from .feedback import recover as recover_feedback
        recover_feedback(db,core.get('feedback',[]),ledger['as_of'])
        from .study import recover as recover_study
        recover_study(db,core.get('study',[]),ledger['as_of'])
        from .learning_guard import recover as recover_learning_reviews, scrub as scrub_learning_reviews
        recover_learning_reviews(db, core.get('learning_reviews', []), ledger['as_of'])
        record_sources = [row['object_id'] for row in ledger.get('records', {}).get('tombstones', [])]
        if record_sources:
            # Imported tombstones are installed in RecordsStore after this
            # transaction, so replay their source identities explicitly now.
            refs = list(record_sources)
            for source in record_sources:
                refs.extend(prefix + ':' + source for prefix in ('import', 'message', 'profile', 'growth', 'activity', 'task'))
            scrub_learning_reviews(db, source_refs=refs, stamp=ledger['as_of'])
        # Records tombstones are applied by the outer restore after this core
        # merge; their provenance must already scrub copied control receipts.
        cleaned = scrub_directives(db, (), ledger["as_of"],
                                   source_refs=[row["object_id"] for row in ledger.get("records", {}).get("tombstones", [])])
        for key in affected:
            affected[key].update(cleaned[key])
        # Apply deletion last: a copied correction, review or accepted-task state
        # must not reintroduce data or effects after the deletion has run.
        for row in db.execute("SELECT * FROM tombstones WHERE target_type='message'").fetchall():
            previous = (original_bodies or {}).get(row["target_id"])
            result = apply_message_deletion(db, row["target_id"], row["created_at"], row["fingerprint"], previous)
            for key in affected:
                affected[key].update(result[key])
        withdrawn = [row[0] for row in db.execute("SELECT target_id FROM tombstones WHERE target_type='context_ref'")]
        if withdrawn:
            result = apply_source_invalidation(db, withdrawn, ledger["as_of"])
            for key in affected:
                affected[key].update(result[key])
        _replay_derived_tombstones(db, affected)
        from .learning import prune_learning_history
        prune_learning_history(db)
        from .chronicle import prune_chronicle_corrections
        prune_chronicle_corrections(db)
        db.execute("INSERT OR REPLACE INTO meta VALUES ('recovery_ledger_as_of',?)", (ledger["as_of"],))
    return {key: sorted(value) for key, value in affected.items()}


def _restore_deleted_files(root, store):
    """Remove only tombstoned owned attachments/native namespaces in staging."""
    from runtime.native_cleanup import purge_sessions
    removed = []
    with store.db(True) as db:
        ids = {row[0] for row in db.execute("SELECT target_id FROM tombstones WHERE target_type='attachment'")}
        for ident in ids:
            row = db.execute("SELECT path FROM attachments WHERE id=?", (ident,)).fetchone()
            if row:
                path = root / row["path"]
                if path.is_symlink() or not path.resolve().is_relative_to((root / "attachments").resolve()):
                    raise ValueError("删除账本中的附件路径不属于实例附件目录")
                shared = db.execute("SELECT id FROM attachments WHERE path=? AND id!=?", (row["path"], ident)).fetchall()
                if not any(other[0] not in ids for other in shared):
                    path.unlink(missing_ok=True)
                db.execute("DELETE FROM attachments WHERE id=?", (ident,))
                removed.append(ident)
        native_ids = [row[0] for row in db.execute("SELECT target_id FROM tombstones WHERE target_type='native_session'")]
    native = purge_sessions(root / "dsh", native_ids)
    with store.db(True) as db:
        for row in db.execute("SELECT id,session_id FROM activities WHERE session_state='deletion_pending'").fetchall():
            if row["session_id"] and row["session_id"] not in native_ids:
                raise ValueError("受影响活动缺少原生删除账本，恢复保持隔离")
            # Global recovery isolation remains in place; any future native
            # session must use a fresh generation and cannot reuse deleted logs.
            key = "native_generation:" + row["id"]
            previous = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(int(previous[0]) + 1 if previous else 1)))
            db.execute("UPDATE activities SET session_id=NULL,session_state='pending' WHERE id=?", (row["id"],))
    for path in (root / "cache").glob("*"):
        if path.is_file() and not path.is_symlink():
            path.unlink()
    return {"attachments_removed": sorted(removed), "native": native}


def _restore_unpublished(content, destination, latest_ledger=None):
    """Only empty destinations are accepted; all automatic processing is isolated."""
    began = time.monotonic()
    manifest = verify_bundle(content)
    root = Path(destination).expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError("恢复目录必须为空；不会覆盖正在运行或已有数据的实例")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for name in archive.namelist():
            # JSON and Markdown remain inspectable; executable release files
            # are references, never run automatically.
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _atomic_write(target, archive.read(name))
    store = Store(root / "personal.sqlite")
    from .calendar import Calendar
    Calendar(store)
    records = RecordsStore(root / "personal.sqlite")
    store.set_meta("isolated", "true")
    store.set_meta("recovery_started", _now())
    store.set_meta("recovery_source_version", manifest["application_version"])
    with store.db(True) as db:
        db.execute("UPDATE jobs SET status='recovery_hold',lease_until=NULL WHERE status NOT IN ('done','cancelled')")
        db.execute("UPDATE notifications SET state='recovery_unknown' WHERE state IN ('pending','sending')")
        db.execute("UPDATE calendar_events SET notification_state='recovery_unknown' WHERE notification_state IN ('pending','sending')")
        db.execute("UPDATE tasks SET status='needs_input',error='恢复隔离：动作与成果需核对后再显式继续' WHERE status NOT IN ('cancelled','accepted')")
        db.execute("UPDATE task_runs SET status='recovery_unknown' WHERE status='running'")
        db.execute("INSERT OR REPLACE INTO records_meta VALUES ('quarantine','1')")
        db.execute("UPDATE records_import_messages SET quarantined=1 WHERE deleted=0")
    bundled_ledger = json.loads((root / "ledger.json").read_text())
    with store.db() as db:
        original_bodies = {row["id"]: row["body"] for row in db.execute("SELECT id,body FROM messages")}
    def merge_deletions(ledger):
        affected = _merge_core_ledger(store, ledger, original_bodies)
        for kind, key in (("message", "message_ids"), ("activity", "activity_ids"), ("task", "task_ids")):
            for ident in affected[key]:
                records.delete_source(kind + ":" + ident, preserve_source=kind in ("activity", "task"))
        with store.db() as db:
            withdrawn = [row[0] for row in db.execute("SELECT target_id FROM tombstones WHERE target_type='context_ref'")]
        for ref in withdrawn:
            if ref.startswith(("import:", "message:", "profile:", "growth:", "activity:", "task:")):
                records.delete_source(ref, preserve_source=ref.startswith(("activity:", "task:")))
    # Invalidate the snapshot's old derivatives before installing newer ledger
    # revisions. A later confirmed profile may legitimately cite this activity.
    merge_deletions(bundled_ledger)
    records.apply_ledger(bundled_ledger["records"])
    if latest_ledger:
        if latest_ledger["as_of"] < bundled_ledger["as_of"]:
            raise ValueError("所谓最新账本早于快照账本，恢复保持隔离")
        merge_deletions(latest_ledger)
        records.apply_ledger(latest_ledger["records"])
    deletion_cleanup = _restore_deleted_files(root, store)
    # New instance requires credentials. Exported placeholders cannot accidentally
    # produce model requests or notifications after a later manual release.
    config = json.loads((root / "config.json").read_text())
    config["model"] = {}
    config["notification"] = {}
    config["backup"] = {}
    config["restored_requires_configuration"] = True
    _atomic_write(root / "config.json", _json(config).encode())
    attachment_results = []
    with store.db() as db:
        for row in db.execute("SELECT * FROM attachments"):
            path = root / row["path"]
            if not path.resolve().is_relative_to(root) or not path.is_file() or sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                raise ValueError("恢复附件缺失或校验失败：" + row["id"])
            attachment_results.append(row["id"])
    # Rebuild portable derived files after ledger merge; stale readable exports
    # must not bypass deletion filtering in the restored active directory.
    from .deletion import compact_deleted_data
    if not compact_deleted_data(store):
        raise RuntimeError("恢复副本删除压缩尚未完成；未发布实例")
    _atomic_write(root / "data.json", _json(_database_json(root / "personal.sqlite")).encode())
    with tempfile.TemporaryDirectory(prefix='dsh-restored-readable-') as temp:
        readable=Path(temp)/'personal.sqlite'
        _readable_snapshot(root/'personal.sqlite',readable)
        _atomic_write(root / "records.json", _json(RecordsStore(readable).export_data()).encode())
    _atomic_write(root / "ledger.json", _json(recovery_ledger(SimpleNamespace(store=store, records=records))).encode())
    _atomic_write(root / "diary.md", _markdown_from(root / "personal.sqlite", records).encode())
    report = {"restored": str(root), "isolated": True, "latest_ledger_supplied": latest_ledger is not None,
              "source_version": manifest["application_version"], "verified_attachments": attachment_results,
              "deletion_cleanup": deletion_cleanup,
              "duration_seconds": round(time.monotonic() - began, 3), "completed_at": _now(),
              "remaining": ["核对最新修订/删除账本的时间覆盖", "核对外部动作与任务成果回执", "重新配置凭据与原生 DSH 可恢复版本", "显式批准解除隔离；不会重放历史通知或任务"]}
    _atomic_write(root / "recovery-report.json", _json(report).encode())
    return report


def restore_bundle(content, destination, latest_ledger=None):
    """Build in staging and publish only an already-isolated, verified database.

    A crash during extraction cannot leave an executable old snapshot at the
    user's intended destination before isolation has been applied.
    """
    destination = Path(destination).expanduser().resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("恢复目录必须为空；不会覆盖正在运行或已有数据的实例")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".dsh-restore-", dir=destination.parent) as temp:
        staged = Path(temp) / "instance"
        report = _restore_unpublished(content, staged, latest_ledger)
        report["restored"] = str(destination)
        _atomic_write(staged / "recovery-report.json", _json(report).encode())
        source_manifest = json.loads((staged / "manifest.json").read_text())
        _atomic_write(staged / "source-manifest.json", _json(source_manifest).encode())
        current_manifest = dict(source_manifest, exported_at=_now(), restored_from=source_manifest["exported_at"], recovery_mode="isolated; merged ledger; current readable exports rebuilt")
        current_manifest["files"] = {}
        for path in staged.rglob("*"):
            if path.is_file() and path.name != "manifest.json":
                raw = path.read_bytes()
                current_manifest["files"][path.relative_to(staged).as_posix()] = {"sha256": sha256(raw).hexdigest(), "size": len(raw)}
        _atomic_write(staged / "manifest.json", _json(current_manifest).encode())
        # POSIX refuses to replace a directory that became nonempty concurrently.
        os.replace(staged, destination)
    return report


def release_restore(data, user_input, ledger_coverage_confirmed=False, effects_confirmed=False):
    """Explicit verification record; ambiguous historical effects never requeue."""
    if not user_input or not ledger_coverage_confirmed or not effects_confirmed:
        raise ValueError("须明确确认最新账本覆盖与外部动作核对，并保留用户输入")
    app = _app(data)
    if not app.store.meta("recovery_ledger_as_of"):
        raise ValueError("没有恢复账本覆盖记录")
    with maintenance_lock(data, exclusive=True):
        ledger = app.records.export_ledger()
        app.records.apply_ledger(ledger, release_quarantine=True, user_input=user_input)
        app.store.set_meta("recovery_confirmation", _json({"at": _now(), "input": user_input, "ledger_coverage_confirmed": True, "effects_confirmed": True}))
        app.store.set_meta("isolated", "false")
    return {"released": True, "historical_effects_requeued": False}


def _app(data):
    root = Path(data).expanduser().resolve()
    if not (root / "personal.sqlite").exists():
        raise ValueError("数据目录没有 personal.sqlite")
    store = Store(root / "personal.sqlite")
    records = RecordsStore(root / "personal.sqlite")
    def config():
        path = root / "config.json"
        return json.loads(path.read_text()) if path.exists() else {}
    return SimpleNamespace(data=root, store=store, records=records, config=config)


def main():
    parser = argparse.ArgumentParser(description="DSH 备份、可读导出、隔离恢复与校验")
    sub = parser.add_subparsers(dest="command", required=True)
    key = sub.add_parser("init-key"); key.add_argument("--path", required=True)
    for command in ("backup", "export", "ledger"):
        p = sub.add_parser(command); p.add_argument("--data", required=True); p.add_argument("--output")
        if command != "export":
            p.add_argument("--key", required=True); p.add_argument("--offsite")
    verify = sub.add_parser("verify"); verify.add_argument("--input", required=True); verify.add_argument("--key")
    restore = sub.add_parser("restore"); restore.add_argument("--input", required=True); restore.add_argument("--destination", required=True); restore.add_argument("--key"); restore.add_argument("--latest-ledger")
    release = sub.add_parser("release"); release.add_argument("--data", required=True); release.add_argument("--user-input", required=True); release.add_argument("--ledger-coverage-confirmed", action="store_true"); release.add_argument("--effects-confirmed", action="store_true")
    args = parser.parse_args()
    if args.command == "init-key":
        result = create_key(args.path)
    elif args.command == "backup":
        result = backup(_app(args.data), args.key, args.output, args.offsite)
    elif args.command == "ledger":
        result = protect_ledgers(_app(args.data), args.key, args.output, args.offsite)
    elif args.command == "export":
        if not args.output:
            raise ValueError("导出须指定 --output ZIP 路径")
        content = export_bundle(_app(args.data)); _atomic_write(args.output, content)
        result = {"exported": args.output, "bytes": len(content)}
    elif args.command in ("verify", "restore"):
        content = Path(args.input).read_bytes()
        if content.startswith(MAGIC):
            if not args.key:
                raise ValueError("加密备份需要 --key；密钥不会写入输出")
            content = decrypt(content, _key(args.key))
        if args.command == "verify":
            manifest = verify_bundle(content)
            result = {"verified": True, "source_version": manifest["application_version"], "files": len(manifest["files"]), "exported_at": manifest["exported_at"]}
        else:
            ledger = None
            if args.latest_ledger:
                raw = Path(args.latest_ledger).read_bytes()
                ledger = json.loads(decrypt(raw, _key(args.key)) if raw.startswith(MAGIC) else raw)
            result = restore_bundle(content, args.destination, ledger)
    else:
        result = release_restore(args.data, args.user_input, args.ledger_coverage_confirmed, args.effects_confirmed)
    print(_json(result))
    if result.get("offsite_error"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
