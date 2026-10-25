// Targeted deletion UI integration. Creates and destroys its own synthetic data;
// never connects to production, an existing QA store, a model, or a device.
const {chromium}=require('../.runtime/browser/node_modules/playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const net=require('node:net');
const {spawn,execFileSync}=require('node:child_process');
const freePort=()=>new Promise(resolve=>{const s=net.createServer();s.listen(0,'127.0.0.1',()=>{const p=s.address().port;s.close(()=>resolve(p));});});
const until=async(fn)=>{for(let i=0;i<150;i++){if(await fn())return;await new Promise(r=>setTimeout(r,100));}throw Error('fixture readiness timeout');};

(async()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'dsh-ui-deletion-'));
 const py=(code,args=[])=>execFileSync('python3',['-c',code,root,...args],{encoding:'utf8'});
 const report={at:new Date().toISOString(),scope:'synthetic isolated browser and actual local API; no model/phone/offsite claims',checks:{},page_errors:[]};
 let browser,server,reader,serverLog='';
 try{
  const fixture=JSON.parse(py(`
from pathlib import Path
import sys,json,uuid
from dsh_personal.server import Application
from dsh_personal.operations import create_key
root=Path(sys.argv[1]);app=Application(root/'data');s=app.store
a=s.ensure_activity('A3','2026-09-01');other=s.ensure_activity('A2','2026-09-02')
target='DELETE_UI_SYNTHETIC_SOURCE_9ab';keep='Independent original also mentions '+target
mid=s.submit(a['id'],'delete-ui-target',target)['receipt']
keepid=s.submit(a['id'],'delete-ui-keep',keep)['receipt']
sids=[]
for aid in [a['id'],other['id']]:
 sid=str(uuid.uuid4());sids.append(sid);folder=app.data/'dsh/sessions/--fixture--'/sid;folder.mkdir(parents=True)
 (folder/'session.v4.jsonl').write_text(json.dumps({'type':'session','version':4,'id':sid})+'\\n'+json.dumps({'fixture':target})+'\\n')
 with s.db(True) as c:c.execute("UPDATE activities SET session_id=?,session_state='ready' WHERE id=?",(sid,aid))
bad=app.data/'dsh/sessions/--fixture--'/sids[0]/'unknown-copy.txt';bad.write_text(target)
with s.db(True) as c:c.execute("INSERT INTO messages VALUES('fixture-derived',?,NULL,'assistant',?,?,'native','fixture-native',NULL,0)",(a['id'],'Derived '+target,s.stamp()))
app.records.add_profile('Derived fixture profile',source_refs=['message:'+mid],confirmed=True,user_input='Synthetic fixture confirmation')
s.create_task('Synthetic independent explicit task',owner='user',source_activity_id=a['id'])
create_key(root/'key')
print(json.dumps({'activity':a['id'],'message':mid,'keep':keepid,'raw':target,'keep_body':keep,'bad':str(bad),'native_count':len(sids)}))
`));
  const port=await freePort(),gateway=await freePort(),url=`http://127.0.0.1:${port}`;
  server=spawn('python3',['-m','dsh_personal.server','--data',path.join(root,'data'),'--port',String(port),'--gateway-port',String(gateway),'--no-worker'],{stdio:['ignore','pipe','pipe']});
  server.stdout.on('data',b=>serverLog+=b);server.stderr.on('data',b=>serverLog+=b);
  await until(()=>{if(server.exitCode!==null)throw Error('isolated server exited');return serverLog.includes('listening on');});
  browser=await chromium.launch({headless:true,executablePath:(process.env.QIJU_CHROMIUM_PATH || chromium.executablePath()),args:['--no-sandbox']});
  const page=await browser.newPage({viewport:{width:1280,height:1000}});
  page.on('pageerror',e=>report.page_errors.push(e.message));
  const requests=[];
  page.on('request',req=>{if(req.url().endsWith('/api/action')){try{const p=req.postDataJSON();if(['delete_message','import_remove'].includes(p.op))requests.push(p);}catch{}}});
  await page.goto(url+'/#activity/'+fixture.activity);
  await page.getByLabel('访问密钥').fill(fs.readFileSync(path.join(root,'data/access-token'),'utf8').trim());
  await page.getByRole('button',{name:'打开起居注'}).click();
  await page.locator(`[data-action="message-delete"][data-id="${fixture.message}"]`).click();
  await page.locator('#delete-form').waitFor();
  assert.equal(await page.locator('[data-delete-count="activity_ids"]').innerText(),'2');
  assert.equal(await page.locator('[data-delete-count="task_ids"]').innerText(),'1');
  assert.equal(await page.locator('[data-delete-count="native_session_ids"]').innerText(),'2');
  assert.ok((await page.locator('#deletion-legacy').innerText()).includes('2 个'));
  assert.ok((await page.locator('#modal').innerText()).includes('即使含相同文字也保留'));
  assert.ok(!(await page.locator('#modal').innerText()).includes(fixture.raw));
  const originalToken=await page.locator('[name="plan_token"]').inputValue();assert.ok(originalToken);
  report.checks.plan_counts_legacy_scope_without_body=true;

  // Change a dependency after preview. The real server must reject, and UI must
  // require a freshly fetched plan plus a new deliberate checkbox confirmation.
  const mutation=await page.request.post(url+'/api/action',{headers:{'X-DSH-Client':'web','Origin':url},data:{op:'message',activity_id:fixture.activity,submission_id:'delete-ui-after-preview',body:'Independent input arriving after plan preview.'}});
  assert.equal(mutation.status(),200);
  await page.locator('[name="confirm"]').check();
  await page.locator('#delete-form button[type="submit"]').click();
  await page.locator('#deletion-error').getByText(/关联记录已经变化/).waitFor();
  assert.equal(await page.locator('[name="plan_token"]').inputValue(),'');
  assert.equal(await page.locator('[name="confirm"]').isChecked(),false);
  assert.equal(await page.locator('#delete-form button[type="submit"]').isDisabled(),true);
  const retained=await (await page.request.get(url+'/api/activity?id='+fixture.activity)).json();
  assert.ok(retained.messages.some(x=>x.id===fixture.message));
  await page.locator('[data-action="deletion-refresh"]').click();
  await page.waitForFunction(old=>document.querySelector('[name="plan_token"]')?.value&&document.querySelector('[name="plan_token"]').value!==old,originalToken);
  assert.equal(await page.locator('[name="confirm"]').isChecked(),false);
  report.checks.changed_plan_requires_reconfirmation=true;

  // A real SQLite read transaction prevents physical compaction; an unknown
  // native file prevents namespace cleanup. Neither is silently called done.
  let readerReady=false;
  reader=spawn('python3',['-u','-c',`import sqlite3,sys\nc=sqlite3.connect(sys.argv[1]);c.execute('BEGIN');c.execute('SELECT * FROM messages').fetchall();print('ready',flush=True);sys.stdin.readline();c.close()`,path.join(root,'data/personal.sqlite')],{stdio:['pipe','pipe','pipe']});
  reader.stdout.on('data',()=>readerReady=true);await until(()=>readerReady);
  await page.locator('[name="confirm"]').check();
  await page.locator('#delete-form button[type="submit"]').click();
  await page.getByRole('heading',{name:'删除处理结果'}).waitFor();
  const partial=await page.locator('#modal').innerText();
  assert.ok(partial.includes('待完成；相关会话保持隔离'));
  assert.ok(partial.includes('数据库空间清理待完成'));
  assert.ok(partial.includes('尚未完成加密保护，需要继续处理')||partial.includes('可能尚未配置备份密钥'));
  assert.ok(!partial.includes('完全删除'));
  const applied=await (await page.request.get(url+'/api/activity?id='+fixture.activity)).json();
  assert.ok(!applied.messages.some(x=>x.id===fixture.message||x.id==='fixture-derived'));
  assert.ok(applied.messages.some(x=>x.id===fixture.keep&&x.body===fixture.keep_body));
  assert.ok(requests.every(p=>typeof p.plan_token==='string'&&p.plan_token.length>0));
  report.checks.partial_actual_native_sqlite_and_unconfigured_ledger_visible=true;
  report.checks.independent_same_text_original_retained_derived_reply_invalidated=true;
  await page.locator('#message-'+fixture.keep).getByText('已保存，未重放',{exact:true}).waitFor();
  assert.ok((await page.locator('#message-'+fixture.keep).innerText()).includes('如需执行请明确重新发送'));
  report.checks.retained_pending_original_shows_no_automatic_replay=true;
  await page.reload();
  await page.locator('.deletion-pending').waitFor();
  const saved=await page.evaluate(()=>localStorage.getItem('dsh-deletion-pending'));
  assert.ok(saved.includes(fixture.message));assert.ok(!saved.includes(fixture.raw));assert.ok(!saved.includes(originalToken));
  report.checks.refresh_preserves_metadata_only_retry=true;
  fs.unlinkSync(fixture.bad);
  await page.locator('.deletion-pending [data-action="deletion-retry"]').click();
  await page.locator('#delete-form').waitFor();
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
  await page.screenshot({path:'evidence/P1/ui-deletion-mobile.png',fullPage:true});
  await page.locator('[name="confirm"]').check();
  await page.locator('#delete-form button[type="submit"]').click();
  await page.getByRole('heading',{name:'删除处理结果'}).waitFor();
  const cachedPartial=await page.locator('#modal').innerText();
  assert.ok(cachedPartial.includes('当前原生会话副本已逻辑清理'));
  assert.ok(cachedPartial.includes('数据库空间清理待完成'));
  assert.ok(cachedPartial.includes('尚未完成加密保护，需要继续处理')||cachedPartial.includes('可能尚未配置备份密钥'));
  // Native cleanup is now cached as complete. The next retry must still compact
  // the released database and publish the newly configured encrypted ledger.
  reader.stdin.end('\n');await until(()=>reader.exitCode!==null);reader=null;
  py(`from pathlib import Path\nimport json,sys\nr=Path(sys.argv[1]);p=r/'data/config.json';c=json.loads(p.read_text());c['backup']['key_file']=str(r/'key');p.write_text(json.dumps(c))`);
  await page.locator('#modal [data-action="deletion-retry"]').click();
  await page.locator('#delete-form').waitFor();
  await page.locator('[name="confirm"]').check();
  await page.locator('#delete-form button[type="submit"]').click();
  await page.getByRole('heading',{name:'删除处理结果'}).waitFor();
  const completed=await page.locator('#modal').innerText();
  assert.ok(completed.includes('当前原生会话副本已逻辑清理'));
  assert.ok(completed.includes('本地数据库清理已完成'));
  assert.ok(completed.includes('最新删除账本已加密保护'));
  assert.ok(completed.includes('这不表示已有离线导出或所有副本均已销毁'));
  assert.equal(await page.locator('#modal [data-action="deletion-retry"]').count(),0);
  assert.deepEqual(JSON.parse(await page.evaluate(()=>localStorage.getItem('dsh-deletion-pending'))),[]);
  report.checks.retry_resumes_cleanup_and_protects_latest_ledger=true;
  report.checks.cached_native_receipt_still_retries_sqlite_and_ledger=true;
  report.checks.mobile_no_horizontal_overflow=true;

  // Import removal uses the same confirmation and status UI, but its identity
  // is a batch and duplicates predating that batch must remain available.
  const imported=JSON.parse(py(`
from pathlib import Path
import sys,json,uuid
from dsh_personal.server import Application
from dsh_personal.deletion import register_context
r=Path(sys.argv[1]);app=Application(r/'data');s=app.store
old={'id':'existing-import','role':'user','content':'KEEP_PRIOR_IMPORT','created_at':'2026-09-03T10:00:00+08:00'}
new={'id':'new-import','role':'user','content':'DELETE_IMPORTED_FIXTURE','created_at':'2026-09-03T10:01:00+08:00'}
app.records.import_apply('kimi',json.dumps({'id':'ui-import','messages':[old]}),'earlier.json')
batch=app.records.import_apply('kimi',json.dumps({'id':'ui-import','messages':[old,new]}),'remove-this.json')
mid=next(m['message_id'] for m in app.records.imported_messages() if m['source_id']=='new-import')
profile=app.records.add_profile('Imported derivative profile',source_refs=['import:'+mid],confirmed=True,user_input='Synthetic import confirmation')
a=s.ensure_activity('A1','2026-09-03');keep=s.submit(a['id'],'import-keep-user','Independent local input')['receipt'];sid=str(uuid.uuid4())
folder=app.data/'dsh/sessions/--fixture--'/sid;folder.mkdir(parents=True)
(folder/'session.v4.jsonl').write_text(json.dumps({'type':'session','version':4,'id':sid})+'\\n'+json.dumps({'fixture':'DELETE_IMPORTED_FIXTURE'})+'\\n')
bad=folder/'unknown-copy.txt';bad.write_text('DELETE_IMPORTED_FIXTURE')
with s.db(True) as c:
 c.execute("UPDATE activities SET session_id=?,session_state='ready' WHERE id=?",(sid,a['id']))
 c.execute("INSERT INTO messages VALUES('import-derived',?,NULL,'assistant','Derived import answer',?,'native','import-fixture-native',NULL,0)",(a['id'],s.stamp()))
register_context(s,sid,['profile:'+profile['item_id']])
s.create_task('Synthetic task linked to imported context',owner='user',source_activity_id=a['id'])
print(json.dumps({'batch':batch['batch_id'],'activity':a['id'],'keep':keep,'bad':str(bad)}))
`));
  await page.locator('#modal [data-action="modal-close"]').last().click();
  await page.goto(url+'/#data');
  await page.locator(`[data-action="import-remove"][data-id="${imported.batch}"]`).click();
  await page.locator('#delete-form').waitFor();
  assert.equal(await page.locator('[name="target_kind"]').inputValue(),'import');
  assert.equal(await page.locator('[data-delete-count="activity_ids"]').innerText(),'1');
  assert.equal(await page.locator('[data-delete-count="task_ids"]').innerText(),'1');
  assert.equal(await page.locator('[data-delete-count="native_session_ids"]').innerText(),'1');
  assert.ok((await page.locator('#modal').innerText()).includes('导入前已存在的相同内容保留'));
  const importToken=await page.locator('[name="plan_token"]').inputValue();
  await page.request.post(url+'/api/action',{headers:{'X-DSH-Client':'web','Origin':url},data:{op:'message',activity_id:imported.activity,submission_id:'import-after-preview',body:'Independent local later input'}});
  await page.locator('[name="confirm"]').check();
  await page.locator('#delete-form button[type="submit"]').click();
  await page.locator('#deletion-error').getByText(/重新核对/).waitFor();
  assert.equal(await page.locator('#delete-form button[type="submit"]').isDisabled(),true);
  await page.locator('[data-action="deletion-refresh"]').click();
  await page.waitForFunction(old=>document.querySelector('[name="plan_token"]')?.value&&document.querySelector('[name="plan_token"]').value!==old,importToken);
  await page.locator('[name="confirm"]').check();
  await page.locator('#delete-form button[type="submit"]').click();
  await page.getByRole('heading',{name:'删除处理结果'}).waitFor();
  assert.ok((await page.locator('#modal').innerText()).includes('待完成；相关会话保持隔离'));
  const importState=await (await page.request.get(url+'/api/state?journal_mode=all')).json();
  assert.ok(importState.journal.some(m=>m.body==='KEEP_PRIOR_IMPORT'));
  assert.ok(!importState.journal.some(m=>m.body==='DELETE_IMPORTED_FIXTURE'));
  const importActivity=await (await page.request.get(url+'/api/activity?id='+imported.activity)).json();
  assert.ok(importActivity.messages.some(m=>m.id===imported.keep));
  assert.ok(!importActivity.messages.some(m=>m.id==='import-derived'));
  report.checks.import_plan_change_and_partial_cleanup_truthful=true;
  report.checks.import_prior_duplicate_and_independent_user_preserved=true;
  await page.reload();
  await page.locator(`.deletion-pending [data-id="${imported.batch}"]`).waitFor();
  fs.unlinkSync(imported.bad);
  await page.locator(`.deletion-pending [data-id="${imported.batch}"]`).click();
  await page.locator('#delete-form').waitFor();
  await page.locator('[name="confirm"]').check();
  await page.locator('#delete-form button[type="submit"]').click();
  await page.getByRole('heading',{name:'删除处理结果'}).waitFor();
  const importComplete=await page.locator('#modal').innerText();
  assert.ok(importComplete.includes('当前原生会话副本已逻辑清理'));
  assert.ok(importComplete.includes('本地数据库清理已完成'));
  assert.ok(importComplete.includes('最新删除账本已加密保护'));
  assert.deepEqual(JSON.parse(await page.evaluate(()=>localStorage.getItem('dsh-deletion-pending'))),[]);
  assert.ok(requests.filter(p=>p.op==='import_remove').every(p=>p.batch_id===imported.batch&&p.plan_token));
  report.checks.import_refresh_and_removed_batch_cleanup_retry=true;
  assert.deepEqual(report.page_errors,[]);
  report.status='PASS';
 }catch(error){report.status='FAIL';report.error=String(error.stack||error);process.exitCode=1;}
 finally{
  if(reader)reader.kill('SIGTERM');
  if(browser)await browser.close();
  if(server){server.kill('SIGTERM');await until(()=>server.exitCode!==null||server.signalCode!==null).catch(()=>server.kill('SIGKILL'));}
  fs.rmSync(root,{recursive:true,force:true});
  fs.writeFileSync('evidence/P1/ui-deletion.json',JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify(report,null,2));
 }
})();
