// Run: node tests/ui_draft_storage_failure.cjs [--web-root candidate-directory]
// Fresh synthetic store on a private OS temporary filesystem; no production reads.
const {chromium}=require('../.runtime/browser/node_modules/playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const net=require('node:net');
const os=require('node:os');
const {createHash,randomBytes}=require('node:crypto');
const {spawn,execFileSync}=require('node:child_process');
const project=path.resolve(__dirname,'..');
const webArg=process.argv.indexOf('--web-root'),webRoot=path.resolve(project,webArg<0?'web':process.argv[webArg+1]);
const freePort=()=>new Promise(resolve=>{const s=net.createServer();s.listen(0,'127.0.0.1',()=>{const p=s.address().port;s.close(()=>resolve(p));});});
const until=async check=>{for(let i=0;i<150;i++){if(check())return;await new Promise(r=>setTimeout(r,100));}throw Error('isolated server readiness timeout');};
const sha256=file=>createHash('sha256').update(fs.readFileSync(file)).digest('hex');

(async()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'dsh-draft-storage-failure-'));
 const data=path.join(root,'data');
 const py=code=>execFileSync('python3',['-c',code,data],{cwd:project,encoding:'utf8'});
 const report={started_at:new Date().toISOString(),kind:'isolated_local_chromium_storage_fault_injection_not_real_phone',acceptance_id:'T07.draft',state:'FAIL',fixture_filesystem:'private OS temporary directory; project mounted volume cannot preserve private mode',checks:{},page_errors:[],production_accessed:false,real_phone_claimed:false,worker_enabled:false,model_and_notification_configured:false,source_sha256:sha256(path.join(webRoot,'app.js')),script_sha256:sha256(__filename)};
 let server,browser,serverLog='',messageRequests=0;
 try{
  const activity=py(`from pathlib import Path
import sys,json
from dsh_personal.server import Application
a=Application(Path(sys.argv[1]))
assert a.config()['model']=={} and a.config()['notification']=={}
print(a.store.ensure_activity('A3','2026-09-01')['id'])
`).trim();
  const durableState=()=>JSON.parse(py(`import sqlite3,sys,urllib.parse,json
from pathlib import Path
c=sqlite3.connect('file:'+urllib.parse.quote(str(Path(sys.argv[1])/'personal.sqlite'))+'?mode=ro',uri=True)
print(json.dumps({'total':c.execute('SELECT COUNT(*) FROM messages').fetchone()[0],'user':c.execute("SELECT COUNT(*) FROM messages WHERE role='user'").fetchone()[0]}))
`));
  const durableBefore=durableState();
  const port=await freePort();let gateway=await freePort();while(gateway===port)gateway=await freePort();
  const origin=`http://127.0.0.1:${port}`;
  server=spawn('python3',['-m','dsh_personal.server','--data',data,'--host','127.0.0.1','--port',String(port),'--gateway-port',String(gateway),'--no-worker','--web-root',webRoot],{cwd:project,stdio:['ignore','pipe','pipe']});
  server.stdout.on('data',b=>serverLog+=b);server.stderr.on('data',b=>serverLog+=b);
  await until(()=>{if(server.exitCode!==null)throw Error('isolated server exited');return serverLog.includes('listening on');});
  browser=await chromium.launch({headless:true,executablePath:(process.env.QIJU_CHROMIUM_PATH || chromium.executablePath()),args:['--no-sandbox']});
  const page=await browser.newPage({viewport:{width:390,height:844}});
  await page.route('**/*',route=>new URL(route.request().url()).origin===origin?route.continue():route.abort());
  page.on('pageerror',e=>report.page_errors.push(e.message));
  page.on('request',req=>{if(req.url()===origin+'/api/action'&&req.method()==='POST'&&req.postDataJSON()?.op==='message')messageRequests++;});
  await page.goto(origin+'/#activity/'+activity);
  const setup=page.locator('#account-setup-form');
  await setup.waitFor();
  const password='Fixture-'+randomBytes(18).toString('hex');
  await setup.locator('[name=username]').fill('ui-storage-test');
  await setup.locator('[name=password]').fill(password);
  await setup.locator('[name=confirm_password]').fill(password);
  await setup.getByRole('button',{name:'创建账号并进入'}).click();
  const input=page.locator('#message-input'),status=page.locator('#draft-status');
  await input.waitFor();
  const before='隔离测试：可保存的未发送草稿';
  const after='隔离测试：存储写入失败时仍保留输入，不发送';
  await input.fill(before);
  assert.equal(await status.innerText(),'未发送草稿 · 仅保存在此浏览器');
  assert.equal(await page.evaluate(id=>JSON.parse(localStorage.getItem('dsh-draft:'+id)).body,activity),before);
  await page.evaluate(id=>{
   const original=Storage.prototype.setItem;
   window.draftStorageFaults=0;
   Storage.prototype.setItem=function(key,value){
    if(this===localStorage&&key==='dsh-draft:'+id){window.draftStorageFaults++;throw new DOMException('Synthetic storage quota exceeded','QuotaExceededError');}
    return original.call(this,key,value);
   };
  },activity);
  await input.fill(after);
  const expected='本地草稿保存失败，请先复制文字；尚未发送。';
  await status.getByText(expected,{exact:true}).waitFor();
  assert.equal(await status.isVisible(),true);
  assert.equal(await status.evaluate(el=>el.classList.contains('error')),true);
  assert.equal(await input.inputValue(),after);
  assert.equal(await input.isEditable(),true);
  const storage=await page.evaluate(id=>({faults:window.draftStorageFaults,body:JSON.parse(localStorage.getItem('dsh-draft:'+id)).body}),activity);
  assert.ok(storage.faults>0);assert.equal(storage.body,before);
  const durableAfter=durableState();
  report.checks={local_account_setup_through_ui:true,baseline_storage_write_confirmed:true,quota_error_injected_count:storage.faults,explicit_failure_and_unsent_hint_visible:true,input_retained_exactly:true,input_still_editable:true,previous_saved_draft_unchanged:true,message_submission_request_count:messageRequests,durable_message_counts_before:durableBefore,durable_message_counts_after:durableAfter};
  assert.equal(messageRequests,0);assert.deepEqual(durableAfter,durableBefore);assert.equal(durableAfter.user,0);assert.deepEqual(report.page_errors,[]);
  report.observed_status=expected;
  report.limitations='Synthetic quota failure in isolated desktop Chromium with mobile viewport. Does not establish Android storage failure or refresh recovery when persistence fails; real-phone ordinary draft recovery is recorded separately.';
  assert.equal(sha256(path.join(webRoot,'app.js')),report.source_sha256,'UI source changed during test; rerun stable source');
  report.state='PASS';
 }catch(error){report.error=error.message;throw error;}
 finally{
  if(browser)await browser.close();
  if(server&&server.exitCode===null){server.kill('SIGTERM');await until(()=>server.exitCode!==null||server.signalCode!==null);}
  fs.rmSync(root,{recursive:true,force:true});
  report.fixture_removed=!fs.existsSync(root);report.finished_at=new Date().toISOString();
  fs.writeFileSync(path.join(project,'evidence/P4/chronicle-draft-storage-failure.json'),JSON.stringify(report,null,2)+'\n');
 }
 console.log('PASS: isolated storage failure visibly warns, retains input, and makes zero message submissions.');
})().catch(error=>{console.error(error.message);process.exitCode=1;});
