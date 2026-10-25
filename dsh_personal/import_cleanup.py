"""Checkpointed import-batch withdrawal, including consumed model contexts."""
from hashlib import sha256
import json

from .core import dumps
from .deletion import apply_source_invalidation, finish_affected_cleanup
from .operations import maintenance_lock, protect_ledgers


def plan_import_removal(app, batch_id):
    """Read-only scope preview, including dependencies saved before a retry."""
    with app.store.db() as db:
        batch = db.execute("SELECT status FROM records_import_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if not batch:
            raise KeyError("导入批次不存在")
        roots = ["import:" + row[0] for row in db.execute("SELECT message_id FROM records_batch_items WHERE batch_id=? AND created=1 ORDER BY message_id", (batch_id,))]
        count = db.execute("SELECT count(*) FROM records_import_messages m JOIN records_batch_items b ON b.message_id=m.message_id WHERE b.batch_id=? AND b.created=1 AND m.deleted=0", (batch_id,)).fetchone()[0]
        prior = db.execute("SELECT value FROM meta WHERE key=?", ("import_removal_plan:" + batch_id,)).fetchone()
        previous = json.loads(prior[0]) if prior else {}
    refs = set(app.records.dependent_refs(roots)) | set(previous.get("source_refs", []))
    with app.store.db() as db:
        contexts = db.execute("SELECT session_id,source_ref FROM context_sources").fetchall()
        known = {row["session_id"] for row in contexts}
        native = {row["session_id"] for row in contexts if row["source_ref"] in refs}
        activities = [dict(row) for row in db.execute("SELECT id,session_id,version FROM activities")]
        runs = [dict(row) for row in db.execute("SELECT task_id,session_id FROM task_runs WHERE session_id IS NOT NULL AND status!='source_deleted'")]
        legacy = {row["session_id"] for row in activities + runs if row.get("session_id") and row["session_id"] not in known} if any(ref.startswith("profile:") for ref in refs) else set()
        if any(ref.startswith("profile:") for ref in refs):
            legacy.update(row["session_id"] for row in contexts if row["source_ref"] == "legacy_profile_context")
        legacy.update(previous.get("legacy_session_ids", []))
        native.update(legacy)
        activity_ids = {row["id"] for row in activities if row["session_id"] in native}
        task_ids = {row["task_id"] for row in runs if row["session_id"] in native}
        tasks = [dict(row) for row in db.execute("SELECT id,source_activity_id,version FROM tasks")]
        task_ids.update(row["id"] for row in tasks if row["source_activity_id"] in activity_ids)
        native.update(row["session_id"] for row in runs if row["task_id"] in task_ids)
    if legacy:
        refs.add("possible_legacy_import:" + batch_id)
    plan = {
        "batch_id": batch_id, "batch_status": batch["status"], "records_to_remove": count,
        "source_refs": sorted(refs), "activity_ids": sorted(activity_ids), "task_ids": sorted(task_ids),
        "native_session_ids": sorted(native), "legacy_session_ids": sorted(legacy), "attachment_ids": [],
        "activity_versions": {row["id"]: row["version"] for row in activities if row["id"] in activity_ids},
        "task_versions": {row["id"]: row["version"] for row in tasks if row["id"] in task_ids},
        "scope": "撤销此批次新增导入记录、同来源会话的助手/工具派生及画像/成长引用；已使用这些资料的模型会话离线清理后换新上下文，关联旧成果失效。其他独立用户原话、修订、委托、评价和仅重复导入的原记录保留。旧版本未记录上下文来源的会话会保守列出；历史加密备份按保留期退出。",
    }
    plan["token"] = sha256(dumps(plan).encode()).hexdigest()
    return plan


def remove_import_batch(app, request):
    batch_id = request["batch_id"]
    with maintenance_lock(app.data, exclusive=True):
        completed = app.store.meta("import_removal_done:" + batch_id)
        if completed:
            result = json.loads(completed)
            result["sqlite_cleanup_pending"] = app.store.meta("deletion_compaction_pending") == "true"
        else:
            plan = plan_import_removal(app, batch_id)
            if request.get("plan_token") and request["plan_token"] != plan["token"]:
                raise ValueError("关联记录已经变化，请重新核对导入撤销范围")
            with app.store.db(True) as db:
                # Batch deletion, context tombstones and derived invalidation commit
                # together: a crash cannot leave a half-applied SQL deletion graph.
                db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", ("import_removal_plan:" + batch_id, dumps(plan)))
                for sid in plan["legacy_session_ids"]:
                    db.execute("INSERT OR IGNORE INTO context_sources VALUES(?,?)", (sid, "possible_legacy_import:" + batch_id))
                result = app.records.remove_batch(batch_id, connection=db)
                affected = apply_source_invalidation(db, plan["source_refs"], app.store.stamp())
                for kind, key in (("message", "message_ids"), ("activity", "activity_ids"), ("task", "task_ids")):
                    for ident in affected[key]:
                        app.records.delete_source(kind + ":" + ident, preserve_source=kind in ("activity", "task"), connection=db)
            result.update(finish_affected_cleanup(app, affected))
            result.update(affected=affected, scope=plan["scope"])
            # Preserve retry identity even after the imported records and profiles
            # have disappeared. Stored metadata contains IDs/counts, never bodies.
            app.store.set_meta("import_removal_result:" + batch_id, dumps(result))
            if not result["native_cleanup_pending"]:
                app.store.set_meta("import_removal_done:" + batch_id, dumps(result))
    key = app.config().get("backup", {}).get("key_file")
    if key:
        try:
            protect_ledgers(app, key)
            result["latest_ledger_protected"] = True
        except Exception:
            result["latest_ledger_protected"] = False
    else:
        result["latest_ledger_protected"] = False
    with maintenance_lock(app.data):
        app.store.set_meta("import_removal_result:" + batch_id, dumps(result))
        if not result["native_cleanup_pending"]:
            app.store.set_meta("import_removal_done:" + batch_id, dumps(result))
    return result
