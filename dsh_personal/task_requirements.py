"""Versioned, explicitly authorized changes to an existing task's requirements.

The caller owns authentication, message persistence and the BEGIN IMMEDIATE
transaction. This module neither commits nor starts work. Requirements revisions
are independent of delivered task versions and never change earlier runs or
reviews. Claiming work must read ``snapshot`` inside the same write transaction
that marks the task running; the worker must use that captured snapshot rather
than a task row fetched before the claim.
"""
from __future__ import annotations

from datetime import datetime
import json
import sqlite3


FIELDS = ("goal", "inputs", "deliverable", "acceptance")
MAX_TEXT_LENGTH = 100_000
EDITABLE_STATES = frozenset({
    "queued", "needs_input", "permission_required", "revision",
    "awaiting_acceptance", "accepted",
})


class TaskRequirementsError(ValueError):
    """A safe, actionable business error; ``code`` is stable for callers."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def initialize(db: sqlite3.Connection) -> None:
    """Add schema-1 tables without executescript's implicit transaction commit."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS task_requirement_revisions (
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            requirements_version INTEGER NOT NULL CHECK(requirements_version > 0),
            before_json TEXT NOT NULL,
            after_json TEXT NOT NULL,
            user_input TEXT NOT NULL,
            source_message_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(task_id, requirements_version)
        )
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS task_requirement_source
        ON task_requirement_revisions(source_message_id)
    """)


def snapshot(db: sqlite3.Connection, task_id: str) -> dict:
    """Read current requirements, treating legacy tasks as revision zero.

    The single SELECT prevents mixed field/revision reads. It does not return
    artifacts, authorization or scheduling fields as editable requirements.
    """
    row = db.execute("""
        SELECT t.id, t.goal, t.inputs, t.deliverable, t.acceptance, t.status,
               COALESCE((SELECT MAX(r.requirements_version)
                         FROM task_requirement_revisions r
                         WHERE r.task_id=t.id), 0)
        FROM tasks t WHERE t.id=?
    """, (task_id,)).fetchone()
    if row is None:
        raise TaskRequirementsError("task_missing", "任务不存在，请重新选择任务。")
    return dict(zip(("task_id", *FIELDS, "status", "requirements_version"), row))


def _text(value, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > MAX_TEXT_LENGTH:
        raise TaskRequirementsError("invalid_patch", f"{label}须为不超过 100000 字符的文本。")
    if not allow_empty and not value.strip():
        raise TaskRequirementsError("invalid_patch", f"{label}不能为空。")
    return value


def apply(db: sqlite3.Connection, task_id: str, expected_revision: int,
          patch: dict, user_input: str, source_message_id: str, stamp: str) -> dict:
    """Apply a requirements-only patch inside the caller's write transaction.

    Editing a delivered task explicitly requests a new delivery, preserving the
    old result and its acceptance. A permission wait is retained; changing prose
    cannot grant execution permission. Queued tasks keep their existing schedule.
    No-op patches are rejected instead of silently restarting a completed task.
    """
    if not db.in_transaction:
        raise TaskRequirementsError("transaction_required", "需求修订必须在业务写入事务中执行。")
    if type(expected_revision) is not int or expected_revision < 0:
        raise TaskRequirementsError("invalid_revision", "请提供有效的需求版本号。")
    if not isinstance(patch, dict) or not patch or set(patch) - set(FIELDS):
        raise TaskRequirementsError("invalid_patch", "只可修订目标、输入、交付内容和验收条件。")
    values = {key: _text(value, key, allow_empty=key == "inputs") for key, value in patch.items()}
    user_input = _text(user_input, "用户原始要求")
    if not isinstance(source_message_id, str) or not source_message_id.strip() or len(source_message_id) > 128:
        raise TaskRequirementsError("invalid_source", "缺少有效的用户消息来源。")
    try:
        parsed_stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if parsed_stamp.utcoffset() is None:
            raise ValueError("timezone required")
    except (AttributeError, TypeError, ValueError):
        raise TaskRequirementsError("invalid_stamp", "修订时间须包含明确时区。") from None

    before = snapshot(db, task_id)
    if before["requirements_version"] != expected_revision:
        raise TaskRequirementsError("requirements_conflict", "任务需求已更新，请重新核对当前需求后再修改。")
    if before["status"] == "running":
        raise TaskRequirementsError("task_running", "任务正在执行，请待本次执行结束后再修订需求。")
    if before["status"] == "cancelled":
        raise TaskRequirementsError("task_cancelled", "任务已取消；如需继续，请明确重新委托。")
    if before["status"] not in EDITABLE_STATES:
        raise TaskRequirementsError("task_blocked", "任务处于隔离或待核对状态，不能通过需求修订恢复执行。")
    source = db.execute("SELECT role, deleted, body FROM messages WHERE id=?", (source_message_id,)).fetchone()
    if source is None or source[0] != "user" or source[1] or source[2] != user_input:
        raise TaskRequirementsError("invalid_source", "需求修订必须关联未删除的用户原始消息。")
    if all(before[key] == value for key, value in values.items()):
        raise TaskRequirementsError("no_changes", "需求内容未改变，没有创建新的需求版本。")

    state = before["status"]
    if state in {"accepted", "awaiting_acceptance"}:
        state = "revision"
    elif state == "needs_input":
        state = "queued"
    after = {**before, **values, "requirements_version": expected_revision + 1, "status": state}
    encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    db.execute("""
        INSERT INTO task_requirement_revisions
            (task_id, requirements_version, before_json, after_json,
             user_input, source_message_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (task_id, after["requirements_version"], encode(before), encode(after),
          user_input, source_message_id, stamp))
    db.execute("""
        UPDATE tasks SET goal=?, inputs=?, deliverable=?, acceptance=?, status=?,
            run_at=CASE WHEN ? IN ('accepted','awaiting_acceptance') THEN ? ELSE run_at END,
            error=CASE WHEN ?='permission_required' THEN error ELSE NULL END
        WHERE id=?
    """, (*[after[key] for key in FIELDS], state, before["status"], stamp, state, task_id))
    return after
