#!/usr/bin/env python3
"""Real DSH Web process and storage probes with a labelled deterministic model fixture.
No external model, credentials, user records or phone are used.
"""
from __future__ import annotations
import copy
import http.server
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.dsh_bridge import DSHBridge
from dsh_personal.server import Application
from dsh_personal.worker import Worker

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / 'evidence/P0/probe.json'

class Fixture(http.server.BaseHTTPRequestHandler):
    unavailable = False
    requests = []
    def log_message(self, *a):
        pass
    def do_GET(self):
        raw=json.dumps({'data':[{'id':x} for x in ['configured-model','foreground-model','delegated-model']]}).encode()
        self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(raw)
    def do_POST(self):
        payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        Fixture.requests.append({'model':payload.get('model'),'stream':payload.get('stream'), 'message_count':len(payload.get('messages',[])), 'response_status':503 if Fixture.unavailable else 200})
        if self.unavailable:
            self.send_response(503);self.send_header('Content-Type','application/json');self.end_headers()
            self.wfile.write(b'{"error":{"message":"P0 fixture provider unavailable","type":"server_error"}}');return
        messages=payload.get('messages',[])
        called=any(m.get('role')=='tool' for m in messages)
        tools=payload.get('tools',[])
        tool_name=next((t['function']['name'] for t in tools if 'ask_user_question' in t['function']['name']),None)
        if not called and tool_name and any('P0_SEED' in str(m.get('content')) for m in messages):
            delta={'role':'assistant','tool_calls':[{'index':0,'id':'call_p0_question','type':'function','function':{'name':tool_name,'arguments':json.dumps({'questions':[{'id':'sleep','question':'P0固定样本：昨晚睡得怎么样？'}],'timeout':1},ensure_ascii=False)}}]}
            finish='tool_calls'
        else:
            content='P0固定样本已回应，可以继续讨论。'
            if payload.get('model')=='delegated-model':
                content=json.dumps({'status':'delivered','artifact':content,'verification':['本地固定夹具已返回；非真实模型能力验证'],'limitations':['没有实际供应商或用户验收']},ensure_ascii=False)
            delta={'role':'assistant','content':content};finish='stop'
        chunks=[{'id':'p0-'+uuid.uuid4().hex,'object':'chat.completion.chunk','created':int(time.time()),'model':payload.get('model'),'choices':[{'index':0,'delta':delta,'finish_reason':None}]},
                {'id':'p0-end','object':'chat.completion.chunk','created':int(time.time()),'model':payload.get('model'),'choices':[{'index':0,'delta':{},'finish_reason':finish}], 'usage':{'prompt_tokens':100,'completion_tokens':30,'total_tokens':130}}]
        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
        for chunk in chunks:
            self.wfile.write(('data: '+json.dumps(chunk,ensure_ascii=False)+'\n\n').encode())
        self.wfile.write(b'data: [DONE]\n\n');self.wfile.flush()

def wait_for(fn, timeout=45):
    end=time.monotonic()+timeout;last=None
    while time.monotonic()<end:
        try:
            value=fn()
            if value:return value
        except Exception as e:last=e
        time.sleep(.15)
    raise TimeoutError(f'Condition not reached: {last}')

def free_port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]

def main():
    root=Path(tempfile.mkdtemp(prefix='dsh-personal-probe-'))
    os.chmod(root,0o700)
    home=root/'home';port=free_port();server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Fixture)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    patch=json.loads((ROOT/'runtime/personal.patch.json').read_text())
    route=next(p for p in patch if p.get('id')=='llm-pi-ai')['config']['providers']['dsh-personal']
    route['baseURL']=f'http://127.0.0.1:{server.server_port}/v1'
    (root/'patch.json').write_text(json.dumps(patch,ensure_ascii=False))
    log=(root/'host.log').open('a');child=None
    results={'kind':'real_dsh_process_with_mock_model','source_commit':'687f282e8625c6b8d11ff58211a90524be2e4317',
             'version':'0.2.1-rc.2','started_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'isolated_root':str(root),'checks':[]}
    def record(name,details):
        results['checks'].append({'name':name,'status':'PASS','actual_result':details});print(name+': PASS',flush=True)
    def start():
        nonlocal child
        auth=home/'bridge-auth.json'
        if auth.exists():auth.unlink()
        child=subprocess.Popen([sys.executable,str(ROOT/'runtime/launch_dsh.py'),'--home',str(home),'--port',str(port),'--patch',str(root/'patch.json')],
                               env=dict(os.environ,DSH_GATEWAY_KEY='isolated-fixture-not-a-secret'),stdout=log,stderr=log)
        wait_for(lambda: auth.exists() or (child.poll() is not None and (_ for _ in ()).throw(RuntimeError('host exited'))),90)
        bridge=DSHBridge(f'http://127.0.0.1:{port}',home)
        wait_for(lambda:bridge.rpc('session/list',{'_request':{}}) is not None)
        return bridge
    def stop():
        nonlocal child
        if child and child.poll() is None:
            child.send_signal(signal.SIGTERM)
            try:child.wait(15)
            except subprocess.TimeoutExpired:child.kill();child.wait()
        child=None
    try:
        bridge=start()
        try:
            urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:{port}/api/session/list',b'{}',{'Content-Type':'application/json'}))
            raise AssertionError('unauthenticated RPC allowed')
        except urllib.error.HTTPError as e:assert e.code==401
        record('unauthenticated_rpc_rejected','实际未带cookie的RPC返回401')
        sid=bridge.ensure_session('fixture-A3-2026-10-20',root/'work')
        assert bridge.ensure_session('fixture-A3-2026-10-20',root/'work')==sid
        record('stable_session_after_lost_association',{'session_id':sid,'returned_twice':True})
        bridge.submit(sid,'p0-seed','P0_SEED 请发出固定睡眠问题，随后结束当前计算。')
        pending=wait_for(lambda:[q for q in (bridge.projections(sid) or {}).get('values',{}).get('userQuestions',{}).get('active',[]) if q['state']=='continued'])
        wait_for(lambda:any(e['type']=='turn/end' for e in bridge.events(sid)))
        record('native_timed_pending_after_turn',{'active':pending,'event_count':len(bridge.events(sid))})
        app=Application(root/'business')
        activity=app.store.ensure_activity('A3','2026-10-20')
        with app.store.db(True) as c:
            c.execute("UPDATE activities SET session_id=?,session_state='ready' WHERE id=?",(sid,activity['id']))
        worker=Worker(app.store,bridge,app.records,app.config)
        worker.sync_all()
        native_events=bridge.events(sid)
        visible=app.store.activity(activity['id'])['messages']
        results['native_event_samples']=[e for e in native_events if e['type'] in {'assistant/message','tool/call','tool/result','turn/end'}]
        assert any('昨晚睡得怎么样' in m['body'] for m in visible), visible
        assert any('P0固定样本已回应' in m['body'] for m in visible), visible
        assert any(m['role']=='tool' and '"pending":true' in m['body'] for m in visible), visible
        record('real_native_question_and_tool_result_business_sync',{'visible_messages':len(visible),'roles':[m['role'] for m in visible]})
        stop();bridge=start()
        assert bridge.ensure_session('fixture-A3-2026-10-20',root/'work')==sid
        pending=bridge.projections(sid)['values']['userQuestions']['active'];assert pending
        record('pending_question_survives_restart',{'session_id':sid,'active':pending})
        stop()
        old_home=home
        source_hashes={str(p.relative_to(old_home)):hashlib.sha256(p.read_bytes()).hexdigest() for p in old_home.rglob('*') if p.is_file()}
        home=root/'relocated'/'dsh'
        shutil.copytree(old_home,home)
        bridge=start()
        assert bridge.ensure_session('fixture-A3-2026-10-20',root/'work')==sid
        pending=bridge.pending_questions(sid)
        assert pending and pending[0]['state']=='continued'
        record('cold_native_home_relocation_preserves_pending',{'original_home':str(old_home),'new_home':str(home),'session_id':sid,'pending_call_id':pending[0]['callId'],'scope':'same_host_independent_directory_copy_not_fresh_machine'})
        Fixture.unavailable=True
        worker.bridge=bridge
        receipt=app.action({'op':'message','activity_id':activity['id'],'submission_id':'p0-targeted-answer',
                           'body':'睡得一般','call_id':pending[0]['callId'],'question_id':'sleep'})
        assert receipt['receipt']
        assert worker.work_one() is True
        stored=next(m for m in app.store.activity(activity['id'])['messages'] if m['id']==receipt['receipt'])
        assert stored['processing_state']=='accepted_by_dsh',stored
        answer=bridge.answer_pending(sid,pending[0]['callId'],[{'id':'sleep','selected':[],'custom':'睡得一般'}])
        assert answer['accepted'] is True
        assert bridge.answer_pending(sid,pending[0]['callId'],[{'id':'sleep','selected':[],'custom':'睡得一般'}])['reconciled'] is True
        wait_for(lambda:not bridge.projections(sid)['values']['userQuestions']['active'])
        record('late_native_answer_through_business_worker',{'same_session':sid,'accepted':answer,'business_processing_state':stored['processing_state']})
        wait_for(lambda:any(r['response_status']==503 for r in Fixture.requests))
        wait_for(lambda:[e for e in bridge.events(sid) if e['type']=='turn/end' and e['data']['reason']['kind']=='error'])
        Fixture.unavailable=False
        before_answer_retry=len([e for e in bridge.events(sid) if e['type']=='assistant/message'])
        answer_retry=bridge.resume_processing(sid,'p0-answer-retry','question:'+pending[0]['callId'])
        wait_for(lambda:len([e for e in bridge.events(sid) if e['type']=='assistant/message'])>before_answer_retry)
        assert len([e for e in bridge.events(sid) if e['type']=='user/message' and e['data'].get('source',{}).get('kind')=='user-question-reply'])==1
        record('targeted_answer_failed_computation_resume',{'accepted':answer_retry['accepted'],'original_answer_events':1,'model':Fixture.requests[-1]['model']})
        unchanged=all(hashlib.sha256((old_home/p).read_bytes()).hexdigest()==digest for p,digest in source_hashes.items())
        assert unchanged
        record('relocated_late_answer_does_not_mutate_source',{'source_files_unchanged':True,'source_file_count':len(source_hashes)})
        bridge.submit(sid,'p0-followup','还想继续讨论睡眠安排。',kind='foreground')
        wait_for(lambda:bridge.reconcile(sid,'p0-followup')['accepted'])
        bridge.submit(sid,'p0-followup','还想继续讨论睡眠安排。',kind='foreground')
        time.sleep(.5)
        matches=[e for e in bridge.events(sid) if e['type']=='user/message' and e['data'].get('source',{}).get('rpcId')=='p0-followup']
        assert len(matches)==1, matches
        record('plain_followup_and_retry_deduplicated',{'matching_native_messages':len(matches),'same_session':sid})
        failed_count=sum(r['response_status']==503 for r in Fixture.requests)
        Fixture.unavailable=True
        receipt=bridge.submit(sid,'p0-offline','模型不可用时原文仍须保存。')
        assert receipt['accepted'] is True
        saved=wait_for(lambda:bridge.reconcile(sid,'p0-offline')['accepted'])
        wait_for(lambda:sum(r['response_status']==503 for r in Fixture.requests)>failed_count)
        stop();bridge=start()
        recovered=bridge.reconcile(sid,'p0-offline');assert recovered['accepted']
        record('provider_failure_durable_receipt_and_restart',{'receipt':receipt,'recovered':recovered})
        Fixture.unavailable=False
        before=bridge.events(sid)
        original_count=sum(e['type']=='user/message' and e['data'].get('source',{}).get('kind')=='user' for e in before)
        resumed=bridge.resume_processing(sid,'p0-retry-once','p0-offline')
        assert resumed['accepted'] is True
        wait_for(lambda:sum(e['type']=='assistant/message' for e in bridge.events(sid)) > sum(e['type']=='assistant/message' for e in before))
        again=bridge.resume_processing(sid,'p0-retry-once','p0-offline');assert again.get('reconciled') is True
        after=bridge.events(sid)
        assert sum(e['type']=='user/message' and e['data'].get('source',{}).get('kind')=='user' for e in after)==original_count
        control=[e for e in after if e['type']=='user/message' and e['data'].get('source',{}).get('kind')=='dsh-personal-retry' and e['data']['source']['retryId']=='p0-retry-once']
        assert len(control)==1
        record('resume_failed_computation_without_duplicate_human_message',{'original_user_messages':original_count,'technical_control_events':len(control),'retry_reconciled':again['reconciled']})
        stop();bridge=start()
        assert bridge.resume_processing(sid,'p0-retry-once','p0-offline').get('reconciled') is True
        record('retry_control_survives_restart',{'same_retry_id':'p0-retry-once','reconciled':True})
        task=app.store.create_task('固定样本文本交付')
        run=app.store.claim_task(task['id'])
        task_sid=bridge.ensure_session('fixture-task-'+run['run_id'],root/'work')
        with app.store.db(True) as c:c.execute('UPDATE task_runs SET session_id=? WHERE id=?',(task_sid,run['run_id']))
        bridge.submit(task_sid,run['run_id'],'请交付固定样本文本。',kind='delegated')
        task_ends=wait_for(lambda:[e for e in bridge.events(task_sid) if e['type']=='turn/end' and e['data'].get('reason',{}).get('kind')=='completed'])
        results['task_event_samples']=bridge.events(task_sid)
        worker.bridge=bridge;worker.sync_all()
        delivered=app.store.task(task['id'])
        assert delivered['status']=='awaiting_acceptance', delivered
        assert 'P0固定样本已回应' in delivered['deliveries'][0]['body']
        record('real_native_completed_turn_business_delivery',{'reason':task_ends[-1]['data']['reason'],'status':delivered['status'],'version':delivered['version']})
        results['status']='PASS';results['requests']=Fixture.requests
    except Exception as exc:
        results['status']='FAIL';results['error']=str(exc);print(type(exc).__name__+': '+str(exc),flush=True)
        log.flush();print((root/'host.log').read_text()[-10000:],flush=True)
    finally:
        stop();server.shutdown();log.close()
        results['finished_at']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
        REPORT.parent.mkdir(parents=True,exist_ok=True);REPORT.write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
    return 0 if results['status']=='PASS' else 1

if __name__=='__main__':sys.exit(main())
