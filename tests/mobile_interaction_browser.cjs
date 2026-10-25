// Isolated mobile regression. Run with project-local Playwright already installed.
const {chromium}=require('../.runtime/browser/node_modules/playwright');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),assert=require('node:assert/strict'),{spawn}=require('node:child_process'),{createHash}=require('node:crypto');
const baseline=process.argv.includes('--baseline'),root=fs.mkdtempSync(path.join(os.tmpdir(),'qiju-mobile-'));
const report={kind:'isolated_mobile_interaction_browser',phase:baseline?'before':'after',started_at:new Date().toISOString(),status:'RUNNING',viewport:{width:390,height:844},production_accessed:false,model_called:false,notifications_sent:false,checks:[],page_errors:[],unexpected_requests:[],app_js_sha256:createHash('sha256').update(fs.readFileSync('web/app.js')).digest('hex')};
let server,browser;
const serverCode=`import json,sys
from pathlib import Path
from dsh_personal.server import Application,create_server
app=Application(Path(sys.argv[1]));a=app.store.ensure_activity('CHAT');b=app.store.ensure_activity('A2');app.records.add_profile('Synthetic active preference',confirmed=True,user_input='fixture')
with app.store.db(True) as c:
 c.execute('DELETE FROM messages WHERE activity_id=?',(a['id'],))
 for i in range(80):
  body=("# Synthetic message %s\\n\\nIt's **readable Markdown**.\\n\\n| A | B |\\n|---|---|\\n| a | b |\\n\\n\x60\x60\x60python\\nprint('fixture')\\n\x60\x60\x60\\n"%i) if i!=1 else 'Synthetic expandable tool result'
  c.execute('INSERT INTO messages VALUES(?,?,NULL,?,?,?,?,?,NULL,0)',('fixture-'+str(i),a['id'],'tool' if i==1 else 'assistant',body,app.store.stamp(),'native',str(i)))
s=create_server(app,port=0);print(json.dumps({'port':s.server_address[1],'id':a['id'],'second':b['id']}),flush=True);s.serve_forever()
`;
function waitFor(test){return new Promise(async(resolve,reject)=>{for(let i=0;i<150;i++){try{if(test())return resolve();}catch(e){return reject(e);}await new Promise(r=>setTimeout(r,100));}reject(Error('fixture startup timed out'));});}
(async()=>{
 let ready,err='';server=spawn('python3',['-c',serverCode,path.join(root,'data')],{cwd:process.cwd(),stdio:['ignore','pipe','pipe']});server.stdout.on('data',b=>{try{ready=JSON.parse(b.toString().trim());}catch{}});server.stderr.on('data',b=>err+=b);await waitFor(()=>{if(server.exitCode!==null)throw Error(err);return !!ready;});
 const base='http://127.0.0.1:'+ready.port;
 browser=await chromium.launch({headless:true,executablePath:(process.env.QIJU_CHROMIUM_PATH || chromium.executablePath()),args:['--no-sandbox']});
 const context=await browser.newContext({viewport:report.viewport,isMobile:true,hasTouch:true});
 await context.route('**/*',async route=>{if(new URL(route.request().url()).origin!==base){report.unexpected_requests.push(route.request().url());await route.abort();}else await route.continue();});
 const page=await context.newPage();page.on('pageerror',e=>report.page_errors.push(e.message));
 await page.goto(base);await page.locator('#account-setup-form').waitFor();await page.getByLabel('账号',{exact:true}).fill('mobile_fixture');await page.getByLabel('密码',{exact:true}).fill('synthetic-fixture-password');await page.getByLabel('再次输入密码',{exact:true}).fill('synthetic-fixture-password');await page.getByRole('button',{name:'创建账号并进入'}).click();await page.locator('#message-input').waitFor();
 await page.goto(base+'/#activity/'+ready.id);await page.locator('#message-fixture-79').waitFor();
 const before=await context.request.get(base+'/api/activity?id='+ready.id).then(r=>r.json());
 await page.evaluate(()=>{polling=true;stopEvents();window.__first=document.querySelector('.message');window.__tool=document.querySelector('.message.tool details');__tool.open=true;window.__renderedMessages=0;const original=messagesHTML;messagesHTML=function(rows){__renderedMessages+=rows.length;return original(rows);};window.__mutations=0;window.__observer=new MutationObserver(rows=>__mutations+=rows.filter(x=>x.type==='childList').length);__observer.observe(document.querySelector('#messages'),{childList:true,subtree:true});document.querySelector('#messages').scrollTop=80;});
 await page.locator('#message-input').fill('手机草稿与中文输入保留');
 await page.evaluate(()=>{const input=document.querySelector('#message-input');input.setSelectionRange(2,5);window.__input=input;});
 for(let i=0;i<3;i++)await page.evaluate(id=>updateActivity(id),ready.id);
 report.unchanged_refresh=await page.evaluate(()=>({refreshes:3,message_count:document.querySelectorAll('.message').length,rendered_message_count:__renderedMessages,child_list_mutations:__mutations,first_node_preserved:__first===document.querySelector('.message'),tool_still_open:document.querySelector('.message.tool details').open,input_node_preserved:__input===document.querySelector('#message-input'),input_value:document.querySelector('#message-input').value,selection_start:document.querySelector('#message-input').selectionStart}));
 if(baseline){report.status='OBSERVED';return;}
 assert.equal(report.unchanged_refresh.rendered_message_count,0);assert.equal(report.unchanged_refresh.child_list_mutations,0);assert.ok(report.unchanged_refresh.first_node_preserved);assert.ok(report.unchanged_refresh.tool_still_open);assert.ok(report.unchanged_refresh.input_node_preserved);assert.equal(report.unchanged_refresh.selection_start,2);
 report.checks.push('Three unchanged activity refreshes render zero messages and cause zero message-tree mutations; open tool details, input node and selection are retained.');
 let payload=structuredClone(before);
 await page.route('**/api/activity?id='+ready.id,route=>route.fulfill({json:payload}));
 await page.evaluate(()=>{const area=document.querySelector('#messages');area.scrollTop=80;window.__readingTop=area.scrollTop;window.__renderedMessages=0;const r=document.createRange();r.selectNodeContents(document.querySelector('#message-fixture-0 h1'));getSelection().removeAllRanges();getSelection().addRange(r);});
 payload.messages.push({id:'fixture-new',role:'assistant',body:'## 新增回复\n\n**增量渲染** <script>window.__injected=1</script>',received_at:before.messages[0].received_at,processing_state:'native'});
 await page.evaluate(id=>updateActivity(id),ready.id);
 report.append_refresh=await page.evaluate(()=>({rendered_message_count:__renderedMessages,first_node_preserved:__first===document.querySelector('.message'),tool_still_open:document.querySelector('.message.tool details').open,scroll_delta:document.querySelector('#messages').scrollTop-__readingTop,selected_text:getSelection().toString()}));
 assert.equal(report.append_refresh.rendered_message_count,1);assert.ok(report.append_refresh.first_node_preserved);assert.ok(report.append_refresh.tool_still_open);assert.ok(Math.abs(report.append_refresh.scroll_delta)<=1);assert.equal(report.append_refresh.selected_text,'Synthetic message 0');assert.equal(await page.evaluate(()=>window.__injected),undefined);assert.equal(await page.locator('#message-fixture-new .message-body strong').innerText(),'增量渲染');
 report.checks.push('One appended reply renders only that reply; existing Markdown nodes, reading position, selected text and expanded tool details survive. Markdown remains escaped.');
 payload.messages=payload.messages.filter(m=>m.id!=='fixture-20');payload.messages.find(m=>m.id==='fixture-30').body='**更正后的内容**';await page.evaluate(id=>updateActivity(id),ready.id);assert.equal(await page.locator('#message-fixture-20').count(),0);assert.equal(await page.locator('#message-fixture-30 strong').last().innerText(),'更正后的内容');assert.ok(await page.evaluate(()=>__first===document.querySelector('.message')));
 report.checks.push('Changed and deleted messages update immediately without recreating unrelated messages.');
 await page.evaluate(()=>{const a=document.querySelector('#messages');a.scrollTop=a.scrollHeight;});payload.messages.push({id:'fixture-new-2',role:'assistant',body:'跟随最新回复',received_at:before.messages[0].received_at,processing_state:'native'});await page.evaluate(id=>updateActivity(id),ready.id);assert.ok(await page.evaluate(()=>{const a=document.querySelector('#messages');return a.scrollHeight-a.scrollTop-a.clientHeight<2;}));
 report.checks.push('Readers already at the bottom follow a new reply; readers above it are not forced down.');
 await page.unroute('**/api/activity?id='+ready.id);await page.reload();await page.locator('#message-input').waitFor();assert.equal(await page.locator('#message-input').inputValue(),'手机草稿与中文输入保留');assert.ok((await page.locator('#draft-status').innerText()).includes('未发送草稿'));
 report.checks.push('Refresh restores the unsent Chinese draft from durable local storage.');
 let statePending=false,parallel=false;await page.route('**/api/state?*',async route=>{statePending=true;await new Promise(r=>setTimeout(r,150));await route.continue();statePending=false;});page.on('request',request=>{if(request.url()===base+'/api/activity?id='+ready.second&&statePending)parallel=true;});await page.goto(base+'/#activity/'+ready.second);await page.locator('#message-input').waitFor();await page.waitForFunction(id=>currentActivity?.activity.id===id,ready.second);assert.ok(parallel);report.checks.push('Navigation starts the activity read while the state request is pending, avoiding a serial network round trip.');
 await page.unroute('**/api/state?*');
 await page.getByRole('button',{name:'打开侧栏',exact:true}).click();await page.getByRole('button',{name:'活动信息',exact:true}).click();await page.getByRole('button',{name:'重试待处理步骤',exact:true}).waitFor({state:'visible'});
 await page.getByRole('button',{name:'关联委托',exact:true}).click();await page.locator('#task-form').waitFor();assert.equal(await page.locator('#task-form [name="source_activity_id"]').inputValue(),ready.second);await page.getByRole('button',{name:'关闭',exact:true}).click();
 await page.goto(base+'/#data');await page.getByText('Synthetic active preference',{exact:true}).waitFor();assert.equal(await page.locator('#import-scope').inputValue(),'imported');
 await page.goto(base+'/#journal');await page.locator('.chronicle-cover').waitFor();
 await page.goto(base+'/#calendar');await page.locator('[data-action="calendar-view"][data-view="week"]').click();await page.locator('.calendar-week-scroll').waitFor();assert.equal(await page.locator('.calendar-hour-label').count(),25);
 report.checks.push('Fresh full-state navigation retains profiles/import scope, personal-history view and the 00:00–24:00 week calendar; lightweight chat retains activity information and the correctly linked task form.');
 await page.goto(base+'/#activity/'+ready.id);await page.locator('#message-fixture-79').waitFor();
 // Deliberately deliver an obsolete activity response after A → B → A.
 let releaseOld,oldSeen;const oldGate=new Promise(r=>releaseOld=r),oldStarted=new Promise(r=>oldSeen=r);let interceptOld=true;
 await page.route('**/api/activity?id='+ready.id,async route=>{if(!interceptOld)return route.continue();interceptOld=false;const response=await route.fetch(),old=await response.json();old.messages.push({id:'stale-poll',role:'assistant',body:'obsolete response',received_at:old.messages[0].received_at});oldSeen();await oldGate;await route.fulfill({json:old});});
 await page.evaluate(()=>{window.__oldRead=updateActivity(routeId);});await oldStarted;
 await page.goto(base+'/#activity/'+ready.second);await page.waitForFunction(id=>currentActivity?.activity.id===id,ready.second);
 await page.goto(base+'/#activity/'+ready.id);await page.locator('#message-fixture-79').waitFor();releaseOld();await page.evaluate(()=>window.__oldRead);assert.equal(await page.locator('#message-stale-poll').count(),0);
 report.checks.push('An obsolete activity poll delivered after A → B → A cannot restore stale message data.');
 await page.unroute('**/api/activity?id='+ready.id);
 // A slow navigation must retain a newer update that has already removed data.
 const removed=structuredClone(before);removed.messages=removed.messages.filter(x=>x.id!=='fixture-20');let releaseSlow,slowHeld=false,activityCalls=0;const slowGate=new Promise(r=>releaseSlow=r);
 await page.route('**/api/state?*',async route=>{slowHeld=true;await slowGate;await route.continue();});
 await page.route('**/api/activity?id='+ready.id,async route=>{activityCalls++;await route.fulfill({json:activityCalls===1?before:removed});});
 await page.evaluate(()=>{window.__slowNavigation=loadRoute();});await waitFor(()=>slowHeld&&activityCalls===1);await page.evaluate(id=>updateActivity(id),ready.id);assert.equal(await page.locator('#message-fixture-20').count(),0);releaseSlow();await page.evaluate(()=>__slowNavigation);assert.equal(await page.locator('#message-fixture-20').count(),0);
 await page.unroute('**/api/state?*');await page.unroute('**/api/activity?id='+ready.id);
 report.checks.push('A slow state read finishing after a newer activity update cannot restore a removed message through the older navigation response.');
 // Display of explicitly unlimited background accounting never invents a cap.
 await page.evaluate(()=>{state.budget={background_limit_enforced:false,daily_limit:null,monthly_limit:null,today:state.today,usage:[{day:state.today,month:state.today.slice(0,7),kind:'background',settled:1234,reserved:56}]};renderSettings();});assert.ok((await page.locator('#page').innerText()).includes('未设本地限额'));assert.equal(await page.locator('#page progress').count(),0);
 report.checks.push('Unlimited local background accounting is labeled explicitly and renders no fallback limit progress bar.');
 // A successful response from before login must not reopen protected UI.
 let releaseState,stateSeen;const stateGate=new Promise(r=>releaseState=r),stateStarted=new Promise(r=>stateSeen=r);let interceptState=true;
 await page.route('**/api/state?*',async route=>{if(!interceptState)return route.continue();interceptState=false;const response=await route.fetch(),old=await response.json();stateSeen();await stateGate;await route.fulfill({json:old});});
 await page.evaluate(()=>{window.__oldRoute=loadRoute();});await stateStarted;await page.evaluate(()=>renderLogin());await page.locator('#login-form').waitFor();releaseState();await page.evaluate(()=>window.__oldRoute);assert.equal(await page.locator('.app-shell').count(),0);assert.ok(await page.evaluate(()=>state===null&&currentActivity===null));
 report.checks.push('Login invalidates pending route/state/activity reads; a late successful response cannot revive the protected interface.');
 assert.deepEqual(report.page_errors,[]);assert.deepEqual(report.unexpected_requests,[]);report.status='PASS';
})().catch(e=>{report.status='FAIL';report.error=e.stack||String(e);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();if(server&&server.exitCode===null){server.kill('SIGTERM');await waitFor(()=>server.exitCode!==null||server.signalCode!==null).catch(()=>{});}fs.rmSync(root,{recursive:true,force:true});report.fixture_removed=!fs.existsSync(root);report.finished_at=new Date().toISOString();const target='evidence/P4/mobile-interaction-'+(baseline?'before':'after')+'.json';fs.writeFileSync(target,JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report,null,2));});
