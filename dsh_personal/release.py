"""Versioned local installation and offline, checkpointed schema migration.

Schema 0 is an explicit pre-release flat-journal interchange used by the
migration probe, not a claim that a previous DSH Personal release existed.
Only schema 0 -> 1 and restoration of its own verified checkpoint are supported.
Cross-version DSH migrations require an independently tested upstream version.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime, timezone
import fcntl
from hashlib import sha256
import json
import os
import ast
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from uuid import uuid5, NAMESPACE_URL

from . import __version__
from .core import Store
from .records import RecordsStore
from .operations import maintenance_lock, _atomic_write, _database_snapshot

ROOT = Path(__file__).resolve().parents[1]
SCHEMA0_DDL = """
CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
INSERT INTO meta VALUES ('schema_version','0');
CREATE TABLE legacy_entries(
  entry_id TEXT PRIMARY KEY,routine TEXT NOT NULL,business_date TEXT NOT NULL,
  received_at TEXT NOT NULL,role TEXT NOT NULL,body TEXT NOT NULL,submission_id TEXT);
"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _source_files(source):
    ignored = {"node_modules", "__pycache__", ".git", ".venv"}
    for dirname in ("dsh_personal", "runtime", "web", "docs", "scripts", "tests"):
        root = source / dirname
        if not root.exists():
            continue
        for current, directories, filenames in os.walk(root, followlinks=False):
            directories[:] = sorted(name for name in directories if name not in ignored)
            for name in directories:
                if (Path(current) / name).is_symlink():
                    raise ValueError("发布源码不跟随符号链接：" + str((Path(current) / name).relative_to(source)))
            for name in sorted(filenames):
                file = Path(current) / name
                relative = file.relative_to(source)
                if file.is_symlink():
                    raise ValueError("发布源码不跟随符号链接：" + str(relative))
                if not file.name.startswith(".env") and file.suffix not in {".pyc", ".key", ".pem"}:
                    yield file, relative
    for name in ("README.md", "pyproject.toml", "requirements.txt", "requirements.lock", "AGENTS.md"):
        if (source / name).is_file():
            yield source / name, Path(name)


def verify_release(path):
    root = Path(path).resolve()
    manifest = json.loads((root / "release-manifest.json").read_text())
    if manifest.get("format") != "dsh-personal-release" or manifest.get("format_version") != 1:
        raise ValueError("未知应用版本清单")
    for name, expected in manifest["files"].items():
        file = root / name
        if not file.resolve().is_relative_to(root) or not file.is_file() or sha256(file.read_bytes()).hexdigest() != expected:
            raise ValueError("应用文件完整性失败：" + name)
    dependency = manifest.get("runtime_dependency")
    if dependency:
        native = root / "runtime/npm/node_modules/@deepseek-ai/dsh/package.json"
        if not native.exists() or json.loads(native.read_text())["version"] != dependency["dsh_version"]:
            raise ValueError("固定 DSH 依赖不存在或版本不一致")
    return manifest


def install_release(source, destination, data=None, activate=False):
    source, destination = Path(source).resolve(), Path(destination).expanduser().resolve()
    if not (source / "dsh_personal/__init__.py").exists():
        raise ValueError("不是 DSH Personal 源码目录")
    if destination.is_relative_to(source):
        raise ValueError("版本仓库必须位于源码目录之外，避免递归复制")
    files = {str(relative): sha256(file.read_bytes()).hexdigest() for file, relative in _source_files(source)}
    content_hash = sha256(_json(files).encode()).hexdigest()
    app_version = None
    for node in ast.parse((source / "dsh_personal/__init__.py").read_text()).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets):
            app_version = ast.literal_eval(node.value)
    if not isinstance(app_version, str) or not app_version:
        raise ValueError("源码没有明确应用版本")
    release_id = app_version + "-" + content_hash[:12]
    target = destination / release_id
    repeated = target.exists()
    if not repeated:
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix=".release-", dir=destination) as temporary:
            staged = Path(temporary) / "application"
            staged.mkdir(mode=0o700)
            for file, relative in _source_files(source):
                (staged / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file, staged / relative)
            dependency = None
            # The pinned 800 MiB native runtime is immutable shared infrastructure;
            # app releases share this exact already-installed dependency. A clean
            # machine materializes package-lock.json using runtime/install.sh.
            native = source / "runtime/npm/node_modules/@deepseek-ai/dsh/package.json"
            if native.exists():
                installed = json.loads(native.read_text())
                module_root = source / "runtime/npm/node_modules"
                (staged / "runtime/npm").mkdir(parents=True, exist_ok=True)
                os.symlink(module_root, staged / "runtime/npm/node_modules", target_is_directory=True)
                dependency = {"kind": "pinned_shared_local_runtime", "dsh_version": installed["version"], "path": str(module_root), "cross_version_rollback_verified": False}
            command = subprocess.run(["git", "-C", str(source), "rev-parse", "--verify", "HEAD"], capture_output=True, text=True)
            commit = command.stdout.strip() if command.returncode == 0 else None
            manifest = {"format": "dsh-personal-release", "format_version": 1, "release_id": release_id,
                        "application_version": app_version, "source_commit": commit,
                        "source_fingerprint": content_hash, "installed_at": _stamp(),
                        "supported_database_schemas": [1], "files": files, "runtime_dependency": dependency}
            _atomic_write(staged / "release-manifest.json", _json(manifest).encode())
            # Compilation validates packaging without starting services or models.
            smoke = subprocess.run([sys.executable, "-c", "from dsh_personal.core import Store; from dsh_personal.records import RecordsStore; from dsh_personal.operations import restore_bundle"], cwd=staged, capture_output=True, text=True)
            if smoke.returncode:
                raise RuntimeError("候选应用导入检查失败：" + smoke.stderr[-1000:])
            verify_release(staged)
            os.replace(staged, target)
    manifest = verify_release(target)
    if activate:
        if data is None:
            raise ValueError("激活版本须给出数据目录")
        activate_release(data, target)
    return {"release": str(target), "release_id": manifest["release_id"], "reused_identical_installation": repeated,
            "application_version": manifest["application_version"], "activated": activate,
            "native_runtime": manifest.get("runtime_dependency"),
            "remaining": [] if manifest.get("runtime_dependency") else ["在候选目录执行 runtime/install.sh 安装锁定 DSH 依赖；不升级全局 Node"]}


def inspect_schema(path):
    path = Path(path)
    if not path.is_file():
        raise ValueError("数据库不存在")
    with sqlite3.connect(path) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if "meta" not in tables:
            raise ValueError("数据库缺少明确 schema_version，禁止猜测旧格式")
        row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if not row:
            raise ValueError("数据库缺少 schema_version")
        version = int(row[0])
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise ValueError("数据库完整性检查失败")
    return {"schema_version": version, "tables": sorted(tables), "integrity": integrity}


@contextmanager
def _offline(data):
    data = Path(data)
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    handles = []
    try:
        for path in (data / "application.lock", data / "dsh/web-host.lock"):
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            handle = path.open("a")
            handles.append(handle)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("升级/回退要求先停止本项目应用及 DSH 宿主；不会覆盖正在运行的实例") from exc
        with maintenance_lock(data, exclusive=True):
            yield
    finally:
        for handle in reversed(handles):
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()


def _checkpoint_wal(path):
    with sqlite3.connect(path, timeout=30) as db:
        busy, _, _ = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if busy:
            raise RuntimeError("数据库仍被使用，不能安全替换")
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            sidecar.unlink()


def activate_release(data, release):
    root, release = Path(data).resolve(), Path(release).resolve()
    manifest = verify_release(release)
    with _offline(root):
        schema = inspect_schema(root / "personal.sqlite")["schema_version"]
        if schema not in manifest["supported_database_schemas"]:
            raise ValueError("应用版本与数据库 schema 不兼容，不能激活")
        _atomic_write(root / "upgrade-in-progress.json", _json({"action": "activate", "target_release": str(release), "started_at": _stamp()}).encode())
        _activate_unlocked(root, release, manifest)
        (root / "upgrade-in-progress.json").unlink()
    return {"active_release": str(release), "release_id": manifest["release_id"]}


def _activate_unlocked(root, release, manifest):
    temporary = root / ".current-release-next"
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    os.symlink(release, temporary, target_is_directory=True)
    os.replace(temporary, root / "current-release")
    _atomic_write(root / "active-release.json", _json({"path": str(release), "release_id": manifest["release_id"], "activated_at": _stamp(), "schema_versions": manifest["supported_database_schemas"]}).encode())


def _migrate_zero(candidate):
    state = inspect_schema(candidate)
    if set(state["tables"]) != {"meta", "legacy_entries"}:
        raise ValueError("schema 0 仅支持明确的 meta + legacy_entries 开发期格式；其他布局需单独迁移")
    with sqlite3.connect(candidate) as db:
        db.row_factory = sqlite3.Row
        expected = {"entry_id", "routine", "business_date", "received_at", "role", "body", "submission_id"}
        if {r[1] for r in db.execute("PRAGMA table_info(legacy_entries)")} != expected:
            raise ValueError("schema 0 字段不匹配")
        entries = [dict(r) for r in db.execute("SELECT * FROM legacy_entries ORDER BY received_at,entry_id")]
    store = Store(candidate)
    RecordsStore(candidate)
    with store.db(True) as db:
        for entry in entries:
            if entry["routine"] not in {"A1", "A2", "A3", "CHAT"} or entry["role"] not in {"user", "assistant", "tool"}:
                raise ValueError("schema 0 包含未知例程或角色")
            date.fromisoformat(entry["business_date"])
            when = datetime.fromisoformat(entry["received_at"].replace("Z", "+00:00"))
            if when.tzinfo is None:
                raise ValueError("旧消息时间缺少时区，不能猜测")
            aid = str(uuid5(NAMESPACE_URL, "dsh-personal/schema0/" + entry["routine"] + "/" + entry["business_date"]))
            db.execute("INSERT OR IGNORE INTO activities(id,routine,business_date,status,created_at,origin,notification_state) VALUES (?,?,?,'partial',?,'schema0_migration','historical_silent')", (aid, entry["routine"], entry["business_date"], entry["received_at"]))
            db.execute("INSERT INTO messages(id,activity_id,submission_id,role,body,received_at,processing_state) VALUES (?,?,?,?,?,?,'migrated_unlinked')", (entry["entry_id"], aid, entry["submission_id"], entry["role"], entry["body"], entry["received_at"]))
            if entry["role"] == "user":
                db.execute("UPDATE activities SET version=version+1 WHERE id=?", (aid,))
        if db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] != len(entries):
            raise ValueError("迁移消息数量核对失败")
        db.execute("DROP TABLE legacy_entries")
        db.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
        db.execute("INSERT OR REPLACE INTO meta VALUES ('isolated','true')")
        db.execute("INSERT OR REPLACE INTO meta VALUES ('migration_source','schema0_development_flat_journal')")
    return {"messages_migrated": len(entries), "activities": len({(x["routine"], x["business_date"]) for x in entries}), "isolated": True}


def migrate(data, target_schema=1, candidate_release=None):
    root = Path(data).expanduser().resolve()
    database = root / "personal.sqlite"
    with _offline(root):
        before = inspect_schema(database)
        if target_schema != 1:
            raise ValueError("仅实现目标 schema 1，不能假定其他升级路径")
        if before["schema_version"] == target_schema:
            return {"changed": False, "schema_version": target_schema, "integrity": "ok", "notice": "当前数据已为 schema 1；这不构成跨 DSH 版本升级证明"}
        if before["schema_version"] != 0:
            raise ValueError("没有该源 schema 的已验证迁移路径")
        if candidate_release:
            manifest = verify_release(candidate_release)
            if target_schema not in manifest["supported_database_schemas"]:
                raise ValueError("候选应用不支持目标 schema")
        checkpoint = root / "upgrade-checkpoints" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        checkpoint.mkdir(parents=True, mode=0o700)
        _database_snapshot(database, checkpoint / "before.sqlite")
        active = json.loads((root / "active-release.json").read_text()) if (root / "active-release.json").exists() else None
        metadata = {"format": "dsh-personal-upgrade-checkpoint", "created_at": _stamp(), "source_schema": before["schema_version"], "target_schema": target_schema,
                    "source_application": active, "target_application": str(Path(candidate_release).resolve()) if candidate_release else None,
                    "database_sha256": sha256((checkpoint / "before.sqlite").read_bytes()).hexdigest(),
                    "source_format": "explicit development schema 0; not a historical released product version"}
        _atomic_write(checkpoint / "checkpoint.json", _json(metadata).encode())
        candidate = checkpoint / "candidate.sqlite"
        _database_snapshot(checkpoint / "before.sqlite", candidate)
        details = _migrate_zero(candidate)
        after = inspect_schema(candidate)
        _atomic_write(root / "upgrade-in-progress.json", _json({"action": "migrate", "checkpoint": str(checkpoint), "source_schema": 0, "target_schema": 1, "started_at": _stamp()}).encode())
        _checkpoint_wal(database)
        _checkpoint_wal(candidate)
        os.replace(candidate, database)
        os.chmod(database, 0o600)
        if candidate_release:
            _activate_unlocked(root, Path(candidate_release).resolve(), manifest)
        result = {"changed": True, "source_schema": 0, "target_schema": 1, "checkpoint": str(checkpoint), "candidate_verified_before_publish": True,
                  "application_activated": bool(candidate_release), "details": details, "integrity": after["integrity"],
                  "remaining": ["核对原文与日期；显式解除数据隔离", "跨实际 DSH 发布版本迁移尚未验证"]}
        _atomic_write(checkpoint / "migration-result.json", _json(result).encode())
        (root / "upgrade-in-progress.json").unlink()
        return result


def rollback(data, checkpoint):
    root, checkpoint = Path(data).expanduser().resolve(), Path(checkpoint).resolve()
    metadata = json.loads((checkpoint / "checkpoint.json").read_text())
    previous = checkpoint / "before.sqlite"
    if metadata.get("format") != "dsh-personal-upgrade-checkpoint" or sha256(previous.read_bytes()).hexdigest() != metadata["database_sha256"]:
        raise ValueError("回退检查点验证失败")
    with _offline(root):
        source_application = metadata.get("source_application")
        manifest = None
        if source_application:
            manifest = verify_release(source_application["path"])
            if metadata["source_schema"] not in manifest["supported_database_schemas"]:
                raise ValueError("历史应用与回退数据库不兼容；拒绝只回退部分")
        current = root / "personal.sqlite"
        _database_snapshot(current, checkpoint / "before-rollback.sqlite")
        _database_snapshot(previous, checkpoint / "rollback-candidate.sqlite")
        candidate = checkpoint / "rollback-candidate.sqlite"
        with sqlite3.connect(candidate) as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('isolated','true')")
        _atomic_write(root / "upgrade-in-progress.json", _json({"action": "rollback", "checkpoint": str(checkpoint), "started_at": _stamp()}).encode())
        _checkpoint_wal(current)
        _checkpoint_wal(candidate)
        os.replace(candidate, current)
        if manifest:
            _activate_unlocked(root, Path(source_application["path"]), manifest)
        else:
            # Do not leave a schema-1 executable pointer presented as compatible
            # with restored schema-0 data when no historical app was available.
            (root / "current-release").unlink(missing_ok=True)
            (root / "active-release.json").unlink(missing_ok=True)
        result = {"database_rolled_back": True, "schema_version": metadata["source_schema"], "isolated": True,
                  "application_rolled_back": manifest is not None, "application": source_application,
                  "remaining": [] if manifest else ["此开发 schema0 没有实际历史发布应用；已撤去当前版本指针，不宣称完整应用回滚通过"],
                  "newer_state_preserved_at": str(checkpoint / "before-rollback.sqlite")}
        _atomic_write(checkpoint / "rollback-result.json", _json(result).encode())
        (root / "upgrade-in-progress.json").unlink()
        return result


def main():
    parser = argparse.ArgumentParser(description="固定应用版本安装、离线 schema 迁移与关联回退")
    sub = parser.add_subparsers(dest="command", required=True)
    install = sub.add_parser("install"); install.add_argument("--source", default=str(ROOT)); install.add_argument("--destination", required=True); install.add_argument("--data"); install.add_argument("--activate", action="store_true")
    verify = sub.add_parser("verify"); verify.add_argument("--release", required=True)
    activate = sub.add_parser("activate"); activate.add_argument("--data", required=True); activate.add_argument("--release", required=True)
    migration = sub.add_parser("migrate"); migration.add_argument("--data", required=True); migration.add_argument("--target-schema", type=int, default=1); migration.add_argument("--candidate-release")
    back = sub.add_parser("rollback"); back.add_argument("--data", required=True); back.add_argument("--checkpoint", required=True)
    args = parser.parse_args()
    if args.command == "install": result = install_release(args.source, args.destination, args.data, args.activate)
    elif args.command == "verify":
        manifest = verify_release(args.release); result = {"verified": True, "release_id": manifest["release_id"], "schemas": manifest["supported_database_schemas"]}
    elif args.command == "activate": result = activate_release(args.data, args.release)
    elif args.command == "migrate": result = migrate(args.data, args.target_schema, args.candidate_release)
    else: result = rollback(args.data, args.checkpoint)
    print(_json(result))


if __name__ == "__main__":
    main()
