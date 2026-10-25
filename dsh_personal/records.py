"""Source-preserving imports, scoped profiles, and versioned growth evidence.

No model calls or filesystem imports are performed by this module. Uploads are
untrusted data. The live conversation store remains owned by the application;
source_refs link to it rather than making independently editable copies.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, date, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
LINES = ("doctorate", "startup", "culture")
GROWTH_KINDS = ("goal", "capability", "plan", "action", "outcome", "evaluation", "review", "current_state")
PROFILE_CATEGORIES = ("background", "preference", "project_rule", "stage_goal", "hypothesis")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return sha256((value if isinstance(value, str) else _json(value)).encode()).hexdigest()


def _timestamp(value):
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            if value > 10**11:  # Some exports use milliseconds.
                value /= 1000
            dt = datetime.fromtimestamp(value, timezone.utc)
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                # Do not invent a timezone for ambiguous historical timestamps.
                return None
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError, TypeError, OSError):
        return None


def _original_day(timestamp):
    return datetime.fromisoformat(timestamp).astimezone(TZ).date().isoformat() if timestamp else None


def _refs(values):
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list) or any(not isinstance(x, str) or not x.strip() for x in values):
        raise ValueError("source_refs 必须是非空来源定位字符串列表")
    return list(dict.fromkeys(values))


def _looks_codex_jsonl(content):
    try:
        first = json.loads(next(line for line in content.splitlines() if line.strip()))
    except (ValueError, StopIteration):
        return False
    return isinstance(first, dict) and first.get("type") in ("session_meta", "response_item", "event_msg", "turn_context") and isinstance(first.get("payload"), dict)


def _attachment_refs(value):
    refs = []
    if isinstance(value, dict):
        value = [value]
    for item in value or []:
        if isinstance(item, str):
            refs.append({"reference": item, "status": "missing"})
        elif isinstance(item, dict):
            pointer = next((item[k] for k in ("asset_pointer", "file_id", "url", "path", "id", "name") if item.get(k)), None)
            if pointer:
                refs.append({"reference": str(pointer), "name": item.get("name"), "status": "missing"})
    return refs


def _parts(value):
    """Return available text plus explicit attachment references, never fetch URLs."""
    texts, attachments = [], []
    if isinstance(value, str):
        return value, attachments
    if isinstance(value, dict):
        if isinstance(value.get("parts"), list):
            return _parts(value["parts"])
        if isinstance(value.get("text"), str):
            texts.append(value["text"])
        for key in ("asset_pointer", "image_url", "file_id", "url", "path"):
            if key in value:
                pointer = value[key]
                if isinstance(pointer, dict):
                    pointer = pointer.get("url") or _json(pointer)
                attachments.append({"reference": str(pointer), "status": "missing", "type": value.get("type") or value.get("content_type")})
        if not texts and not attachments and value:
            texts.append(_json(value))
    elif isinstance(value, list):
        for part in value:
            text, assets = _parts(part)
            if text:
                texts.append(text)
            attachments.extend(assets)
    elif value is not None:
        texts.append(str(value))
    return "\n".join(texts), attachments


class RecordsStore:
    FORMAT_VERSION = 1

    def __init__(self, db_path, clock=None):
        self.db_path = str(db_path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS records_import_batches (
                batch_id TEXT PRIMARY KEY, channel TEXT NOT NULL, filename TEXT NOT NULL,
                file_fingerprint TEXT NOT NULL, kind TEXT NOT NULL, scope TEXT NOT NULL,
                imported_at TEXT NOT NULL, status TEXT NOT NULL, report TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS records_import_messages (
                message_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE,
                source_key TEXT NOT NULL, channel TEXT NOT NULL, conversation_id TEXT NOT NULL,
                source_id TEXT NOT NULL, title TEXT NOT NULL, role TEXT NOT NULL,
                body TEXT NOT NULL, original_at TEXT, original_date TEXT, imported_at TEXT NOT NULL,
                kind TEXT NOT NULL, scope TEXT NOT NULL, attachments TEXT NOT NULL,
                metadata TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
                quarantined INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS records_import_day ON records_import_messages(original_date);
            CREATE TABLE IF NOT EXISTS records_batch_items (
                batch_id TEXT NOT NULL, message_id TEXT NOT NULL, created INTEGER NOT NULL,
                PRIMARY KEY(batch_id,message_id));
            CREATE TABLE IF NOT EXISTS records_tombstones (
                object_id TEXT PRIMARY KEY, fingerprint TEXT, source_key TEXT,
                deleted_at TEXT NOT NULL, scope TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS records_profile (
                item_id TEXT NOT NULL, version INTEGER NOT NULL, content TEXT NOT NULL,
                category TEXT NOT NULL, scope TEXT NOT NULL, source_refs TEXT NOT NULL,
                original_at TEXT, imported_at TEXT NOT NULL, valid_until TEXT,
                confirmed INTEGER NOT NULL, active INTEGER NOT NULL, supersedes INTEGER,
                user_input TEXT, PRIMARY KEY(item_id,version));
            CREATE TABLE IF NOT EXISTS records_growth (
                item_id TEXT NOT NULL, version INTEGER NOT NULL, line TEXT NOT NULL,
                kind TEXT NOT NULL, content TEXT NOT NULL, source_refs TEXT NOT NULL,
                parent_id TEXT, business_date TEXT NOT NULL, created_at TEXT NOT NULL,
                active INTEGER NOT NULL, user_input TEXT, PRIMARY KEY(item_id,version));
            CREATE TABLE IF NOT EXISTS records_activity_refs (
                activity_id TEXT PRIMARY KEY, routine TEXT NOT NULL,
                business_date TEXT NOT NULL, input_version INTEGER NOT NULL,
                correction_version INTEGER NOT NULL, source_refs TEXT NOT NULL,
                lines TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS records_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            """)

    def _now(self):
        value = self.clock()
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise ValueError("clock 必须含时区")
        return value.astimezone(timezone.utc).isoformat()

    @contextmanager
    def _db(self, write=False):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _parse(self, channel, content, filename="", kind="conversation", scope="imported"):
        if channel not in ("kimi", "codex", "chatgpt"):
            raise ValueError("channel 必须为 kimi、codex 或 chatgpt")
        if kind not in ("conversation", "memory", "instructions"):
            raise ValueError("kind 必须为 conversation、memory 或 instructions")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("导入内容为空")
        if len(content.encode()) > 50 * 1024 * 1024:
            raise ValueError("单次文本导入限 50 MiB，请拆分源文件")
        if kind == "instructions" and not scope.startswith("project:"):
            raise ValueError("工作规则须明确 project:项目 的作用域，不能自动成为全局指令")
        file_hash = _digest(content)
        parsed, failures, warnings = [], [], []

        def add(conv, source_id, title, role, body, at=None, attachments=None, metadata=None):
            text, detected = _parts(body)
            assets = list(attachments or []) + detected
            if not text and not assets:
                return
            source_key = _digest([channel, str(conv), str(source_id), kind])
            timestamp = _timestamp(at)
            meta = dict(metadata or {})
            if at and not timestamp:
                meta["unparsed_original_time"] = str(at)
                warnings.append(f"{source_id}: 原始时间没有明确时区或无法解析，未伪造日期")
            parsed.append({"message_id": "imp_" + _digest([source_key, str(role), text])[:32],
                           "fingerprint": _digest([source_key, str(role), text]), "source_key": source_key,
                           "channel": channel, "conversation_id": str(conv), "source_id": str(source_id),
                           "title": str(title or filename), "role": str(role), "body": text,
                           "original_at": timestamp, "original_date": _original_day(timestamp),
                           "kind": kind, "scope": scope, "attachments": assets, "metadata": meta})

        if kind != "conversation":
            add(file_hash, "document", filename or kind, "source", content,
                metadata={"instruction_policy": "source_material_only", "profile_category": "project_rule" if kind == "instructions" else "background", "confirmed": False})
        elif channel == "codex" and (filename.endswith(".jsonl") or _looks_codex_jsonl(content)):
            rows = []
            conv = file_hash
            for number, line in enumerate(content.splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                    if not isinstance(item, dict):
                        raise ValueError("JSONL 行不是对象")
                    rows.append((number, item))
                    if item.get("type") == "session_meta":
                        conv = item.get("payload", {}).get("id") or item.get("payload", {}).get("session_id") or conv
                except (ValueError, TypeError) as exc:
                    failures.append({"location": f"line:{number}", "error": str(exc)})
            response_keys = set()
            for number, item in rows:
                payload = item.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                if item.get("type") == "response_item":
                    typ = payload.get("type")
                    sid = payload.get("id") or f"line:{number}"
                    meta = {"line": number, "parent_turn": payload.get("turn_id"), "native_type": typ}
                    if typ == "message":
                        role = payload.get("role", "unknown")
                        text, assets = _parts(payload.get("content", []))
                        # Internal reasoning is not user-visible conversation history.
                        if payload.get("channel") == "analysis":
                            continue
                        add(conv, sid, filename, role, text, item.get("timestamp"), assets, meta)
                        response_keys.add((role, text))
                    elif typ in ("function_call", "custom_tool_call"):
                        add(conv, sid, filename, "tool_call", payload.get("arguments", payload.get("input", "")), item.get("timestamp"), metadata={**meta, "name": payload.get("name"), "call_id": payload.get("call_id")})
                    elif typ in ("function_call_output", "custom_tool_call_output"):
                        add(conv, sid, filename, "tool", payload.get("output", ""), item.get("timestamp"), metadata={**meta, "call_id": payload.get("call_id")})
            # Older Codex exports sometimes have only event_msg user/agent messages.
            for number, item in rows:
                payload = item.get("payload") or {}
                if item.get("type") != "event_msg" or not isinstance(payload, dict):
                    continue
                role = {"user_message": "user", "agent_message": "assistant"}.get(payload.get("type"))
                text = payload.get("message", "")
                if role and (role, text) not in response_keys:
                    add(conv, f"event:{number}", filename, role, text, item.get("timestamp"), metadata={"line": number, "native_type": payload.get("type")})
        else:
            try:
                data = json.loads(content)
            except ValueError:
                data = None
            if data is not None:
                if isinstance(data, dict) and "conversations" in data:
                    conversations = data["conversations"]
                elif isinstance(data, list):
                    # Kimi messages array and exported conversation arrays differ.
                    conversations = [{"messages": data}] if data and isinstance(data[0], dict) and ("role" in data[0] or "content" in data[0]) else data
                elif isinstance(data, dict):
                    conversations = [data]
                else:
                    conversations = []
                if not isinstance(conversations, list):
                    raise ValueError("conversations 必须为数组")
                for ci, conv in enumerate(conversations):
                    if not isinstance(conv, dict):
                        failures.append({"location": f"conversation:{ci}", "error": "会话不是对象"})
                        continue
                    cid = conv.get("id") or conv.get("conversation_id") or conv.get("chat_id") or f"{file_hash}:{ci}"
                    title = conv.get("title") or conv.get("name") or filename
                    mapping = conv.get("mapping")
                    if isinstance(mapping, dict):
                        for nid, node in mapping.items():
                            msg = node.get("message") if isinstance(node, dict) else None
                            if not isinstance(msg, dict):
                                continue
                            if msg.get("channel") == "analysis":
                                continue
                            author = msg.get("author", {})
                            meta = {"parent": node.get("parent"), "children": node.get("children", []), "native_metadata": msg.get("metadata", {})}
                            add(cid, msg.get("id") or nid, title, author.get("role", "unknown") if isinstance(author, dict) else str(author), msg.get("content", ""), msg.get("create_time"), attachments=_attachment_refs((msg.get("metadata") or {}).get("attachments", [])), metadata=meta)
                    else:
                        messages = conv.get("messages") or conv.get("history") or conv.get("items") or []
                        if not isinstance(messages, list):
                            failures.append({"location": f"conversation:{ci}", "error": "messages/history 必须为数组"})
                            continue
                        for mi, msg in enumerate(messages):
                            if not isinstance(msg, dict):
                                failures.append({"location": f"{cid}:{mi}", "error": "消息不是对象"})
                                continue
                            role = msg.get("role") or msg.get("sender") or msg.get("author") or "unknown"
                            if isinstance(role, dict):
                                role = role.get("role", "unknown")
                            add(cid, msg.get("id") or msg.get("message_id") or f"message:{mi}", title, role,
                                msg.get("content", msg.get("text", msg.get("message", ""))),
                                msg.get("created_at", msg.get("create_time", msg.get("timestamp"))),
                                attachments=_attachment_refs(msg.get("attachments", msg.get("files", []))),
                                metadata={"position": mi})
            elif channel == "kimi" or filename.lower().endswith((".md", ".txt")):
                # Explicitly supported Markdown form; unknown formats stay as one
                # readable source document, never pretend to have inferred roles.
                sections = re.split(r"(?m)^#{1,6}\s*(user|assistant|human|用户|助手|Kimi|ChatGPT)\s*[:：]?\s*\n", content, flags=re.I)
                if len(sections) > 1:
                    aliases = {"human": "user", "用户": "user", "助手": "assistant", "kimi": "assistant", "chatgpt": "assistant"}
                    if sections[0].strip():
                        add(file_hash, "preamble", filename, "source", sections[0])
                    for si in range(1, len(sections), 2):
                        role = aliases.get(sections[si].lower(), sections[si].lower())
                        add(file_hash, f"section:{si//2}", filename, role, sections[si + 1])
                else:
                    add(file_hash, "document", filename, "source", content)
                    warnings.append("未识别逐消息结构；已保留为完整来源文档，未推断角色或原始日期")
            else:
                failures.append({"location": "document", "error": "无法识别会话格式；请提供 JSON/JSONL 或 Markdown 导出"})
        if not parsed and not failures:
            failures.append({"location": "document", "error": "未发现可导入消息"})
        # Exact repeat of a native event inside one file is only imported once.
        unique = {item["fingerprint"]: item for item in parsed}
        return list(unique.values()), failures, warnings, file_hash

    @staticmethod
    def _selection(parsed, selected_message_ids):
        if selected_message_ids is None:
            return parsed
        if not isinstance(selected_message_ids, list) or not selected_message_ids or any(not isinstance(x,str) for x in selected_message_ids):
            raise ValueError('请至少选择一条待导入材料')
        selected = set(selected_message_ids)
        if selected - {x['message_id'] for x in parsed}:
            raise ValueError('选段已变化或不存在，请重新预览')
        return [x for x in parsed if x['message_id'] in selected]

    def import_preview(self, channel, content, filename="", kind="conversation", scope="imported", selected_message_ids=None):
        parsed, failures, warnings, file_hash = self._parse(channel, content, filename, kind, scope)
        items = [{k:x[k] for k in ('message_id','conversation_id','title','role','body','original_at')} for x in parsed]
        parsed = self._selection(parsed, selected_message_ids)
        counts = {"total": len(parsed), "new": 0, "duplicates": 0, "deleted": 0}
        with self._db() as db:
            for item in parsed:
                if self._is_deleted(db, item):
                    counts["deleted"] += 1
                elif db.execute("SELECT 1 FROM records_import_messages WHERE fingerprint=?", (item["fingerprint"],)).fetchone():
                    counts["duplicates"] += 1
                else:
                    counts["new"] += 1
        dates = sorted(x["original_at"] for x in parsed if x["original_at"])
        return {"channel": channel, "filename": filename, "kind": kind, "scope": scope,
                "file_fingerprint": file_hash, "counts": counts,
                "date_range": [dates[0], dates[-1]] if dates else [None, None],
                "failures": failures, "warnings": warnings,
                "items": items, "selected_message_ids": [x['message_id'] for x in parsed],
                "missing_attachments": [{"message_id": x["message_id"], **a} for x in parsed for a in x["attachments"] if a.get("status") == "missing"],
                "sample": [{k: x[k] for k in ("message_id", "role", "body", "original_at", "scope")} for x in parsed[:5]],
                "preview_token": _digest([channel, file_hash, filename, kind, scope] + ([sorted(set(selected_message_ids))] if selected_message_ids is not None else []))}

    @staticmethod
    def _is_deleted(db, item):
        return db.execute("SELECT 1 FROM records_tombstones WHERE object_id=? OR fingerprint=? OR source_key=?", (item["message_id"], item["fingerprint"], item["source_key"])).fetchone() is not None

    def import_apply(self, channel, content, filename="", kind="conversation", scope="imported", preview_token=None, selected_message_ids=None):
        report = self.import_preview(channel, content, filename, kind, scope, selected_message_ids)
        if preview_token is not None and preview_token != report["preview_token"]:
            raise ValueError("预览后输入改变，请重新预览")
        parsed, _, _, _ = self._parse(channel, content, filename, kind, scope)
        parsed = self._selection(parsed, selected_message_ids)
        now, batch = self._now(), "batch_" + uuid4().hex
        with self._db(write=True) as db:
            # Count inside the transaction so concurrent imports report truthfully.
            counts = {"total": len(parsed), "new": 0, "duplicates": 0, "deleted": 0}
            db.execute("INSERT INTO records_import_batches VALUES (?,?,?,?,?,?,?,?,?)", (batch, channel, filename, report["file_fingerprint"], kind, scope, now, "active", "{}"))
            for item in parsed:
                if self._is_deleted(db, item):
                    counts["deleted"] += 1
                    continue
                existing = db.execute("SELECT message_id FROM records_import_messages WHERE fingerprint=?", (item["fingerprint"],)).fetchone()
                created = not bool(existing)
                if created:
                    values = [item[k] for k in ("message_id", "fingerprint", "source_key", "channel", "conversation_id", "source_id", "title", "role", "body", "original_at", "original_date")]
                    db.execute("INSERT INTO records_import_messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0)", values + [now, kind, scope, _json(item["attachments"]), _json(item["metadata"])])
                db.execute("INSERT OR IGNORE INTO records_batch_items VALUES (?,?,?)", (batch, item["message_id"], int(created)))
                counts["new" if created else "duplicates"] += 1
            report["counts"] = counts
            db.execute("UPDATE records_import_batches SET report=? WHERE batch_id=?", (_json({k: v for k, v in report.items() if k not in {"sample","items"}}), batch))
            self._scrub_batch_reports(db)
        return {**report, "batch_id": batch, "imported_at": now}

    def list_batches(self):
        with self._db() as db:
            result = []
            for row in db.execute("SELECT * FROM records_import_batches ORDER BY imported_at DESC"):
                value = dict(row)
                value["report"] = json.loads(value["report"])
                result.append(value)
            return result

    def imported_messages(self, query="", day=None, limit=None, include_quarantined=False, offset=0):
        where, params = ["deleted=0"], []
        if not include_quarantined:
            where.append("quarantined=0")
        if query:
            where.append("(instr(lower(body),lower(?))>0 OR instr(lower(title),lower(?))>0)")
            params.extend([query, query])
        if day and day != "unknown":
            date.fromisoformat(day)
        pagination = ""
        if limit is not None and not day:
            params.extend([min(max(int(limit), 1), 10000), max(int(offset), 0)])
            pagination = " LIMIT ? OFFSET ?"
        elif offset and not day:
            params.append(max(int(offset), 0))
            pagination = " LIMIT -1 OFFSET ?"
        with self._db() as db:
            rows = db.execute("SELECT * FROM records_import_messages WHERE " + " AND ".join(where) + " ORDER BY COALESCE(original_at,imported_at),message_id" + pagination, params).fetchall()
            items = [self._decode_import(row) for row in rows]
        annotations = self.date_annotations()
        for item in items:
            evidence = annotations.get(item['message_id'])
            item['date_evidence'] = evidence
            item['display_date'] = item['original_date'] or (evidence or {}).get('value')
            if item['display_date'] and not re.fullmatch(r'\d{4}-\d{2}-\d{2}',item['display_date']):item['display_date']=None
        if day:
            items = [x for x in items if (not x['display_date'] if day=='unknown' else x['display_date']==day)]
            items = items[max(int(offset),0):]
            if limit is not None:items=items[:min(max(int(limit),1),10000)]
        return items

    def date_annotations(self):
        """Derived date labels never overwrite immutable message timestamps."""
        if self.is_quarantined():return {}
        result={}
        for profile in self.list_profile(scope='date_annotation'):
            try:payload=json.loads(profile['content'])
            except (ValueError,TypeError):continue
            for ident in payload.get('message_ids',[]):result[ident]=payload.get('date',{})
        return result

    def historical_records(self, activity_id):
        if self.is_quarantined():return []
        result=[]
        for profile in self.list_profile(scope='activity:'+activity_id):
            try:payload=json.loads(profile['content'])
            except (ValueError,TypeError):continue
            result.append({**payload,'id':profile['item_id'],'source_refs':profile['source_refs'],
                           'recorded_at':profile['imported_at'],'version':profile['version']})
        return result

    def is_quarantined(self):
        with self._db() as db:
            return bool(db.execute("SELECT 1 FROM records_meta WHERE key='quarantine' AND value='1'").fetchone())

    @staticmethod
    def _decode_import(row):
        item = dict(row)
        for key in ("attachments", "metadata"):
            item[key] = json.loads(item[key])
        return item

    def _delete_message(self, db, item, now):
        if item["kind"] == "conversation" and item["role"] == "user":
            # Imported transcripts do not supply reliable per-claim provenance.
            # Invalidate model-derived turns within this source conversation,
            # while retaining every other independent user statement.
            peers = db.execute("SELECT * FROM records_import_messages WHERE channel=? AND conversation_id=? AND kind='conversation' AND role IN ('assistant','tool') AND deleted=0", (item["channel"], item["conversation_id"])).fetchall()
            for peer in peers:
                self._delete_message(db, peer, now)
        db.execute("INSERT OR REPLACE INTO records_tombstones VALUES (?,?,?,?,?)", (item["message_id"], item["fingerprint"], item["source_key"], now, item["scope"]))
        db.execute("UPDATE records_import_messages SET body='',title='',attachments='[]',metadata='{}',deleted=1 WHERE message_id=?", (item["message_id"],))
        self._remove_derived(db, item["message_id"], now)
        self._prune_activity_refs(db, item["message_id"], now)
        self._scrub_batch_reports(db)

    def _scrub_batch_reports(self, db):
        """Attachment diagnostics are derived data and follow source deletion."""
        deleted = {row[0] for row in db.execute("SELECT object_id FROM records_tombstones")}
        for row in db.execute("SELECT batch_id,report FROM records_import_batches").fetchall():
            report = json.loads(row["report"])
            clean = dict(report)
            clean.pop("sample", None)
            if "missing_attachments" in clean:
                clean["missing_attachments"] = [item for item in clean["missing_attachments"] if item.get("message_id") not in deleted]
            if clean != report:
                db.execute("UPDATE records_import_batches SET report=? WHERE batch_id=?", (_json(clean), row["batch_id"]))

    def _remove_derived(self, db, source_id, now):
        aliases = {source_id} | {prefix + ":" + source_id for prefix in ("import", "message", "profile", "growth", "activity", "task", "outcome")}
        for table in ("records_profile", "records_growth"):
            ids = {row["item_id"] for row in db.execute(f"SELECT item_id,source_refs FROM {table}") if aliases.intersection(json.loads(row["source_refs"]))}
            if table == "records_growth":
                ids.update(row["item_id"] for row in db.execute("SELECT item_id,parent_id FROM records_growth") if row["parent_id"] == source_id)
            for item_id in ids:
                db.execute("INSERT OR REPLACE INTO records_tombstones VALUES (?,NULL,NULL,?,?)", (item_id, now, "derived"))
                # Scrub every old revision too; a historical version is not an
                # exception to deletion of the underlying source.
                db.execute(f"DELETE FROM {table} WHERE item_id=?", (item_id,))
                self._remove_derived(db, item_id, now)

    def delete_imported(self, message_id):
        with self._db(write=True) as db:
            item = db.execute("SELECT * FROM records_import_messages WHERE message_id=?", (message_id,)).fetchone()
            if not item:
                raise KeyError(message_id)
            self._delete_message(db, item, self._now())
        return {"message_id": message_id, "deleted": True, "scope": "本实例所选导入原文、同来源会话的助手/工具派生输出、派生画像/成长及后续导出；其他用户原话保留。旧加密备份按保留期退出，外部源文件不受本接口控制"}

    def remove_batch(self, batch_id, *, connection=None):
        if connection is None:
            with self._db(write=True) as db:
                return self.remove_batch(batch_id, connection=db)
        db = connection
        batch = db.execute("SELECT status FROM records_import_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if not batch:
            raise KeyError(batch_id)
        count = 0
        if batch["status"] != "removed":
            items = db.execute("SELECT m.* FROM records_import_messages m JOIN records_batch_items b ON m.message_id=b.message_id WHERE b.batch_id=? AND b.created=1 AND m.deleted=0", (batch_id,)).fetchall()
            for item in items:
                self._delete_message(db, item, self._now())
                count += 1
            db.execute("UPDATE records_import_batches SET status='removed' WHERE batch_id=?", (batch_id,))
        return {"batch_id": batch_id, "removed_new_messages": count, "previous_duplicates_preserved": True}

    def _assert_refs(self, db, refs):
        for ref in refs:
            ident = re.sub(r"^(import|message|profile|growth|activity|task|outcome):", "", ref)
            if db.execute("SELECT 1 FROM records_tombstones WHERE object_id=?", (ident,)).fetchone():
                raise ValueError("来源已删除，不能重新用于画像或成长证据")

    def add_profile(self, content, category="background", scope="global", source_refs=None, confirmed=False,
                    original_at=None, valid_until=None, user_input=None, item_id=None):
        if not isinstance(content, str) or not content.strip():
            raise ValueError("画像内容不能为空")
        if category not in PROFILE_CATEGORIES:
            raise ValueError("无效画像类别")
        if category == "project_rule" and not scope.startswith("project:"):
            raise ValueError("项目规则必须限定 project: 作用域")
        if confirmed and not user_input:
            raise ValueError("确认画像需保留用户原始输入")
        if category == "hypothesis" and confirmed:
            raise ValueError("推测需经用户修订为相应事实类别后确认")
        if valid_until:
            date.fromisoformat(valid_until[:10])
        refs = _refs(source_refs)
        item_id = item_id or "profile_" + uuid4().hex
        with self._db(write=True) as db:
            self._assert_refs(db, refs + [item_id])
            if db.execute("SELECT 1 FROM records_profile WHERE item_id=?", (item_id,)).fetchone():
                raise ValueError("画像已存在，请使用 revise_profile")
            db.execute("INSERT INTO records_profile VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (item_id, 1, content, category, scope, _json(refs), (original_at if isinstance(original_at, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", original_at) else _timestamp(original_at)), self._now(), valid_until, int(confirmed), 1, None, user_input))
        return self.get_profile(item_id)

    def get_profile(self, item_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM records_profile WHERE item_id=? ORDER BY version DESC LIMIT 1", (item_id,)).fetchone()
            if not row:
                raise KeyError(item_id)
            return self._decode_profile(row)

    @staticmethod
    def _decode_profile(row):
        value = dict(row)
        value["source_refs"] = json.loads(value["source_refs"])
        value["active"] = bool(value["active"])
        value["confirmed"] = bool(value["confirmed"])
        try:provenance=json.loads(value.get('user_input') or '{}')
        except (ValueError,TypeError):provenance={}
        value['evidence_status']='source_attested' if (isinstance(provenance,dict) and provenance.get('actor')=='qiju_agent' and provenance.get('method')=='material_compilation' and provenance.get('context_eligible') is True) else None
        return value

    def revise_profile(self, item_id, expected_version, user_input, **changes):
        if not user_input:
            raise ValueError("修订须保留用户原始输入")
        allowed = {"content", "category", "scope", "source_refs", "valid_until", "confirmed", "active"}
        if set(changes) - allowed:
            raise ValueError("不支持的画像修订字段")
        with self._db(write=True) as db:
            row = db.execute("SELECT * FROM records_profile WHERE item_id=? ORDER BY version DESC LIMIT 1", (item_id,)).fetchone()
            if not row:
                raise KeyError(item_id)
            item = self._decode_profile(row)
            if item["version"] != int(expected_version):
                raise ValueError("版本冲突，请刷新后修订")
            item.update(changes)
            item["source_refs"] = _refs(item["source_refs"])
            self._assert_refs(db, item["source_refs"])
            if item["category"] not in PROFILE_CATEGORIES or not str(item["content"]).strip():
                raise ValueError("无效画像内容或类别")
            if item["category"] == "project_rule" and not item["scope"].startswith("project:"):
                raise ValueError("项目规则必须限定 project: 作用域")
            if item["category"] == "hypothesis" and item["confirmed"]:
                raise ValueError("推测不可直接确认为事实")
            if item["valid_until"]:
                date.fromisoformat(item["valid_until"][:10])
            db.execute("INSERT INTO records_profile VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (item_id, expected_version + 1, item["content"], item["category"], item["scope"], _json(item["source_refs"]), item["original_at"], self._now(), item["valid_until"], int(item["confirmed"]), int(item["active"]), expected_version, user_input))
        return self.get_profile(item_id)

    def disable_profile(self, item_id, expected_version, user_input):
        return self.revise_profile(item_id, expected_version, user_input, active=False)

    def list_profile(self, scope=None, include_inactive=False, history=False):
        with self._db() as db:
            sql = "SELECT p.* FROM records_profile p"
            if not history:
                sql += " WHERE p.version=(SELECT MAX(version) FROM records_profile p2 WHERE p2.item_id=p.item_id)"
            rows = [self._decode_profile(r) for r in db.execute(sql + " ORDER BY imported_at,item_id,version")]
        return [r for r in rows if (include_inactive or r["active"]) and (scope is None or r["scope"] == scope)]

    def context(self, scope="global"):
        """Confirmed facts or explicit source-attested preferences, in scope."""
        today = _original_day(self._now())
        with self._db() as db:
            if db.execute("SELECT value FROM records_meta WHERE key='quarantine' AND value='1'").fetchone():
                return []
        return [p for p in self.list_profile() if (p["confirmed"] or p.get('evidence_status')=='source_attested') and p["category"] != "hypothesis" and p["scope"] in ("global", scope) and (not p["valid_until"] or p["valid_until"][:10] >= today)]

    def put_growth(self, line, kind, content, source_refs=None, parent_id=None, item_id=None,
                   expected_version=None, user_input=None, business_date=None, active=True):
        if line not in LINES or kind not in GROWTH_KINDS:
            raise ValueError("未知成长主线或记录类型")
        if isinstance(content, str):
            content = {"text": content}
        if not isinstance(content, dict) or not content:
            raise ValueError("成长记录内容须为非空对象或文字")
        if kind in ("evaluation", "current_state") and not user_input:
            raise ValueError("用户评价或当前状态核对须保留用户原始输入")
        if kind == "plan":
            content = dict(content)
            content.setdefault("horizon_days", 90)
            if not isinstance(content["horizon_days"], int) or not 1 <= content["horizon_days"] <= 366:
                raise ValueError("阶段计划范围须为 1–366 天")
        refs, now = _refs(source_refs), self._now()
        day = business_date or _original_day(now)
        if day=='unknown':
            if content.get('origin') not in ('imported_user_excerpt','source_record_transcription') or not content.get('date_evidence'):
                raise ValueError('未知业务日期须带历史材料日期证据')
        else:date.fromisoformat(day)
        item_id = item_id or "growth_" + uuid4().hex
        with self._db(write=True) as db:
            self._assert_refs(db, refs + [item_id])
            if parent_id:
                parent = db.execute("SELECT line FROM records_growth WHERE item_id=? ORDER BY version DESC LIMIT 1", (parent_id,)).fetchone()
                if not parent or parent["line"] != line:
                    raise ValueError("父项必须是同一主线的已有目标/记录")
                if parent_id == item_id:
                    raise ValueError("成长记录不能以自身为父项")
            current = db.execute("SELECT * FROM records_growth WHERE item_id=? ORDER BY version DESC LIMIT 1", (item_id,)).fetchone()
            version = 1
            if current:
                if expected_version is None or int(expected_version) != current["version"]:
                    raise ValueError("版本冲突，请刷新后修订")
                if current["line"] != line or current["kind"] != kind:
                    raise ValueError("修订不能改变记录身份/主线/类型")
                if not user_input:
                    raise ValueError("计划或证据修订须保留用户输入")
                version += current["version"]
            elif expected_version not in (None, 0):
                raise ValueError("记录不存在，不能从未知版本修订")
            db.execute("INSERT INTO records_growth VALUES (?,?,?,?,?,?,?,?,?,?,?)", (item_id, version, line, kind, _json(content), _json(refs), parent_id, day, now, int(active), user_input))
        return self.get_growth(item_id)

    @staticmethod
    def _decode_growth(row):
        item = dict(row)
        item["content"] = json.loads(item["content"])
        item["source_refs"] = json.loads(item["source_refs"])
        item["active"] = bool(item["active"])
        return item

    def get_growth(self, item_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM records_growth WHERE item_id=? ORDER BY version DESC LIMIT 1", (item_id,)).fetchone()
            if not row:
                raise KeyError(item_id)
            return self._decode_growth(row)

    def growth(self, line=None, include_history=False, include_inactive=False):
        if line is not None and line not in LINES:
            raise ValueError("未知成长主线")
        with self._db() as db:
            rows = [self._decode_growth(r) for r in db.execute("SELECT * FROM records_growth ORDER BY created_at,item_id,version")]
        if line:
            rows = [r for r in rows if r["line"] == line]
        latest = {}
        for row in rows:
            latest[row["item_id"]] = row
        items = [r for r in latest.values() if include_inactive or r["active"]]
        kinds = {r["kind"] for r in items}
        return {"line": line, "items": items, "versions": rows if include_history else [],
                "missing_steps": [k for k in GROWTH_KINDS if k != "current_state" and k not in kinds],
                "current_state": next((r for r in reversed(items) if r["kind"] == "current_state"), None),
                "notice": "缺少记录不代表未投入；功能数据不构成用户真实闭环验收"}

    def weekly_review(self, day=None):
        current = date.fromisoformat(_original_day(self._now()))
        chosen = date.fromisoformat(day) if day else current
        start = chosen - timedelta(days=chosen.weekday())
        end = start + timedelta(days=6)
        this_week = current - timedelta(days=current.weekday())
        label = "本周截至目前" if start == this_week else ("上一完整周" if start == this_week - timedelta(days=7) else "所选自然周")
        grouped = {}
        for line in LINES:
            grouped[line] = [x for x in self.growth(line)["items"] if start.isoformat() <= x["business_date"] <= min(end, current).isoformat()]
        evidence = [x for x in self.activity_evidence() if start.isoformat() <= x["business_date"] <= min(end, current).isoformat()]
        return {"start": start.isoformat(), "end": end.isoformat(), "timezone": "Asia/Shanghai", "label": label,
                "as_of": self._now(), "lines": grouped, "activity_evidence": evidence, "automatic_judgment": None}

    def _dependent_refs(self, db, source_refs):
        normalize = lambda ref: re.sub(r"^(import|message|profile|growth|activity|task|outcome):", "", ref)
        refs = _refs(source_refs)
        found, pending, visited = set(refs), [normalize(ref) for ref in refs], set()
        edges, own = {}, {}
        for table, kind in (("records_profile", "profile"), ("records_growth", "growth")):
            # All revisions matter: a newer revision dropping a source pointer
            # cannot exempt the old version or its descendants from deletion.
            for row in db.execute(f"SELECT * FROM {table}"):
                alias = kind + ":" + row["item_id"]
                own.setdefault(row["item_id"], set()).add(alias)
                sources = json.loads(row["source_refs"])
                if kind == "growth" and row["parent_id"]:
                    sources.append(row["parent_id"])
                for source in sources:
                    edges.setdefault(normalize(source), set()).add((row["item_id"], alias))
        imports = db.execute("SELECT message_id,channel,conversation_id,kind,role FROM records_import_messages").fetchall()
        user_conversations, conversation_derived, expanded_conversations = {}, {}, set()
        for row in imports:
            own.setdefault(row["message_id"], set()).add("import:" + row["message_id"])
            conversation = (row["channel"], row["conversation_id"])
            if row["kind"] == "conversation" and row["role"] == "user":
                user_conversations[row["message_id"]] = conversation
            elif row["kind"] == "conversation" and row["role"] in ("assistant", "tool"):
                conversation_derived.setdefault(conversation, set()).add(row["message_id"])
        while pending:
            ident = pending.pop()
            if ident in visited:
                continue
            visited.add(ident)
            found.update(own.get(ident, ()))
            conversation = user_conversations.get(ident)
            if conversation is not None and conversation not in expanded_conversations:
                expanded_conversations.add(conversation)
                for child in conversation_derived.get(conversation, ()):
                    found.add("import:" + child)
                    pending.append(child)
            for child, alias in edges.get(ident, ()):
                found.add(alias)
                pending.append(child)
        return sorted(found)

    def dependent_refs(self, source_refs):
        """Read-only transitive source aliases, including the supplied roots."""
        with self._db() as db:
            return self._dependent_refs(db, source_refs)

    def delete_source(self, source_ref, *, preserve_source=False, connection=None):
        """Delete a local source and its descendants, or mark an external source."""
        ident = re.sub(r"^(import|message|profile|growth|activity|task|outcome):", "", source_ref)
        if not ident:
            raise ValueError("需要来源 ID")
        if preserve_source and not source_ref.startswith(("activity:", "task:")):
            raise ValueError("仅持续存在的活动/任务可保留来源身份")
        if connection is None:
            with self._db(write=True) as db:
                return self.delete_source(source_ref, preserve_source=preserve_source, connection=db)
        db = connection
        now = self._now()
        affected = self._dependent_refs(db, [source_ref])
        # Preserve an existing import fingerprint so calling the general
        # deletion API cannot weaken its repeat-import protection.
        if preserve_source:
            db.execute("DELETE FROM records_tombstones WHERE object_id=? AND scope='source_reference'", (ident,))
        else:
            db.execute("INSERT INTO records_tombstones VALUES (?,NULL,NULL,?,?) ON CONFLICT(object_id) DO UPDATE SET deleted_at=excluded.deleted_at", (ident, now, "source_reference"))
        imported = db.execute("SELECT * FROM records_import_messages WHERE message_id=?", (ident,)).fetchone()
        if imported:
            self._delete_message(db, imported, now)
        for table in ("records_profile", "records_growth"):
            db.execute(f"DELETE FROM {table} WHERE item_id=?", (ident,))
        self._remove_derived(db, ident, now)
        self._prune_activity_refs(db, ident, now)
        return {"source_ref": source_ref, "derived_deleted": True, "affected_refs": affected, "source_preserved": preserve_source}

    def _prune_activity_refs(self, db, ident, now):
        db.execute("DELETE FROM records_activity_refs WHERE activity_id=?", (ident,))
        for row in db.execute("SELECT activity_id,source_refs FROM records_activity_refs").fetchall():
            refs = json.loads(row["source_refs"])
            filtered = [x for x in refs if re.sub(r"^(message|import):", "", x) != ident]
            if filtered != refs:
                db.execute("UPDATE records_activity_refs SET source_refs=?,updated_at=? WHERE activity_id=?", (_json(filtered), now, row["activity_id"]))

    def sync_activity(self, activity_id, routine, business_date, source_refs, input_version,
                      correction_version=0, lines=None):
        """Maintain a live evidence pointer; the live summary remains authoritative.

        No capability, trait, or success judgment is inferred. Empty lines means
        unassigned evidence, not evidence that all three lines made progress.
        """
        if routine not in ("A2", "A3"):
            raise ValueError("成长活动证据只接收 A2/A3；A1 保留学习历史")
        date.fromisoformat(business_date)
        refs = _refs(source_refs)
        lines = list(dict.fromkeys(lines or []))
        if any(x not in LINES for x in lines):
            raise ValueError("未知成长主线")
        if type(input_version) is not int or type(correction_version) is not int or min(input_version, correction_version) < 0:
            raise ValueError("版本应为非负整数")
        with self._db(write=True) as db:
            self._assert_refs(db, refs + [activity_id])
            old = db.execute("SELECT * FROM records_activity_refs WHERE activity_id=?", (activity_id,)).fetchone()
            if old:
                if old["business_date"] != business_date or old["routine"] != routine:
                    raise ValueError("原活动日期与类型不可改变")
                if old["input_version"] > input_version or old["correction_version"] > correction_version:
                    return False
            db.execute("INSERT OR REPLACE INTO records_activity_refs VALUES (?,?,?,?,?,?,?,?)", (activity_id, routine, business_date, input_version, correction_version, _json(refs), _json(lines), self._now()))
        return True

    def activity_evidence(self):
        with self._db() as db:
            values = []
            for row in db.execute("SELECT * FROM records_activity_refs ORDER BY business_date,activity_id"):
                item = dict(row)
                item["source_refs"] = json.loads(item["source_refs"])
                item["lines"] = json.loads(item["lines"])
                item["summary_source"] = "activity:" + item["activity_id"]
                values.append(item)
            return values

    def export_ledger(self):
        with self._db() as db:
            tombstones = [dict(r) for r in db.execute("SELECT * FROM records_tombstones ORDER BY deleted_at,object_id")]
        return {"format": "dsh-personal-records-ledger", "version": self.FORMAT_VERSION, "as_of": self._now(),
                "tombstones": tombstones,
                "profiles": self.list_profile(include_inactive=True),
                "growth": self.growth(include_history=False, include_inactive=True)["items"],
                "activity_refs": self.activity_evidence()}

    def _apply_ledger(self, db, ledger):
        if ledger.get("format") != "dsh-personal-records-ledger" or ledger.get("version") != self.FORMAT_VERSION:
            raise ValueError("不支持的修订/删除账本格式")
        for tomb in ledger.get("tombstones", []):
            db.execute("INSERT OR REPLACE INTO records_tombstones VALUES (?,?,?,?,?)", tuple(tomb.get(k) for k in ("object_id", "fingerprint", "source_key", "deleted_at", "scope")))
            rows = db.execute("SELECT * FROM records_import_messages WHERE message_id=? OR fingerprint=? OR source_key=?", (tomb["object_id"], tomb.get("fingerprint"), tomb.get("source_key"))).fetchall()
            for row in rows:
                self._delete_message(db, row, tomb["deleted_at"])
            for table in ("records_profile", "records_growth"):
                db.execute(f"DELETE FROM {table} WHERE item_id=?", (tomb["object_id"],))
            self._remove_derived(db, tomb["object_id"], tomb["deleted_at"])
            self._prune_activity_refs(db, tomb["object_id"], tomb["deleted_at"])
        for profile in ledger.get("profiles", []):
            self._restore_profile(db, profile)
        for item in ledger.get("growth", []):
            self._restore_growth(db, item)
        for item in ledger.get("activity_refs", []):
            self._restore_activity_ref(db, item)
        db.execute("INSERT OR REPLACE INTO records_meta VALUES ('ledger_as_of',?)", (ledger["as_of"],))

    def apply_ledger(self, ledger, release_quarantine=False, user_input=None):
        if release_quarantine and not user_input:
            raise ValueError("解除恢复隔离须由用户核对账本覆盖范围并保留输入")
        with self._db(write=True) as db:
            self._apply_ledger(db, ledger)
            if release_quarantine:
                db.execute("UPDATE records_import_messages SET quarantined=0 WHERE deleted=0")
                db.execute("INSERT OR REPLACE INTO records_meta VALUES ('quarantine','0')")
                db.execute("INSERT OR REPLACE INTO records_meta VALUES ('recovery_confirmation',?)", (_json({"at": self._now(), "input": user_input, "ledger_as_of": ledger["as_of"]}),))
        return {"applied": True, "ledger_as_of": ledger["as_of"], "released": release_quarantine}

    def _restore_profile(self, db, item):
        try:
            self._assert_refs(db, item["source_refs"] + [item["item_id"]])
        except ValueError:
            return
        row = db.execute("SELECT version FROM records_profile WHERE item_id=? AND version=?", (item["item_id"], item["version"])).fetchone()
        if row is not None:
            return
        db.execute("INSERT INTO records_profile VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (item["item_id"], item["version"], item["content"], item["category"], item["scope"], _json(item["source_refs"]), item.get("original_at"), item["imported_at"], item.get("valid_until"), int(item["confirmed"]), int(item["active"]), item.get("supersedes"), item.get("user_input")))

    def _restore_growth(self, db, item):
        try:
            self._assert_refs(db, item["source_refs"] + [item["item_id"]] + ([item["parent_id"]] if item.get("parent_id") else []))
        except ValueError:
            return
        row = db.execute("SELECT version FROM records_growth WHERE item_id=? AND version=?", (item["item_id"], item["version"])).fetchone()
        if row is not None:
            return
        db.execute("INSERT INTO records_growth VALUES (?,?,?,?,?,?,?,?,?,?,?)", (item["item_id"], item["version"], item["line"], item["kind"], _json(item["content"]), _json(item["source_refs"]), item.get("parent_id"), item["business_date"], item["created_at"], int(item["active"]), item.get("user_input")))

    def _restore_activity_ref(self, db, item):
        try:
            self._assert_refs(db, [item["activity_id"]])
        except ValueError:
            return
        refs = []
        for ref in item["source_refs"]:
            try:
                self._assert_refs(db, [ref])
                refs.append(ref)
            except ValueError:
                pass
        old = db.execute("SELECT input_version,correction_version FROM records_activity_refs WHERE activity_id=?", (item["activity_id"],)).fetchone()
        if old and (old["input_version"] > item["input_version"] or old["correction_version"] > item["correction_version"]):
            return
        db.execute("INSERT OR REPLACE INTO records_activity_refs VALUES (?,?,?,?,?,?,?,?)", (item["activity_id"], item["routine"], item["business_date"], item["input_version"], item["correction_version"], _json(refs), _json(item["lines"]), item["updated_at"]))

    def export_data(self):
        with self._db() as db:
            messages = [self._decode_import(r) for r in db.execute("SELECT * FROM records_import_messages WHERE deleted=0 ORDER BY imported_at,message_id")]
            batches = [dict(r) for r in db.execute("SELECT * FROM records_import_batches")]
            links = [dict(r) for r in db.execute("SELECT * FROM records_batch_items")]
        return {"format": "dsh-personal-records", "version": self.FORMAT_VERSION, "exported_at": self._now(),
                "messages": messages, "batches": batches, "batch_items": links,
                "profiles": self.list_profile(include_inactive=True, history=True),
                "growth": self.growth(include_history=True)["versions"], "activity_refs": self.activity_evidence(), "ledger": self.export_ledger()}

    def import_data(self, data, latest_ledger=None):
        """Restore is quarantined until the latest ledger coverage is verified.

        Supplying a ledger applies known changes but is not in itself permission
        to resume model context or automatic execution.
        """
        if data.get("format") != "dsh-personal-records" or data.get("version") != self.FORMAT_VERSION:
            raise ValueError("不支持的资料导出版本")
        with self._db(write=True) as db:
            self._apply_ledger(db, data["ledger"])
            if latest_ledger:
                self._apply_ledger(db, latest_ledger)
            for item in data.get("messages", []):
                if self._is_deleted(db, item):
                    continue
                db.execute("INSERT OR IGNORE INTO records_import_messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,1)", tuple(item[k] for k in ("message_id", "fingerprint", "source_key", "channel", "conversation_id", "source_id", "title", "role", "body", "original_at", "original_date", "imported_at", "kind", "scope")) + (_json(item["attachments"]), _json(item["metadata"])))
            for batch in data.get("batches", []):
                db.execute("INSERT OR IGNORE INTO records_import_batches VALUES (?,?,?,?,?,?,?,?,?)", tuple(batch[k] for k in ("batch_id", "channel", "filename", "file_fingerprint", "kind", "scope", "imported_at", "status", "report")))
            for link in data.get("batch_items", []):
                db.execute("INSERT OR IGNORE INTO records_batch_items VALUES (?,?,?)", tuple(link[k] for k in ("batch_id", "message_id", "created")))
            for profile in sorted(data.get("profiles", []), key=lambda p: p["version"]):
                self._restore_profile(db, profile)
            for item in sorted(data.get("growth", []), key=lambda p: p["version"]):
                self._restore_growth(db, item)
            for item in data.get("activity_refs", []):
                self._restore_activity_ref(db, item)
            # Old batch diagnostics are inserted after the current tombstones.
            self._scrub_batch_reports(db)
            db.execute("INSERT OR REPLACE INTO records_meta VALUES ('quarantine','1')")
        return {"restored": True, "quarantined": True, "latest_ledger_supplied": latest_ledger is not None,
                "remaining": "核对最新账本的覆盖时间、外部动作回执及附件后，显式解除隔离；不会自动执行或补发通知"}

    def render_markdown(self):
        data = self.export_data()
        lines = ["# DSH 导入资料与成长档案", "", f"导出时间：{data['exported_at']}", ""]
        for msg in data["messages"]:
            lines += [f"## {msg['original_date'] or '原始日期未知'} · {msg['channel']} · {msg['title']}", "", f"- ID：{msg['message_id']}", f"- 来源：{msg['conversation_id']} / {msg['source_id']}", f"- 角色：{msg['role']}；导入时间：{msg['imported_at']}", "", msg["body"], ""]
            if msg["attachments"]:
                lines += ["附件定位：" + _json(msg["attachments"]), ""]
        lines += ["## 当前画像", ""]
        for item in self.list_profile(include_inactive=True):
            lines += [f"### {item['item_id']} v{item['version']} · {item['scope']}", "", item["content"], "", f"确认：{item['confirmed']}；启用：{item['active']}；来源：{', '.join(item['source_refs'])}", ""]
        for line in LINES:
            lines += [f"## 成长主线：{line}", ""]
            for item in self.growth(line, include_history=True)["versions"]:
                lines += [f"### {item['kind']} · {item['item_id']} v{item['version']}", "", json.dumps(item["content"], ensure_ascii=False, indent=2), "", f"来源：{', '.join(item['source_refs'])}", ""]
        return "\n".join(lines)
