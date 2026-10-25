// Authenticated local app with synthetic receipts/tasks; no production or model.
const {chromium}=require('../.runtime/browser/node_modules/playwright');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),assert=require('node:assert/strict'),{spawn}=require('node:child_process'),{createHash}=require('node:crypto');
const root=fs.mkdtempSync(path.join(os.tmpdir(),'qiju-directive-ui-'));
const report={kind:'isolated_directive_receipt_browser',started_at:new Date().toISOString(),status:'RUNNING',viewport:{width:390,height:844},production_accessed:false,model_called:false,notifications_sent:false,checks:[],page_errors:[],unexpected_requests:[],source_sha256:Object.fromEntries(['web/app.js','web/style.css'].map(p=>[p,createHash('sha256').update(fs.readFileSync(p)).digest('hex')]))};
let server,browser;
const code=`import json,sys
from pathlib import Path
from dsh_personal.server import Application,create_server
from dsh_personal.task_requirements import apply
app=Application(Path(sys.argv[1]));a=app.store.ensure_activity('CHAT');stamp=app.store.stamp()
sources=[]
for i,body in enumerate(['请保存我的回复偏好。','请改这个任务。','请修改系统文件。']):
 sources.append(app.store.submit(a['id'],'directive-fixture-source-'+str(i),body)['receipt'])
with app.store.db(True) as c:
 for i,body in enumerate(['**已保存**：以后回复简洁。','**需要补充**：请指定要修改的任务。','**未执行**：不能通过聊天修改系统代码。\\n\\n<img src="https://invalid.example/pixel" onerror="window.receiptPwned=1"> [危险链接](javascript:alert(1))']):
  c.execute('INSERT INTO messages VALUES(?,?,NULL,?,?,?,?,NULL,NULL,0)',('directive-receipt:'+sources[i],a['id'],'assistant',body,stamp,'local_control'))
 c.execute('INSERT INTO messages VALUES(?,?,NULL,?,?,?,?,NULL,NULL,0)',('ordinary-local',a['id'],'assistant','普通活动回执。',stamp,'local_control'))
 c.execute('INSERT INTO messages VALUES(?,?,NULL,?,?,?,?,NULL,NULL,0)',('directive-receipt:ordinary-native',a['id'],'assistant','**普通模型回复**，即使文字说已保存也不是设置回执。',stamp,'native'))
t=app.store.create_task('旧目标',inputs='旧输入',deliverable='旧交付',acceptance='旧验收')
app.store.claim_task(t['id']);app.store.deliver_task(t['id'],1,'第一版旧成果');app.store.review_task(t['id'],1,'accept','旧成果验收通过')
for rev,patch in enumerate([{'goal':'新目标','inputs':'新输入材料','deliverable':'新的三例成果','acceptance':'每例标注出处'},{'inputs':'新输入材料\\n补充材料 <script>window.requirementPwned=1</script>'}]):
 with app.store.db(True) as c:apply(c,t['id'],rev,patch,'请保存我的回复偏好。',sources[0],stamp)
running=app.store.create_task('运行中的测试任务');app.store.claim_task(running['id']);app.store.deliver_task(running['id'],1,'运行任务旧成果');app.store.review_task(running['id'],1,'revise','补充一例');app.store.claim_task(running['id'])
profile=app.records.add_profile('每日一学先给完整示例，再提一个问题。',category='preference',scope='routine:A1',source_refs=['message:'+sources[0]],confirmed=True,user_input='合成界面测试规则')
s=create_server(app,port=0);print(json.dumps({'port':s.server_address[1],'id':a['id'],'sources':sources,'task':t['id'],'running':running['id'],'profile':profile.get('item_id') or profile.get('id')}),flush=True);s.serve_forever()
`;
function waitFor(test){return new Promise(async(resolve,reject)=>{for(let i=0;i<150;i++){try{if(test())return resolve();}catch(e){return reject(e);}await new Promise(r=>setTimeout(r,100));}reject(Error('fixture startup timed out'));});}
async function noOverflow(page,label){const result=await page.evaluate(()=>({viewport:innerWidth,document:document.documentElement.scrollWidth,body:document.body.scrollWidth}));assert.ok(result.document<=result.viewport+1&&result.body<=result.viewport+1,label+': '+JSON.stringify(result));}
(async()=>{
 let ready,err='';server=spawn('python3',['-c',code,path.join(root,'data')],{cwd:process.cwd(),stdio:['ignore','pipe','pipe']});server.stdout.on('data',b=>{try{ready=JSON.parse(b.toString().trim());}catch{}});server.stderr.on('data',b=>err+=b);await waitFor(()=>{if(server.exitCode!==null)throw Error(err);return !!ready;});const base='http://127.0.0.1:'+ready.port;
 browser=await chromium.launch({headless:true,executablePath:(process.env.QIJU_CHROMIUM_PATH || chromium.executablePath()),args:['--no-sandbox']});const context=await browser.newContext({viewport:report.viewport,isMobile:true,hasTouch:true});
 await context.route('**/*',async route=>{if(new URL(route.request().url()).origin!==base){report.unexpected_requests.push(route.request().url());await route.abort();}else await route.continue();});const page=await context.newPage();page.on('pageerror',e=>report.page_errors.push(e.message));
 await page.goto(base);await page.locator('#account-setup-form').waitFor();await page.getByLabel('账号',{exact:true}).fill('directive_fixture');await page.getByLabel('密码',{exact:true}).fill('synthetic-fixture-password');await page.getByLabel('再次输入密码',{exact:true}).fill('synthetic-fixture-password');await page.getByRole('button',{name:'创建账号并进入'}).click();await page.locator('#message-input').waitFor();
 await page.goto(base+'/#activity/'+ready.id);await page.locator('[data-directive-receipt]').first().waitFor();await page.evaluate(()=>{polling=true;stopEvents();});
 assert.equal(await page.locator('[data-directive-receipt]').count(),3);
 for(const [index,text] of ['已保存','需要补充','未执行'].entries()){
  const receipt=page.locator('[id="message-directive-receipt:'+ready.sources[index]+'"]');
  assert.equal(await receipt.locator('[data-directive-receipt]').innerText(),'设置处理回执');
  assert.equal(await receipt.locator('.message-body strong').first().innerText(),text);
  assert.equal(await receipt.locator('.message-meta .good').count(),0);
 }
 assert.equal(await page.locator('#message-ordinary-local [data-directive-receipt]').count(),0);
 assert.equal(await page.locator('[id="message-directive-receipt:ordinary-native"] [data-directive-receipt]').count(),0);
 assert.equal(await page.locator('.message.user [data-directive-receipt]').count(),0);
 assert.equal(await page.locator('.markdown-body img,.markdown-body script,.markdown-body [onerror],.markdown-body a[href^="javascript:"]').count(),0);
 assert.equal(await page.evaluate(()=>window.receiptPwned||0),0);
 await noOverflow(page,'receipts');
 report.checks.push('Success, clarification and refusal receipts receive the same neutral label; user messages, ordinary local responses and native model prose are not mislabeled. Markdown works and injected HTML/JavaScript stays inert.');
 await page.evaluate(()=>{window.__receiptNode=document.querySelector('[data-directive-receipt]').closest('.message');const range=document.createRange();range.selectNodeContents(__receiptNode.querySelector('.message-body'));getSelection().removeAllRanges();getSelection().addRange(range);window.__selectedReceipt=getSelection().toString();});
 const selected=await page.evaluate(()=>__selectedReceipt);assert.ok(selected.includes('已保存'));
 await page.evaluate(id=>updateActivity(id),ready.id);
 assert.ok(await page.evaluate(()=>__receiptNode===document.querySelector('[data-directive-receipt]').closest('.message')&&getSelection().toString()===__selectedReceipt));
 report.checks.push('An unchanged refresh reuses receipt nodes and preserves selected plain text for copying.');
 await page.goto(base+'/#tasks');await page.locator('[data-task-id="'+ready.task+'"]').waitFor();
 const task=page.locator('[data-task-id="'+ready.task+'"]');
 assert.ok((await task.locator('.card-top').innerText()).includes('需求版本 2'));
 assert.equal(await task.locator('[data-task-requirements]').getAttribute('open'),null);
 await task.locator('[data-task-requirements]>summary').click();
 const fields=await task.locator('[data-task-requirements]>.summary-list').innerText();
 for(const text of ['目标','新目标','输入材料','新输入材料','交付内容','新的三例成果','验收条件','每例标注出处'])assert.ok(fields.includes(text),text);
 await task.locator('[data-requirements-history]>summary').click();
 await task.locator('[data-requirements-revision="1"]>summary').click();
 assert.ok((await task.locator('[data-requirements-revision="1"]').innerText()).includes('旧目标'));
 assert.ok((await task.locator('[data-requirements-revision="1"]').innerText()).includes('新目标'));
 assert.equal(await task.locator('[data-requirements-revision="1"] [data-action="source-open"]').getAttribute('data-ref'),'message:'+ready.sources[0]);
 await task.locator('[data-requirements-revision="2"]>summary').click();
 assert.equal(await task.locator('script').count(),0);assert.equal(await page.evaluate(()=>window.requirementPwned||0),0);
 await task.locator('[data-task-delivery]>summary').click();
 assert.ok((await task.locator('[data-task-delivery]').innerText()).includes('成果版本 1'));
 assert.ok((await task.locator('[data-task-delivery]').innerText()).includes('此前成果仍保留'));
 assert.equal(await task.locator('.task-result').innerText(),'第一版旧成果');
 assert.equal(await task.locator('[data-review="accept"]').count(),0);
 const running=page.locator('[data-task-id="'+ready.running+'"]');await running.locator('[data-task-delivery]>summary').click();
 assert.ok((await running.locator('[data-task-delivery]').innerText()).includes('成果版本 1'));
 assert.ok(!(await running.locator('[data-task-delivery]').innerText()).includes('成果版本 2'));
 await noOverflow(page,'expanded task requirements/history');
 report.checks.push('Task requirement version 2, all four current fields and source-linked before/after revisions are inspectable. Earlier accepted version 1 stays labeled as an old artifact; an in-progress version 2 does not relabel the retained version 1 artifact. Expanded details fit 390px.');
 await page.goto(base+'/#data');await page.locator('[data-action="profile-edit"][data-id="'+ready.profile+'"]').click();
 // Use the modal-specific id because the import panel has another scope input.
 assert.equal(await page.locator('#profile-scope').getAttribute('list'),'profile-scope-options');
 assert.deepEqual(await page.locator('#profile-scope-options option').evaluateAll(rows=>rows.map(x=>x.value)),['global','routine:A1','routine:A2','routine:A3']);
 assert.equal(await page.locator('#profile-scope').inputValue(),'routine:A1');
 assert.ok((await page.locator('#profile-scope-help').innerText()).includes('在聊天中保存的专属规则'));
 assert.ok((await page.locator('#profile-scope-help').innerText()).includes('project:项目名'));
 await noOverflow(page,'profile modal');
 await page.locator('#profile-scope').fill('project:合成科研项目');
 await page.getByRole('button',{name:'保存此版本',exact:true}).click();await page.locator('#profile-form').waitFor({state:'hidden'});
 await page.waitForFunction(()=>document.querySelector('#page')?.textContent.includes('project:合成科研项目'));
 report.checks.push('Profile scope suggestions explain global and all three routine rules; existing scoped rules remain editable and a custom project scope can still be saved.');
 await page.goto(base+'/#activity/'+ready.id);await page.locator('#message-input').waitFor();
 await page.getByRole('button',{name:'打开侧栏',exact:true}).click();
 await page.getByRole('button',{name:'管理原话',exact:true}).click();
 const deletion=page.locator('[data-action="message-delete"][data-id="'+ready.sources[0]+'"]');await deletion.click();await page.locator('#delete-form').waitFor();
 assert.equal(await page.locator('#delete-form input[name="record_id"]').inputValue(),ready.sources[0]);
 assert.ok(await page.locator('#delete-form input[name="confirm"]').isVisible());
 report.checks.push('The original message remains accessible through the existing explicit deletion-plan UI; this presentation test does not perform deletion.');
 assert.deepEqual(report.page_errors,[]);assert.deepEqual(report.unexpected_requests,[]);report.status='PASS';
})().catch(e=>{report.status='FAIL';report.error=e.stack||String(e);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();if(server&&server.exitCode===null){server.kill('SIGTERM');await waitFor(()=>server.exitCode!==null||server.signalCode!==null).catch(()=>{});}fs.rmSync(root,{recursive:true,force:true});report.fixture_removed=!fs.existsSync(root);report.finished_at=new Date().toISOString();fs.writeFileSync('evidence/P4/directive-receipt-browser.json',JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report,null,2));});
