"""SQL-only deletion of chat directives and copied task requirements.

Callers own the write transaction and the application maintenance checkpoint.
Requirement snapshots copy earlier values, so one withdrawn source invalidates
the task's entire existing requirements history. Identity-only revision rows keep
the version monotonic; independent messages, authorizations and reviews survive.
Exact revision tombstones make replay safe for later, explicitly revised work.
"""
from __future__ import annotations

import uuid
import json
import re

REQUIREMENT_FIELDS = ("goal", "inputs", "deliverable", "acceptance")
_HOLD_ERROR = "需求来源已删除；当前需求及旧成果失效，需明确重新委托后继续"


def _tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _tomb(db, kind, ident, stamp):
    db.execute("INSERT OR IGNORE INTO tombstones VALUES(?,?,?,?,?)",
               (str(uuid.uuid4()), kind, ident, None, stamp))


def _empty():
    return {key: set() for key in ("activity_ids", "task_ids", "message_ids",
                                  "native_session_ids", "attachment_ids")}


def _identity(ref):
    return re.sub(r"^(message|import|profile|growth|activity|task|outcome):", "", ref)


def _dependent_requests(db, source_ids, source_refs):
    """Control replies can quote earlier preferences without a native session."""
    tables = _tables(db)
    withdrawn = set(source_ids) | {_identity(ref) for ref in source_refs}
    if "deletion_context_refs" in tables:
        for mid in source_ids:
            withdrawn.update(_identity(r[0]) for r in db.execute("SELECT ref FROM deletion_context_refs WHERE message_id=?", (mid,)))
    if "records_tombstones" in tables:
        withdrawn.update(r[0] for r in db.execute("SELECT object_id FROM records_tombstones"))
    if "records_profile" in tables:
        profiles = db.execute("SELECT item_id,source_refs FROM records_profile").fetchall()
        while True:
            derived = {r["item_id"] for r in profiles if withdrawn.intersection(_identity(ref) for ref in json.loads(r["source_refs"]))}
            if derived <= withdrawn:
                break
            withdrawn.update(derived)
    dependent = set(source_ids)
    if "directive_requests" not in tables:
        return dependent
    for row in db.execute("SELECT message_id,decision FROM directive_requests WHERE decision IS NOT NULL"):
        try:
            decision = json.loads(row["decision"])
        except (ValueError, TypeError):
            continue
        if not isinstance(decision, dict):
            continue
        refs = decision.get("source_refs", [])
        refs = refs if isinstance(refs, list) else []
        identities = {_identity(ref) for ref in refs if isinstance(ref, str)}
        # Compatibility for the first candidate's mutation receipts. Showing
        # preferences requires explicit program-written source_refs as well.
        for item in decision.get("receipt", []) if isinstance(decision.get("receipt"), list) else []:
            if isinstance(item, dict) and item.get("kind") in ("preference", "task") and isinstance(item.get("id"), str):
                identities.add(item["id"])
        if withdrawn.intersection(identities):
            dependent.add(row["message_id"])
    return dependent


def requirement_version(db, task_id):
    if "task_requirement_revisions" not in _tables(db):
        return 0
    return db.execute("SELECT COALESCE(MAX(requirements_version),0) FROM task_requirement_revisions WHERE task_id=?",
                      (task_id,)).fetchone()[0]


def directive_task_ids(db, message_id):
    """Direct provenance works even when the task belongs to another activity."""
    if "task_requirement_revisions" not in _tables(db):
        return set()
    return {r[0] for r in db.execute(
        "SELECT task_id FROM task_requirement_revisions WHERE source_message_id=?",
        (message_id,))}


def scrub_receipt(db, receipt_id, stamp, affected):
    if not receipt_id:
        return
    # A control receipt can quote the requirement. Do not erase an unrelated
    # user original even if a malformed record points to it.
    receipt = db.execute("SELECT role FROM messages WHERE id=?", (receipt_id,)).fetchone()
    if receipt is None or receipt[0] in ("assistant", "tool"):
        _tomb(db, "derived_message", receipt_id, stamp)
        db.execute("UPDATE messages SET body='',deleted=1,processing_state='deleted',error=NULL WHERE id=? AND role IN ('assistant','tool')", (receipt_id,))
        affected["message_ids"].add(receipt_id)


def _scrub_requests(db, source_ids, stamp, affected):
    if "directive_requests" not in _tables(db):
        return
    for mid in source_ids:
        row = db.execute("SELECT receipt_id FROM directive_requests WHERE message_id=?", (mid,)).fetchone()
        if row is None:
            continue
        _tomb(db, "directive_request", mid, stamp)
        scrub_receipt(db, row[0], stamp, affected)
        db.execute("UPDATE directive_requests SET status='deleted',decision=NULL,receipt_id=NULL,updated_at=? WHERE message_id=?", (stamp, mid))


def _invalidate_requirements(db, task_id, through_version, stamp, affected):
    tables = _tables(db)
    if "task_requirement_revisions" not in tables:
        return
    rows = db.execute("SELECT * FROM task_requirement_revisions WHERE task_id=? AND requirements_version<=?",
                      (task_id, through_version)).fetchall()
    _scrub_requests(db, {r["source_message_id"] for r in rows}, stamp, affected)
    db.execute("UPDATE task_requirement_revisions SET before_json='{}',after_json='{}',user_input='' WHERE task_id=? AND requirements_version<=?",
               (task_id, through_version))
    task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if task is None or requirement_version(db, task_id) > through_version:
        return
    # Only affected tasks lose their requirements, not unrelated user-authored
    # task fields merely sharing words with the withdrawn message.
    db.execute("UPDATE tasks SET goal='',inputs='',deliverable='',acceptance='',result=NULL,status='recovery_hold',error=? WHERE id=?",
               (_HOLD_ERROR, task_id))
    affected["task_ids"].add(task_id)
    _tomb(db, "task_derivatives", task_id, stamp)
    _tomb(db, "task_version", task_id + ":" + str(task["version"]), stamp)
    for run in db.execute("SELECT id,version,session_id FROM task_runs WHERE task_id=?", (task_id,)).fetchall():
        _tomb(db, "task_run", run["id"], stamp)
        _tomb(db, "task_version", task_id + ":" + str(run["version"]), stamp)
        if run["session_id"]:
            _tomb(db, "native_session", run["session_id"], stamp)
            affected["native_session_ids"].add(run["session_id"])
    db.execute("UPDATE task_runs SET body=NULL,status='source_deleted' WHERE task_id=?", (task_id,))


def scrub_directives(db, source_ids, stamp, *, source_refs=()):
    """Invalidate newly withdrawn sources, including when no message row exists.

    Already scrubbed rows must not grow the deletion watermark on a later replay:
    a future independent requirements revision is not part of the old deletion.
    """
    affected = _empty()
    source_ids = set(source_ids)
    _scrub_requests(db, _dependent_requests(db, source_ids, source_refs), stamp, affected)
    if "task_requirement_revisions" in _tables(db):
        tasks = set()
        for mid in source_ids:
            tasks.update(r[0] for r in db.execute(
                "SELECT task_id FROM task_requirement_revisions WHERE source_message_id=? AND (before_json!='{}' OR after_json!='{}' OR user_input!='')",
                (mid,)))
        for tid in tasks:
            revision = requirement_version(db, tid)
            _tomb(db, "task_requirements", tid + ":" + str(revision), stamp)
            _invalidate_requirements(db, tid, revision, stamp, affected)
        _scrub_requests(db, _dependent_requests(db, (), ["task:" + tid for tid in tasks]), stamp, affected)
    return affected


def replay_directive_tombstones(db, stamp):
    """Deletion wins over merged old or new ledgers, without requiring raw text."""
    affected = _empty()
    source_ids = {r[0] for r in db.execute("SELECT target_id FROM tombstones WHERE target_type IN ('message','directive_request')")}
    _scrub_requests(db, _dependent_requests(db, source_ids, ()), stamp, affected)
    for row in db.execute("SELECT target_id,created_at FROM tombstones WHERE target_type='task_requirements'").fetchall():
        tid, sep, version = row[0].rpartition(":")
        if not sep or not version.isdigit() or int(version) < 1:
            raise ValueError("删除账本任务需求版本身份无效")
        _invalidate_requirements(db, tid, int(version), row[1], affected)
    return affected
