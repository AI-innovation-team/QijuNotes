#!/usr/bin/env python3
"""Bounded real-provider scenarios in a disposable business/native DSH instance.

Without --run, print the plan without reading credentials or contacting services.
Only the project's internal gateway-key is read; provider credentials and real
diary/profile/task records are never read. Production gateway usage is expected.
"""
from __future__ import annotations
import argparse
import hashlib
import http.server
import json
import os
from pathlib import Path
import secrets
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from unittest.mock import patch as patch_object

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dsh_personal.core import dumps
from dsh_personal.gateway import RequestDeadline, UsageReceipt
from dsh_personal.server import Application
from dsh_personal.worker import Worker
from runtime.dsh_bridge import DSHBridge
from runtime.probe import free_port

PROMPTS = {
    "A1": [
        "围绕你刚引用的典籍继续深入：请区分原文、白话解释和你的推论；给出篇名或章次，原文不确定请说明。不要换学习主题。",
        "继续刚才的典籍主题。请指出一种可能的误读，并解释原文语境如何支持或限制你刚才的理解；不要用虚构出处补足空白。",
        "仍沿用刚才的原文和误读。给一个与前两轮一致的具体应用，再说明这个应用不能从原文推出的部分。列出可复核的原始出处。",
    ],
    "A2": [
        "以下是隔离开发验证的虚构人物，不是实际用户经历：博士方向今天只整理了实验排期，未完成实验；科研创业当期没有投入，文化方向今天暂时跳过。现在只想深入博士实验排期约束，不要强制切换维度。",
        "继续深入刚才的博士排期。虚构约束是设备周三可用、我周三上午有组会。为什么这会使原计划冲突？先分析约束，不替我声称实验完成。",
        "继续同一博士问题。若把设备使用移到周三下午，哪些约束解决了，哪些信息仍未知？创业没有投入、文化跳过仍按原话保留，不虚构进展。",
        "定向摘要已经生成，我还要继续同一博士问题。结合前面的周三设备和上午组会，列出可检查的下一步；区分已发生的排期整理和未来实验计划。",
    ],
    "A3": [
        "以下是隔离开发验证的虚构睡眠记录，不是实际用户经历：2026-10-22凌晨00:30睡，同日07:00起，睡眠质量2分，当天还没有运动。我先说这些，还想继续讨论，不需要诊断。",
        "继续刚才的记录。虚构计划是今晚把睡前阅读缩短15分钟；请区分计划和已经发生的运动、睡眠事实，不要把短答当作结束。",
        "继续这个睡眠话题：如果今晚只调整阅读时间，明天看哪项记录才能判断变化？沿用前面给出的时间，不要推断病因。",
        "摘要和人工更正已经保存。请按当前更正复述起床时间与主观质量，再结合今晚缩短阅读的计划继续分析；不要沿用被更正的旧值，也不要声称计划已完成。",
    ],
}


def usage_rows(data):
    """Allowlisted accounting-only read; no production conversation-table reads."""
    uri = "file:" + urllib.parse.quote(str(Path(data) / "personal.sqlite")) + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        db.row_factory = sqlite3.Row
        return {r["id"]: dict(r) for r in db.execute("SELECT u.id,u.kind,u.reserved,u.actual,u.state,d.model,d.output_limit_field,d.output_limit,d.stream,d.started_at,d.finished_at,d.reported_total,d.usage_json,d.response_state,d.usage_state FROM usage u LEFT JOIN usage_details d ON d.reservation_id=u.id")}


class LimitReached(RuntimeError):
    pass


class CallBudget:
    def __init__(self, maximum, per_turn):
        self.maximum, self.per_turn = maximum, per_turn
        self.lock, self.turn, self.records, self.rejections = threading.Lock(), "startup", [], []

    def begin(self, label):
        with self.lock:
            self.turn = label

    def claim(self):
        with self.lock:
            if len(self.records) >= self.maximum or sum(r["turn"] == self.turn for r in self.records) >= self.per_turn:
                self.rejections.append({"turn": self.turn, "reason": "hard_request_limit"})
                raise LimitReached("验证调用达到上限，停止而不自动加预算")
            row = {"ordinal": len(self.records) + 1, "turn": self.turn, "started_at": time.time()}
            self.records.append(row)
            return row


def finish_reasons(parsed):
    return [choice["finish_reason"] for choice in parsed.get("choices", []) if choice.get("finish_reason")] if isinstance(parsed, dict) else []


class GatewayRelay:
    """Counts every native and summary request, including tool continuations."""
    def __init__(self, data, key, budget, max_tokens, timeout, learning_initial_limit=None):
        self.data, self.key, self.budget = Path(data), key, budget
        self.max_tokens, self.timeout, self.local_key = max_tokens, timeout, secrets.token_urlsafe(32)
        self.learning_initial_limit = learning_initial_limit or max_tokens
        self.server, self.url = None, None

    def start(self):
        outer = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                if self.path != "/v1/chat/completions" or self.headers.get("Authorization") != "Bearer " + outer.local_key:
                    self.send_error(403)
                    return
                row, before, started = None, None, False
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 2_000_000:
                        raise ValueError("验证请求大小越界")
                    payload = json.loads(self.rfile.read(size))
                    row = outer.budget.claim()
                    field = "max_completion_tokens" if "max_completion_tokens" in payload else "max_tokens"
                    limit = outer.learning_initial_limit if row["turn"].startswith("A1.initial") else outer.max_tokens
                    payload[field] = min(int(payload.get(field, limit)), limit)
                    row.update(model_alias=payload.get("model"), stream=bool(payload.get("stream")), requested_output_limit=payload[field], request_sha256=hashlib.sha256(dumps(payload).encode()).hexdigest())
                    before = set(usage_rows(outer.data))
                    req = urllib.request.Request("http://127.0.0.1:18782/v1/chat/completions", dumps(payload).encode(), {"Authorization": "Bearer " + outer.key, "Content-Type": "application/json"})
                    receipt, raw, endings = UsageReceipt(), bytearray(), []
                    with RequestDeadline(outer.timeout) as deadline, deadline.open(req) as response:
                        self.send_response(response.status)
                        self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                        self.end_headers()
                        started = True
                        while True:
                            deadline.before_read()
                            line = response.readline()
                            if not line:
                                break
                            receipt.line(line)
                            if line.startswith(b"data:"):
                                try:
                                    endings.extend(finish_reasons(json.loads(line[5:].strip())))
                                except ValueError:
                                    pass
                            raw.extend(line)
                            self.wfile.write(line)
                            self.wfile.flush()
                    if not payload.get("stream"):
                        parsed = json.loads(raw)
                        receipt.accept(parsed)
                        endings.extend(finish_reasons(parsed))
                    receipt.complete()
                    row.update(status="completed", finish_reasons=endings, response_sha256=hashlib.sha256(raw).hexdigest(), response_text=raw.decode("utf-8", errors="replace"), usage=receipt.values, reported_total=receipt.total, usage_state=receipt.state)
                except Exception as exc:
                    if row is not None:
                        row.update(status="failed", error_type=type(exc).__name__)
                    if not started:
                        try:
                            self.send_response(429 if isinstance(exc, LimitReached) else 502)
                            self.send_header("Content-Type", "application/json")
                            self.end_headers()
                            self.wfile.write(b'{"error":{"message":"bounded synthetic verification stopped"}}')
                        except OSError:
                            pass
                finally:
                    if row is not None:
                        row["finished_at"] = time.time()
                        if before is not None:
                            after = usage_rows(outer.data)
                            candidates = [after[rid] for rid in sorted(set(after) - before)]
                            # Concurrent gateway activity is never silently attributed.
                            row["ledger_candidates"] = candidates
                            row["ledger_attribution"] = "unique_request_window" if len(candidates) == 1 else "ambiguous_concurrent_window"
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port) + "/v1"
        return self.url

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


class NativeHost:
    def __init__(self, root, patch, key):
        self.root, self.patch, self.key = root, patch, key
        self.port, self.child, self.log = free_port(), None, None

    def start(self):
        home = self.root / "dsh"
        self.log = (self.root / "native-host.log").open("w")
        self.child = subprocess.Popen([sys.executable, str(ROOT / "runtime/launch_dsh.py"), "--home", str(home), "--port", str(self.port), "--patch", str(self.patch)], env=dict(os.environ, DSH_GATEWAY_KEY=self.key), stdout=self.log, stderr=self.log, start_new_session=True)
        until = time.monotonic() + 90
        while not (home / "bridge-auth.json").exists():
            if self.child.poll() is not None or time.monotonic() > until:
                raise RuntimeError("隔离 DSH 启动失败")
            time.sleep(.2)
        bridge = DSHBridge("http://127.0.0.1:" + str(self.port), home)
        bridge.rpc("session/list", {"_request": {}})
        return bridge

    def close(self):
        if self.child and self.child.poll() is None:
            os.killpg(self.child.pid, signal.SIGTERM)
            try:
                self.child.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(self.child.pid, signal.SIGKILL)
                self.child.wait()
        if self.log:
            self.log.close()


def assistant_text(events):
    return ["\n".join(p.get("text", "") for p in e.get("data", {}).get("message", {}).get("content", []) if p.get("type") == "text") for e in events if e.get("type") == "assistant/message"]


class Scenario:
    def __init__(self, app, bridge, relay, report, timeout):
        self.app, self.bridge, self.relay, self.report, self.timeout = app, bridge, relay, report, timeout
        self.worker = Worker(app.store, bridge, app.records, app.config)

    def job(self, kind, target):
        # Only disposable queue scheduling is controlled here. Execution uses
        # the actual Worker claim/receipt path; incidental summaries are deferred.
        with self.app.store.db(True) as db:
            row = db.execute("SELECT id FROM jobs WHERE kind=? AND target=? ORDER BY rowid DESC LIMIT 1", (kind, target)).fetchone()
            if not row:
                raise RuntimeError("所需业务 job 不存在")
            ident = row[0]
            db.execute("UPDATE jobs SET status='validation_deferred' WHERE status='pending'")
            db.execute("UPDATE jobs SET status='pending',run_after=? WHERE id=?", (self.app.store.stamp(), ident))
        if not self.worker.work_one():
            raise RuntimeError("业务 job 未执行")
        with self.app.store.db() as db:
            state = db.execute("SELECT status FROM jobs WHERE id=?", (ident,)).fetchone()[0]
        if state != "done":
            raise RuntimeError("业务 job 未完成；停止而不自动重试：" + state)

    def wait_turn(self, sid, after):
        until = time.monotonic() + self.timeout
        while time.monotonic() < until:
            if self.relay.budget.rejections:
                raise LimitReached("原生工具续轮达到验证上限")
            events = self.bridge.events(sid, after)
            ends = [e for e in events if e.get("type") == "turn/end"]
            if ends:
                reason = ends[-1].get("data", {}).get("reason", {}).get("kind")
                if reason != "completed":
                    raise RuntimeError("实际原生 turn 未完成：" + str(reason))
                if any("length" in row.get("finish_reasons", []) for row in self.relay.budget.records if row["turn"] == self.relay.budget.turn):
                    raise RuntimeError("实际模型输出因验证上限截断；不能视为完整内容")
                answer = [text for text in assistant_text(events) if text.strip()]
                if not answer:
                    raise RuntimeError("原生 turn 无可读回答，保留待核对")
                self.worker.sync_all()
                return {"session_id": sid, "events": events, "assistant_text": answer, "pending_questions": self.bridge.pending_questions(sid)}
            time.sleep(.3)
        self.bridge.cancel_processing(sid)
        raise TimeoutError("实际模型轮次超时；已取消隔离会话")

    def turn(self, activity, label, body=None):
        self.relay.budget.begin(label)
        sid = self.worker.ensure_session(activity["id"])
        after = max((e["seq"] for e in self.bridge.events(sid)), default=-1)
        if body is None:
            self.job("learning", activity["id"])
            receipt = None
        else:
            receipt = self.app.action({"op": "message", "activity_id": activity["id"], "submission_id": "live-validation-" + uuid.uuid4().hex, "body": body})
            self.job("handoff", receipt["receipt"])
        if activity["routine"] == "A1":
            selection = self.app.store.meta("learning_selection:" + activity["id"])
            self.report.setdefault("learning_selections", {})[activity["id"]] = json.loads(selection) if selection else None
        try:
            result = self.wait_turn(sid, after)
        except Exception:
            self.report.setdefault("incomplete_turns", []).append({"label": label, "session_id": sid, "synthetic_user_input": body, "events": self.bridge.events(sid, after)})
            raise
        if activity["routine"] == "A1":
            raw = self.app.store.meta("learning_selection:" + activity["id"])
            selection = json.loads(raw) if raw else None
            result["learning_selection"] = selection
            if body is None and selection:
                answer = urllib.parse.unquote("\n".join(result["assistant_text"]))
                complete = all(urllib.parse.unquote(card["source_url"]) in answer for card in selection["cards"])
                result["all_three_source_links_present"] = complete
                if selection.get("world_fun_fact"):
                    complete = complete and urllib.parse.unquote(selection["world_fun_fact"]["source_url"]) in answer
                result["all_selected_source_links_present"] = complete
                if not complete:
                    self.report.setdefault("incomplete_turns", []).append({"label": label, **result})
                    raise RuntimeError("A1初轮遗漏来源包链接；保留原文，不据生成成功标为完整")
        result.update(label=label, synthetic_user_input=body, receipt=receipt, business_state=self.app.store.activity(activity["id"])["activity"])
        self.report["turns"].append(result)
        print(label + ": actual model response recorded", flush=True)

    def summary(self, activity):
        label = activity["routine"] + ".semantic-summary"
        self.relay.budget.begin(label)
        self.report.setdefault("summary_inputs", []).append({"label": label, "snapshot": self.app.store.summary_snapshot(activity["id"])})
        original = urllib.request.urlopen
        def routed(req, *args, **kwargs):
            if req.full_url != "http://127.0.0.1:18782/v1/chat/completions":
                raise RuntimeError("摘要出现非预期目标地址")
            # Transport-only substitution keeps the original summary method,
            # parser and commit logic while enforcing the same hard call cap.
            redirected = urllib.request.Request(self.relay.url + "/chat/completions", req.data, {"Authorization": "Bearer " + self.relay.local_key, "Content-Type": "application/json"})
            return original(redirected, *args, **kwargs)
        with patch_object("dsh_personal.worker.urllib.request.urlopen", routed):
            self.worker.semantic_summary(activity["id"])
        view = self.app.store.activity(activity["id"])
        if view["summary"].get("method") != "model_extraction":
            raise RuntimeError("摘要不是实际模型抽取")
        self.report["summaries"].append({"label": label, "content": view["summary"], "state": view["summary_state"]})

    def run(self, phase):
        if phase == "teaching-repair":
            from dsh_personal.learning import prepare_learning
            selected=[]
            for day in ("2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22"):
                activity=self.app.store.ensure_activity("A1",day)
                prepare_learning(self.app.store,activity["id"])
                selected.append(activity)
            self.report["repair_scope"]="Two explicit synthetic user questions use foreground accounting. This rechecks identified source-context errors, not scheduled initial lesson delivery or user mastery."
            self.turn(selected[1], "A1.repair.sequence", "合成练习：我把顺序写成‘先在陈称王，再借扶苏项燕名望动员，苟富贵无相忘就是起事现场说的’。请直接纠正这条顺序，说明这句原话实际属于什么情境，再解释动员、杀将尉、占陈称王的联系。不要用未经给定的事实补齐。")
            self.turn(selected[3], "A1.repair.sunzi", "请直接讲清《谋攻》知彼知己这段：一胜一负能否当成50%胜率？同篇有没有具体判断条件？讲其中两项，并说明怎样有限地用于项目判断。暂时不做题。")
            self.report["activities"]=[self.app.store.activity(a["id"]) for a in (selected[1],selected[3])]
            return
        if phase == "teaching":
            from dsh_personal.learning import prepare_learning
            selected = []
            for day in ("2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22"):
                activity = self.app.store.ensure_activity("A1", day)
                prepare_learning(self.app.store, activity["id"])
                selected.append(activity)
            self.report["selection_setup"] = "Four disposable historical A1 selections; only packs 1 and 3 generate lessons. Selection is not delivery or mastery."
            self.turn(selected[1], "A1.initial.chinese-history")
            self.turn(selected[1], "A1.practice.feedback", "以下是合成练习作答：我的理解是陈胜先在陈称王，再借扶苏和项燕的名望召集戍卒，所以只要打出名号就能稳定统治。请指出哪部分错了，并说明正确的事件联系；不换话题。")
            self.turn(selected[3], "A1.initial.cpc-history")
            self.turn(selected[3], "A1.direct.explanation", "请直接解释1978公报中的工作重点转移，怎样与企业自主权、农业措施和法制安排联系起来。暂时不做题，请不要先问我问题；用三四段说明，不把后来效果写成当时已实现。")
            self.report["activities"] = [self.app.store.activity(a["id"]) for a in (selected[1], selected[3])]
            return
        if phase == "smoke":
            self.turn(self.app.store.ensure_activity("A3"), "smoke.A3", PROMPTS["A3"][0])
            return
        if phase == "focused":
            activity = self.app.store.ensure_activity("A3", "2026-10-22")
            for body in PROMPTS["A3"][:3]:
                self.app.store.submit(activity["id"], "focus-history-" + uuid.uuid4().hex, body)
            self.app.action({"op": "correction", "activity_id": activity["id"], "fields": {"wake_time": "07:40", "quality": "4分"}, "source": "合成验证人工更正：起床应为07:40，主观质量应为4分。"})
            self.app.store.set_meta("native_generation:" + activity["id"], "1")
            # Existing user history is synthetic setup, not three new provider
            # conversations. Only summary + final continuation are paid calls.
            self.summary(activity)
            self.turn(activity, "focused.A3-corrected-continuation", PROMPTS["A3"][3] + "本轮只需简洁回答，不提出新问题、不调用提问工具；请明确评分范围，并说明能否仅由一晚前后比较判断阅读调整的因果作用。")
            self.report["activities"].append(self.app.store.activity(activity["id"]))
            self.report["focused_setup_note"] = "Three synthetic source messages were seeded locally; this focused run does not re-prove three provider conversation rounds."
            return
        profile = self.app.action({"op": "profile_save", "content": "虚构验证人物的排期核对窗口是周一上午；仅为测试旧版本。", "confirmed": True, "user_input": "合成验证确认，不代表真实用户"})
        hypothesis = self.app.records.add_profile("虚构验证人物可能偏好夜间工作；这是未经确认的假设。", category="hypothesis")
        routines = ("A1",) if phase == "learning" else ("A2", "A3") if phase == "routines" else ("A1", "A2", "A3")
        for routine in routines:
            activity = self.app.store.ensure_activity(routine, "2026-10-22")
            self.turn(activity, routine + ".initial", None if routine == "A1" else PROMPTS[routine][0])
            followups = PROMPTS[routine] if routine == "A1" else PROMPTS[routine][1:]
            for index, body in enumerate(followups, 1):
                if routine in ("A2", "A3") and index == 3:
                    if routine == "A3":
                        self.app.action({"op": "correction", "activity_id": activity["id"], "fields": {"wake_time": "07:40", "quality": "4分"}, "source": "合成验证人工更正：原07:00和2分有误，不是实际用户资料。"})
                        self.app.action({"op": "profile_save", "item_id": profile["item_id"], "version": profile["version"], "content": "虚构验证人物当前排期核对窗口是周五下午；旧周一上午已被更正。", "confirmed": True, "user_input": "合成验证近期修订，不代表实际用户"})
                    self.summary(activity)
                self.turn(activity, routine + ".followup." + str(index), body)
            self.report["activities"].append(self.app.store.activity(activity["id"]))
        if phase == "learning":
            return
        self.relay.budget.begin("current-profile.delegated-task")
        task = self.app.store.create_task("合成验证：根据当前有效画像，写出虚构人物的排期核对窗口，并说明夜间工作偏好是否已确认。只根据所给资料，不把假设当事实。交付两到四句话。", inputs="隔离开发样本，不是实际用户任务。", deliverable="简短核对结果", acceptance="使用当前周五下午，不沿用旧周一上午；未知偏好保持未知。")
        self.worker.dispatch_tasks()
        current = self.app.store.task(task["id"])
        if current["status"] != "running":
            raise RuntimeError("合成委托没有启动")
        with self.app.store.db() as db:
            sid = db.execute("SELECT session_id FROM task_runs WHERE task_id=? ORDER BY version DESC LIMIT 1", (task["id"],)).fetchone()[0]
        response = self.wait_turn(sid, -1)
        current = self.app.store.task(task["id"])
        self.report["current_profile_task"] = {"task": current, "native": response, "confirmed_context": self.app.records.context(), "excluded_hypothesis_id": hypothesis["item_id"], "human_accepted": False}
        if current["status"] != "awaiting_acceptance":
            raise RuntimeError("实际模型未交付可读成果，不冒充接受")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--phase", choices=("smoke", "routines", "learning", "teaching", "teaching-repair", "focused", "full"), default="smoke")
    parser.add_argument("--production-data", type=Path, default=Path.home() / ".local/share/dsh-personal")
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--per-turn", type=int, default=3)
    parser.add_argument("--max-tokens", type=int, default=1200)
    parser.add_argument("--learning-initial-limit", type=int, default=1200)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    args.max_calls = args.max_calls or {"smoke": 3, "routines": 18, "learning": 8, "teaching": 6, "teaching-repair": 2, "focused": 2, "full": 24}[args.phase]
    if not 1 <= args.max_calls <= 25 or not 1 <= args.per_turn <= 3 or not 1 <= args.max_tokens <= 1200 or not 1 <= args.learning_initial_limit <= 8192 or not 30 <= args.timeout <= 300:
        parser.error("调用上限1–25，每轮1–3，续轮输出1–1200，首课输出1–8192，超时30–300秒")
    report = {"mode": "plan_only", "phase": args.phase, "synthetic_only": True, "production_records_written": False, "production_usage_written_by_gateway": True, "minimum_provider_requests": {"smoke": 1, "routines": 11, "learning": 4, "teaching": 4, "teaching-repair": 2, "focused": 2, "full": 15}[args.phase], "maximum_provider_requests": args.max_calls, "maximum_per_turn": args.per_turn, "output_limit": args.max_tokens, "human_acceptance": False, "manual_review_required": ["T01.content source accuracy", "T02.depth continuity", "T05.three_routines semantic context", "T17.current correction and uncertainty", "T21.budget provider usage semantics"]}
    if not args.run:
        report["learning_initial_limit"] = args.learning_initial_limit
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    target = args.report or ROOT / "evidence/P4" / ("live-model-" + args.phase + "-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + ".json")
    if target.exists():
        parser.error("报告已存在；请使用新文件名，保留初次失败证据")
    model = json.loads((args.production_data / "config.json").read_text()).get("model", {})
    report["model"] = {k: model[k] for k in ("name", "base_url", "max_tokens", "thinking") if k in model}
    gateway_key = (args.production_data / "gateway-key").read_text().strip()
    budget = CallBudget(args.max_calls, args.per_turn)
    relay = GatewayRelay(args.production_data, gateway_key, budget, args.max_tokens, args.timeout, args.learning_initial_limit)
    report["learning_initial_limit"] = args.learning_initial_limit
    source_files = ["scripts/verify-live-model.py", "dsh_personal/worker.py", "dsh_personal/gateway.py", "runtime/personal.patch.json"]
    if (ROOT / "dsh_personal/learning.py").exists():
        source_files.append("dsh_personal/learning.py")
    report.update(mode="actual_provider_synthetic_scenarios", started_at=time.time(), turns=[], summaries=[], activities=[], source_sha256={name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in source_files})
    host = None
    with tempfile.TemporaryDirectory(prefix="dsh-live-model-synthetic-") as directory:
        root = Path(directory)
        os.chmod(root, 0o700)
        try:
            app = Application(root / "instance")
            app.config_path.write_text(dumps({"model": {"name": model.get("name")}, "notification": {}, "backup": {}}))
            native_patch = json.loads((ROOT / "runtime/personal.patch.json").read_text())
            provider = next(p for p in native_patch if p.get("id") == "llm-pi-ai")["config"]["providers"]["dsh-personal"]
            native_cap = max(args.max_tokens, args.learning_initial_limit)
            provider["baseURL"], provider["defaultMaxTokens"] = relay.start(), native_cap
            for entry in provider["models"]:
                entry["maxTokens"] = native_cap
            patch_path = root / "native.patch.json"
            patch_path.write_text(dumps(native_patch))
            host = NativeHost(app.data, patch_path, relay.local_key)
            bridge = host.start()
            scenario = Scenario(app, bridge, relay, report, args.timeout)
            scenario.run(args.phase)
            report["status"] = "ENGINEERING_SCENARIOS_RECORDED_REVIEW_REQUIRED"
        except Exception as exc:
            report.update(status="FAILED_OR_INCOMPLETE", error_type=type(exc).__name__, error=str(exc)[:300] if isinstance(exc, (LimitReached, RuntimeError, TimeoutError, ValueError)) else "Details withheld; inspect labelled synthetic evidence.")
        finally:
            if host:
                host.close()
            relay.close()
            report.update(calls=budget.records, call_count=len(budget.records), rejected_calls=budget.rejections, finished_at=time.time(), synthetic_instance_removed=True)
            report["usage_window_note"] = "Response usage belongs to this relay. Concurrent ledger candidates are explicitly ambiguous; no aggregate production window is treated as test usage."
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            os.chmod(target, 0o600)
            print("Report: " + str(target.resolve()), flush=True)
    return 0 if report["status"] == "ENGINEERING_SCENARIOS_RECORDED_REVIEW_REQUIRED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
