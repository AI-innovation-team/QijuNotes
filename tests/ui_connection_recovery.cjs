// Run: node tests/ui_connection_recovery.cjs [--web-root .runtime/ui-redesign/web]
// Real isolated HTTP server + Chromium. Faults only withhold/fail real responses.
const { chromium }=require('../.runtime/browser/node_modules/playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const net=require('node:net');
const os=require('node:os');
const {spawn,execFileSync}=require('node:child_process');
const {randomBytes,createHash}=require('node:crypto');
const project=path.resolve(__dirname,'..');
const webArg=process.argv.indexOf('--web-root');
const webRoot=path.resolve(project,webArg<0?'web':process.argv[webArg+1]);
const hash=file=>createHash('sha256').update(fs.readFileSync(file)).digest('hex');
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
const until=async(fn,timeout=15000)=>{const start=Date.now();while(Date.now()-start<timeout){if(await fn())return;await pause(100);}throw Error('bounded test wait expired');};
const freePort=()=>new Promise(resolve=>{const s=net.createServer();s.listen(0,'127.0.0.1',()=>{const p=s.address().port;s.close(()=>resolve(p));});});

(async()=>{
 const fixture=fs.mkdtempSync(path.join(os.tmpdir(),'dsh-ui-connection-recovery-'));
 const data=path.join(fixture,'data');
 const py=(code,args=[])=>execFileSync('python3',['-c',code,data,...args],{cwd:project,encoding:'utf8'});
 const report={started_at:new Date().toISOString(),kind:'isolated_real_http_chromium_fault_injection_not_harmony_phone',state:'FAIL',fixture_filesystem:'private OS temporary directory; project mounted volume cannot preserve private mode',production_accessed:false,worker_enabled:false,real_device_claimed:false,model_and_notification_configured:false,web_root:path.relative(project,webRoot),source_sha256:hash(path.join(webRoot,'app.js')),script_sha256:hash(__filename),checks:{},page_errors:[]};
 let server,browser,serverLog='',failReads=false,holdFirstReceipt=true;
 const lostReceipt=deferred(),releaseReceipt=deferred(),messageRequests=[],readRequests=[],eventRequests=[];
 try{
  const activity=py(`from pathlib import Path
import sys
from dsh_personal.server import Application
a=Application(Path(sys.argv[1]))
assert a.config()['model']=={} and a.config()['notification']=={}
print(a.store.ensure_activity('CHAT')['id'])
`).trim();
  const durable=()=>JSON.parse(py(`import sqlite3,sys,urllib.parse,json
from pathlib import Path
c=sqlite3.connect('file:'+urllib.parse.quote(str(Path(sys.argv[1])/'personal.sqlite'))+'?mode=ro',uri=True)
rows=c.execute("SELECT submission_id,body FROM messages WHERE role='user' AND activity_id=?",(sys.argv[2],)).fetchall()
print(json.dumps({'count':len(rows),'submissions':[r[0] for r in rows],'bodies':[r[1] for r in rows]}))
`,[activity]));
  const port=await freePort();let gateway=await freePort();while(gateway===port)gateway=await freePort();
  const origin=`http://127.0.0.1:${port}`;
  server=spawn('python3',['-m','dsh_personal.server','--data',data,'--host','127.0.0.1','--port',String(port),'--gateway-port',String(gateway),'--web-root',webRoot,'--no-worker'],{cwd:project,stdio:['ignore','pipe','pipe']});
  server.stdout.on('data',b=>serverLog+=b);server.stderr.on('data',b=>serverLog+=b);
  await until(()=>{if(server.exitCode!==null)throw Error('isolated server exited before readiness');return serverLog.includes('listening on');});
  browser=await chromium.launch({headless:true,executablePath:(process.env.QIJU_CHROMIUM_PATH || chromium.executablePath()),args:['--no-sandbox']});
  const context=await browser.newContext({viewport:{width:1280,height:900}});
  await context.addInitScript(()=>{
   // VPN reachability may fail while a device still reports a working link.
   Object.defineProperty(navigator,'onLine',{configurable:true,get:()=>true});
   const NativeEventSource=window.EventSource;
   window.__eventSourceProbe={created:0,active:0,maxActive:0,instances:[]};
   window.EventSource=class extends NativeEventSource{
    constructor(...args){super(...args);const p=window.__eventSourceProbe;p.created++;p.active++;p.maxActive=Math.max(p.maxActive,p.active);p.instances.push(this);this.__closedByApp=false;}
    close(){if(!this.__closedByApp){this.__closedByApp=true;window.__eventSourceProbe.active--;}return super.close();}
   };
  });
  const page=await context.newPage();
  page.on('pageerror',e=>report.page_errors.push(e.message));
  page.on('response',async response=>{if(new URL(response.url()).pathname==='/api/auth/status')report.last_auth_status={http_status:response.status(),body:await response.json().catch(()=>null)};});
  page.on('request',request=>{
   const u=new URL(request.url());if(u.origin!==origin)return;
   if(u.pathname==='/api/events')eventRequests.push(Date.now());
   else if(u.pathname.startsWith('/api/')&&request.method()==='GET')readRequests.push({at:Date.now(),path:u.pathname,failedByFixture:failReads});
   if(u.pathname==='/api/action'&&request.method()==='POST'&&request.postDataJSON()?.op==='message')messageRequests.push({at:Date.now(),...request.postDataJSON()});
  });
  await page.route('**/*',async route=>{
   const req=route.request(),u=new URL(req.url());
   if(u.origin!==origin)return route.abort();
   if(failReads&&u.pathname.startsWith('/api/')&&req.method()==='GET')return route.abort('internetdisconnected');
   if(holdFirstReceipt&&u.pathname==='/api/action'&&req.method()==='POST'&&req.postDataJSON()?.op==='message'){
    holdFirstReceipt=false;
    try{
     const response=await route.fetch();const receipt=await response.json();
     assert.equal(response.status(),200);assert.ok(receipt.receipt,'real server did not provide a durable receipt');
     lostReceipt.resolve(receipt.receipt);
     // The real server has committed. Keep its response from the UI until timeout.
     await releaseReceipt.promise;
     await route.abort('failed').catch(()=>{});
    }catch(error){lostReceipt.resolve({error:error.message});throw error;}
    return;
   }
   return route.continue();
  });
  await page.goto(origin+'/#activity/'+activity);
  const setup=page.locator('#account-setup-form');try{await setup.waitFor();}catch(error){report.pre_setup_visible_text=await page.locator('body').innerText();throw error;}
  console.log('stage: local account setup');
  const password='Fixture-'+randomBytes(18).toString('hex');
  await setup.locator('[name=username]').fill('ui-recovery-test');
  await setup.locator('[name=password]').fill(password);
  await setup.locator('[name=confirm_password]').fill(password);
  await setup.getByRole('button',{name:'创建账号并进入'}).click();
  await page.locator('#message-input').waitFor();
  report.checks.local_account_setup_through_ui=true;console.log('stage: lost message receipt');
  const input=page.locator('#message-input'),status=page.locator('#draft-status');
  const original='隔离网络测试：本条已到服务但回执将丢失。';
  const newer='隔离网络测试：提交期间继续编辑的新草稿，不发送。';
  await input.fill(original);
  const sendAt=Date.now();await page.locator('#send-button').click();
  const receipt=await Promise.race([lostReceipt.promise,pause(10000).then(()=>{throw Error('real message did not commit');})]);
  assert.equal(typeof receipt,'string');assert.equal(durable().count,1);
  await input.fill(newer);
  await until(async()=>await page.locator('#send-button').isEnabled(),22000);
  const timeoutMs=Date.now()-sendAt;
  assert.ok(timeoutMs>=13000&&timeoutMs<22000,'did not observe bounded real 15 second POST timeout');
  assert.match(await status.innerText(),/超时|未确认|未取得保存回执|无法连接|暂时/);
  let pending=await page.evaluate(id=>JSON.parse(localStorage.getItem('dsh-draft:'+id)),activity);
  assert.equal(pending.pending.submission_id,messageRequests[0].submission_id);
  assert.equal(pending.pending.body,original);assert.equal(await input.inputValue(),newer);
  releaseReceipt.resolve();
  await page.evaluate(()=>{window.dispatchEvent(new Event('online'));document.dispatchEvent(new Event('visibilitychange'));window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));});
  await pause(1500);assert.equal(messageRequests.length,1,'resume automatically repeated message POST');
  await page.reload();await input.waitFor();
  assert.equal(await input.inputValue(),newer);
  pending=await page.evaluate(id=>JSON.parse(localStorage.getItem('dsh-draft:'+id)),activity);
  assert.equal(pending.pending.submission_id,messageRequests[0].submission_id);
  await pause(1000);assert.equal(messageRequests.length,1,'reload automatically repeated message POST');
  await page.locator('#retry-send').click();
  await until(async()=>!(await page.evaluate(id=>JSON.parse(localStorage.getItem('dsh-draft:'+id)).pending,activity)));
  assert.equal(messageRequests.length,2);assert.equal(messageRequests[1].submission_id,messageRequests[0].submission_id);assert.equal(messageRequests[1].body,original);
  const stored=durable();assert.equal(stored.count,1);assert.equal(stored.bodies[0],original);assert.equal(await input.inputValue(),newer);
  report.checks.ambiguous_post={server_committed_before_timeout:true,timeout_ms:timeoutMs,pending_survived_resume_and_reload:true,automatic_reposts:0,manual_requests:messageRequests.length,retry_used_same_submission_id:true,durable_user_messages:stored.count,newer_draft_preserved:true};

  console.log('stage: read recovery');
  // Lose the read path while navigator.onLine remains true. Existing content is usable.
  await page.locator('.message.user .message-body').getByText(original,{exact:true}).waitFor();
  await input.focus();await input.evaluate(el=>el.setSelectionRange(2,5));
  await page.evaluate(()=>{window.__retainedComposer=document.querySelector('#message-input');window.__retainedMessages=document.querySelector('#messages');});
  const failedAt=Date.now();failReads=true;
  await page.evaluate(()=>{
   // Physically close existing synthetic SSE consumers and deliver the network error event.
   for(const source of window.__eventSourceProbe.instances.filter(s=>!s.__closedByApp)){source.close();source.dispatchEvent(new Event('error'));}
   window.dispatchEvent(new Event('online'));document.dispatchEvent(new Event('visibilitychange'));window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));
  });
  await page.locator('#connection-banner:not([hidden])').waitFor({timeout:15000});
  assert.equal(await page.evaluate(()=>navigator.onLine),true);
  assert.equal(await input.inputValue(),newer);
  assert.equal(await page.evaluate(()=>document.activeElement===window.__retainedComposer),true);
  assert.equal(await page.evaluate(()=>document.querySelector('#messages')===window.__retainedMessages),true);
  // Explicit reread failing must also keep the existing conversation instead of an error page.
  await page.locator('[data-action=refresh]').first().click();
  await pause(600);
  assert.equal(await page.evaluate(()=>document.querySelector('#message-input')===window.__retainedComposer),true);
  assert.equal(await page.evaluate(()=>document.querySelector('#messages')===window.__retainedMessages),true);
  assert.equal(await input.inputValue(),newer);
  await input.focus();
  await pause(9500);
  const failedReads=readRequests.filter(r=>r.at>=failedAt&&r.failedByFixture);
  assert.ok(failedReads.length>=2,'did not exercise failed reread and recovery attempts');
  assert.ok(failedReads.length<=12,'recovery read request storm');
  assert.equal(messageRequests.length,2,'offline recovery resent a mutation');
  const afterReconnect='隔离服务在断网期间追加的助手测试消息。';
  py(`from pathlib import Path
import sys
from dsh_personal.core import Store,uid
s=Store(Path(sys.argv[1])/'personal.sqlite')
with s.db(True) as c:c.execute("INSERT INTO messages VALUES(?,?,NULL,'assistant',?,?,'local_prompt',NULL,NULL,0)",(uid(),sys.argv[2],sys.argv[3],s.stamp()))
`,[activity,afterReconnect]);
  failReads=false;const recoveredAt=Date.now();
  await page.evaluate(()=>{window.dispatchEvent(new Event('online'));document.dispatchEvent(new Event('visibilitychange'));window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));});
  await page.locator('#messages').getByText(afterReconnect,{exact:true}).waitFor({timeout:18000});
  assert.equal(await input.inputValue(),newer);assert.equal(messageRequests.length,2);
  assert.equal(await page.evaluate(()=>document.querySelector('#message-input')===window.__retainedComposer),true);
  const streams=await page.evaluate(()=>({created:window.__eventSourceProbe.created,active:window.__eventSourceProbe.active,maxActive:window.__eventSourceProbe.maxActive}));
  assert.ok(streams.maxActive<=1,'more than one live SSE consumer');
  report.checks.read_recovery={navigator_online_during_fault:true,old_message_dom_retained:true,composer_dom_retained:true,draft_retained:true,focus_retained_during_background_failure:true,explicit_reread_failure_did_not_replace_conversation:true,failed_read_attempt_count:failedReads.length,failed_read_offsets_ms:failedReads.map(r=>r.at-failedAt),no_automatic_message_posts:true,reconnected_server_message_visible:true,recovery_ms:Date.now()-recoveredAt,event_source_probe:streams};
  assert.deepEqual(report.page_errors,[]);
  assert.equal(hash(path.join(webRoot,'app.js')),report.source_sha256,'candidate changed during test; rerun stable source');
  report.state='PASS';
 }catch(error){report.error=error.message;throw error;}
 finally{
  releaseReceipt.resolve();
  if(browser)await browser.close();
  if(server&&server.exitCode===null){server.kill('SIGTERM');await until(()=>server.exitCode!==null||server.signalCode!==null);}
  fs.rmSync(fixture,{recursive:true,force:true});report.fixture_removed=!fs.existsSync(fixture);report.finished_at=new Date().toISOString();
  fs.writeFileSync(path.join(project,'evidence/P4/ui-connection-recovery.json'),JSON.stringify(report,null,2)+'\n');
 }
 console.log('PASS: real committed-message timeout retry is idempotent; read recovery preserves conversation and drafts.');
})().catch(error=>{console.error(error.message);process.exitCode=1;});
