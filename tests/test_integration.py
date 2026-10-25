"""Authenticated HTTP and worker contracts using isolated synthetic data.

These tests exercise the actual app routes, not static UI mock responses. The
DSH bridge is deliberately replaced only for model-independent queue tests;
real native recovery and provider calls have separate runtime probes.
"""
import base64
from datetime import datetime, timezone
import http.cookiejar
import io
import json
from pathlib import Path
import tempfile
import sqlite3
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from dsh_personal.server import Application, create_server


class FixtureBridge:
    def __init__(self):
        self.calls = []
    def ensure_session(self, activity_id):
        return "fixture-session-" + activity_id
    def reconcile(self, session_id, submission_id):
        return {"accepted": any(c[1] == submission_id for c in self.calls)}
    def submit(self, session_id, submission_id, text, **kwargs):
        self.calls.append((session_id, submission_id, text, kwargs))
        return {"accepted": True}
    def events(self, session_id):
        return []


class HTTPIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Application(Path(self.temp.name) / "data", clock=lambda: datetime(2026, 10, 22, 15, 40, tzinfo=timezone.utc))
        self.bridge = FixtureBridge()
        self.app.bridge = self.app.worker.bridge = self.bridge
        self.server = create_server(self.app, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)
        self.base = "http://127.0.0.1:" + str(self.server.server_address[1])
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        status, data = self.request("/api/login", {"token": self.app.access_token})
        self.assertEqual(status, 200, data)

    def shutdown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, payload=None, headers=None, opener=None):
        head = {"X-DSH-Client": "web", "Content-Type": "application/json", "Origin": self.base}
        head.update(headers or {})
        req = urllib.request.Request(self.base + path, None if payload is None else json.dumps(payload, ensure_ascii=False).encode(), head)
        try:
            response = (opener or self.opener).open(req, timeout=10)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            raw = response.read()
            data = json.loads(raw) if response.headers.get("Content-Type", "").startswith("application/json") else raw
            return response.status, data

    def action(self, op, **values):
        code, data = self.request("/api/action", {"op": op, **values})
        self.assertEqual(code, 200, data)
        return data

    def activity(self, routine="A3", day="2026-10-21"):
        return self.action("ensure_activity", routine=routine, date=day)["activity"]

    def drain(self, limit=20):
        for _ in range(limit):
            if not self.app.worker.work_one():
                break

    def test_authentication_wrong_origin_and_isolation_enforced(self):
        plain = urllib.request.build_opener()
        code, _ = self.request("/api/state", opener=plain)
        self.assertEqual(code, 401)
        code, _ = self.request("/api/action", {"op": "ensure_activity", "routine": "A3"}, headers={"Origin": "https://another.invalid"})
        self.assertEqual(code, 403)
        activity = self.activity()
        self.action("message", activity_id=activity["id"], submission_id="isolation-sensitive-message", body="恢复核对前不得检索的测试原文")
        self.app.store.set_meta("isolated", "true")
        code, _ = self.request("/api/action", {"op": "ensure_activity", "routine": "A3"})
        self.assertEqual(code, 403)
        code, state = self.request("/api/state?journal_mode=all&q=" + urllib.parse.quote("测试原文"))
        self.assertEqual(code, 200)
        self.assertFalse(state["journal"])
        self.assertFalse(state["profiles"])
        code, _ = self.request("/api/activity?id=" + activity["id"])
        self.assertEqual(code, 403)
        code, _ = self.request("/api/export")
        self.assertEqual(code, 403)

    def test_retry_learning_before_first_user_reply_uses_original_request(self):
        activity=self.activity('A1')
        self.drain()
        self.assertEqual(len(self.bridge.calls),1)
        before_messages=self.app.store.activity(activity['id'])['messages']
        retried=[]
        self.bridge.resume_processing=lambda *args: retried.append(args)
        self.action('retry',activity_id=activity['id'])
        self.drain()
        self.assertEqual(len(retried),1)
        self.assertEqual(retried[0][2],'learning-'+activity['id'])
        self.assertEqual(len(self.bridge.calls),1)
        self.assertEqual(self.app.store.activity(activity['id'])['messages'],before_messages)
        with self.app.store.db(True) as db:
            db.execute("UPDATE activities SET status='awaiting_reply',ready_at=? WHERE id=?",(self.app.store.stamp(),activity['id']))
        self.action('retry',activity_id=activity['id'])
        self.drain()
        self.assertEqual(len(retried),1)

    def test_profile_ui_payload_correction_used_by_later_conversation(self):
        a = self.activity()
        p = self.action("profile_save", content="我偏好简短回复", category="preference", scope="global", source_refs=["activity:" + a["id"]], confirmed=True, valid_until=None, user_input="我偏好简短回复")
        revised = self.action("profile_save", id=p["item_id"], item_id=p["item_id"], expected_version=1, content="需要充分展开论证", category="preference", scope="global", source_refs=p["source_refs"], confirmed=True, user_input="更正：需要充分展开论证")
        self.assertEqual(revised["version"], 2)
        code, _ = self.request("/api/action", {"op": "profile_save", "item_id": p["item_id"], "expected_version": 1, "content": "旧页面值", "confirmed": True, "user_input": "旧页面值"})
        self.assertEqual(code, 400)
        self.action("message", activity_id=a["id"], submission_id="http-profile-message", body="睡得一般，想继续讨论")
        self.drain()
        sent = self.bridge.calls[-1][2]
        self.assertIn("需要充分展开论证", sent)
        self.assertNotIn("我偏好简短回复", sent)
        self.action("profile_disable", item_id=p["item_id"])
        self.assertFalse(self.app.records.context())

    def test_growth_object_business_date_and_version_through_http(self):
        goal = self.action("growth_save", line="doctorate", kind="goal", content={"text": "测试目标"}, business_date="2026-10-14", user_input="测试目标")
        plan = self.action("growth_save", line="doctorate", kind="plan", content={"text": "测试计划", "review_date": "2026-11-01"}, parent_id=goal["item_id"], source_refs=["growth:" + goal["item_id"]], business_date="2026-10-14", user_input="测试计划")
        revised = self.action("growth_save", line="doctorate", kind="plan", item_id=plan["item_id"], expected_version=1, content={"text": "修订测试计划", "review_date": "2026-11-03"}, parent_id=goal["item_id"], business_date="2026-10-14", user_input="修订测试计划")
        self.assertEqual(revised["version"], 2)
        self.assertEqual(revised["content"]["review_date"], "2026-11-03")
        _, state = self.request("/api/state?date=2026-10-14")
        self.assertEqual(state["weekly_review"]["label"], "上一完整周")
        self.assertEqual(len(state["weekly_review"]["lines"]["doctorate"]), 2)
        self.assertFalse(state["tasks"])
        code, _ = self.request("/api/action", {"op": "growth_save", "line": "doctorate", "kind": "plan", "item_id": plan["item_id"], "expected_version": 1, "content": {"text": "过时提交"}, "user_input": "过时提交"})
        self.assertEqual(code, 400)

    def test_import_preview_apply_complete_journal_unknown_and_source_link(self):
        messages = [{"id": str(i), "role": "user", "content": f"批量测试消息 {i}", "created_at": "2026-10-14T12:00:00+08:00"} for i in range(205)]
        params = {"source": "kimi", "content": json.dumps({"id": "http-import", "messages": messages}), "filename": "kimi.json", "kind": "conversation", "scope": "imported"}
        preview = self.action("import_preview", **params)
        self.assertEqual(preview["counts"]["new"], 205)
        self.action("import_apply", **params, preview_token=preview["preview_token"])
        self.action("import_apply", source="kimi", content="# 用户\n无日期材料", filename="undated.md", kind="conversation", scope="imported")
        _, day = self.request("/api/state?date=2026-10-14")
        self.assertEqual(len(day["journal"]), 205)
        _, undated = self.request("/api/state?journal_mode=undated")
        self.assertEqual(len(undated["journal"]), 1)
        _, all_rows = self.request("/api/state?journal_mode=all")
        self.assertEqual(len(all_rows["journal"]), 206)
        row = day["journal"][0]
        code, source = self.request("/api/source?ref=" + urllib.parse.quote("import:" + row["message_id"]))
        self.assertEqual(code, 200, source)
        self.assertEqual(source["body"], row["body"])
        # Tampering after preview must not apply different content.
        bad = dict(params, content=params["content"] + " ")
        code, _ = self.request("/api/action", {"op": "import_apply", **bad, "preview_token": preview["preview_token"]})
        self.assertEqual(code, 400)

    def test_summary_correction_and_backfill_update_original_week_refs(self):
        a = self.activity("A2", "2026-10-14")
        first = self.action("message", activity_id=a["id"], submission_id="http-summary-first", body="博士实验完成了预试，科研创业暂未投入")
        self.drain()
        self.action("correction", activity_id=a["id"], fields={"progress": "更正为完成一次预试"}, source="测试用户更正")
        second = self.action("message", activity_id=a["id"], submission_id="http-summary-second", body="补充：预试发现刺激时序需要核对")
        self.drain()
        _, view = self.request("/api/activity?id=" + a["id"])
        self.assertEqual(view["summary"]["fields"]["progress"], "更正为完成一次预试")
        self.assertEqual(set(view["summary"]["source_ids"]), {first["receipt"], second["receipt"]})
        _, state = self.request("/api/state?date=2026-10-14")
        refs = state["weekly_review"]["activity_evidence"]
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["input_version"], 2)
        self.assertEqual(refs[0]["correction_version"], 1)
        self.assertEqual(refs[0]["business_date"], "2026-10-14")

    def test_delete_source_cleans_profile_and_growth_and_blocks_model_context(self):
        a = self.activity()
        r = self.action("message", activity_id=a["id"], submission_id="http-delete-message", body="睡得一般")
        self.drain()
        p = self.action("profile_save", content="源自本条原话的背景", source_refs=["message:" + r["receipt"]], confirmed=True, user_input="测试确认")
        self.action("growth_save", line="doctorate", kind="capability", content={"text": "待验证判断"}, source_refs=["profile:" + p["item_id"]], user_input="测试记录")
        deleted = self.action("delete_message", message_id=r["receipt"], source="测试用户删除")
        self.assertTrue(deleted["native_cleanup_pending"])
        _, state = self.request("/api/state?journal_mode=all")
        self.assertFalse(state["profiles"])
        self.assertFalse(state["growth"]["items"])
        self.assertFalse(any(m.get("id") == r["receipt"] for m in state["journal"]))
        with self.assertRaises(RuntimeError):
            self.app.worker.ensure_session(a["id"])

    def test_attachment_http_download_export_and_readable_source(self):
        a = self.activity()
        self.action("message", activity_id=a["id"], submission_id="http-attachment-message", body="睡得一般")
        asset = self.action("attachment_upload", activity_id=a["id"], filename="实验说明.txt", content_base64=base64.b64encode(b"fixture attachment").decode())
        code, raw = self.request("/api/attachment?id=" + asset["id"])
        self.assertEqual(code, 200)
        self.assertEqual(raw, b"fixture attachment")
        code, markdown = self.request("/api/export?format=markdown")
        self.assertEqual(code, 200)
        self.assertIn("睡得一般", markdown.decode())
        self.assertIn("实验说明.txt", markdown.decode())
        code, bundle = self.request("/api/export")
        self.assertEqual(code, 200)
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            self.assertEqual(archive.read("attachments/" + asset["id"]), b"fixture attachment")
            self.assertNotIn("access-token", archive.namelist())


class StartupProtectionTests(unittest.TestCase):
    def test_unfinished_upgrade_marker_prevents_database_initialization(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "upgrade-in-progress.json").write_text('{"action":"test interrupted upgrade"}')
            with self.assertRaisesRegex(RuntimeError, "升级切换"):
                Application(root)
            self.assertFalse((root / "personal.sqlite").exists())

    def test_old_schema_refused_without_modifying_source(self):
        from dsh_personal.release import SCHEMA0_DDL
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with sqlite3.connect(root / "personal.sqlite") as db:
                db.executescript(SCHEMA0_DDL)
            before = (root / "personal.sqlite").read_bytes()
            with self.assertRaisesRegex(RuntimeError, "格式不兼容"):
                Application(root)
            self.assertEqual((root / "personal.sqlite").read_bytes(), before)

    def test_current_schema_reopens_in_hash_and_space_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "data #special ?name"
            first = Application(root)
            activity = first.store.ensure_activity("A3")
            second = Application(root)
            self.assertEqual(second.store.activity(activity["id"])["activity"]["id"], activity["id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
