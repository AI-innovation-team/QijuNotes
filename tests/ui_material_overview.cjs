// Synthetic imported materials only. No production database, model, or notification access.
const {chromium}=require('../.runtime/browser/node_modules/playwright');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {spawn,spawnSync}=require('node:child_process'),assert=require('node:assert/strict');
const {createHash}=require('node:crypto');
const project=process.cwd(),root=fs.mkdtempSync(path.join(os.tmpdir(),'dsh-material-overview-ui-'));
const data=path.join(root,'data'),web=path.join(root,'web'),arg=process.argv.indexOf('--web-root');
fs.cpSync(path.resolve(project,arg<0?'web':process.argv[arg+1]),web,{recursive:true});
const stamp=new Date().toISOString().replace(/[:.]/g,'-');
const output=path.join(project,'evidence/P4/material-overview-ui',stamp);
const reportPath=path.join(project,'evidence/P4/material-overview-ui.json');
fs.mkdirSync(output,{recursive:true});
if(fs.existsSync(reportPath))fs.copyFileSync(reportPath,path.join(output,'previous-report.json'));
const report={kind:'isolated_synthetic_material_overview_browser',started_at:new Date().toISOString(),state:'RUNNING',production_accessed:false,model_called:false,notifications_sent:false,checks:[],viewports:[],screenshots:[],page_errors:[],external_requests:[],assets:{}};
for(const file of fs.readdirSync(web).filter(f=>/\.(js|css)$/.test(f)))report.assets[file]=createHash('sha256').update(fs.readFileSync(path.join(web,file))).digest('hex');
const fixture={
  user:'我曾计划整理合成项目甲的资料；计划是否完成及具体日期不详。\n**这是本人原话的合成测试，不是当前状态。**',
  source:'来源说明：这是合成测试文件。原日期不详，部分内容可能为连续摘录，不得据导入日期推定经历日期。<script>window.materialSourceExecuted=true</script>',
  assistant:'助手参考稿：可以考虑整理一个索引；这一建议没有被本人确认。',
  overview:[
    '## 合成资料概览',
    '',
    '**资料索引，不代表当前状态。**',
    '',
    '- 本人原话记载的是一项历史计划；完成情况未知。',
    '- 来源说明与助手参考稿按各自角色保留，原日期不详。',
    '',
    '[安全外链](https://example.invalid/synthetic-reference)',
    '',
    '| 原话 | 平台来源说明 | 助手参考稿 |',
    '| --- | --- | --- |',
    '| 历史计划，状态未知 | 不能把导入日期写为经历日期 | 不升级为本人决定 |',
    '',
    '```text',
    'synthetic_readonly_reference_'.repeat(12),
    '```',
    '',
    '<img src="https://example.invalid/pixel" onerror="window.materialHtmlExecuted=true">',
    '<script>window.materialScriptExecuted=true</script>',
    '[危险链接](javascript:alert(1))'
  ].join('\n')
};
const fixturePath=path.join(root,'fixture.json');fs.writeFileSync(fixturePath,JSON.stringify(fixture));
const code=`import sys,json,datetime as dt
from pathlib import Path
from dsh_personal.server import Application,create_server
from dsh_personal.core import TZ
a=Application(Path(sys.argv[1]),clock=lambda:dt.datetime(2026,10,23,12,0,tzinfo=TZ),web_root=Path(sys.argv[2]))
f=json.loads(Path(sys.argv[3]).read_text())
payload={'id':'material-ui-synthetic-conversation','title':'合成材料来源','messages':[{'id':'synthetic-user','role':'user','content':f['user'],'created_at':None},{'id':'synthetic-source','role':'source','content':f['source'],'created_at':None},{'id':'synthetic-assistant','role':'assistant','content':f['assistant'],'created_at':None}]}
a.records.import_apply('chatgpt',json.dumps(payload,ensure_ascii=False),filename='synthetic-personal-history.json')
ids={m['role']:m['message_id'] for m in a.records.imported_messages()}
p=a.records.add_profile(f['overview'],category='background',scope='reference:personal-history',confirmed=False,source_refs=['import:'+ids['user'],'import:'+ids['source'],'import:'+ids['assistant']])
a.records.add_profile('合成普通画像，作为独立显示的对照。',category='background',scope='global',confirmed=True,user_input='合成用户确认输入')
server=create_server(a,port=0)
print(json.dumps({'port':server.server_address[1],'profile_id':p['item_id'],'ids':ids}),flush=True)
server.serve_forever()
`;
let server,browser,ready;
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function until(fn){for(let i=0;i<180;i++){if(fn())return;await pause(100);}throw Error('Timed out');}
function recordsSnapshot(){
  const result=spawnSync('python3',['-c',`import json,sys
from pathlib import Path
from dsh_personal.records import RecordsStore
r=RecordsStore(Path(sys.argv[1])/'personal.sqlite')
print(json.dumps({'global':r.context('global'),'profile':r.get_profile(sys.argv[2]),'history':r.list_profile(history=True,include_inactive=True),'imports':r.imported_messages()},ensure_ascii=False))`,data,ready.profile_id],{cwd:project,encoding:'utf8'});
  assert.equal(result.status,0,result.stderr);return JSON.parse(result.stdout);
}
function assertOriginals(items){
  for(const role of ['user','source','assistant']){
    const item=items.find(m=>m.message_id===ready.ids[role]);
    assert.ok(item);assert.equal(item.role,role);assert.equal(item.body,fixture[role]);assert.equal(item.original_at,null);assert.equal(item.original_date,null);
  }
}
(async()=>{
  let stdout='',stderr='';
  server=spawn('python3',['-c',code,data,web,fixturePath],{cwd:project,stdio:['ignore','pipe','pipe']});
  server.stdout.on('data',b=>{stdout+=b;try{ready=JSON.parse(stdout.trim());}catch{}});
  server.stderr.on('data',b=>stderr+=b);
  await until(()=>{if(server.exitCode!==null)throw Error(stderr);return ready;});
  const base='http://127.0.0.1:'+ready.port;
  browser=await chromium.launch({headless:true,executablePath:(process.env.QIJU_CHROMIUM_PATH || chromium.executablePath()),args:['--no-sandbox']});
  const context=await browser.newContext({viewport:{width:1440,height:1000}}),page=await context.newPage();
  page.on('pageerror',err=>report.page_errors.push(err.message));
  await context.route('**/*',route=>{
    if(new URL(route.request().url()).origin===base)return route.continue();
    report.external_requests.push(route.request().url());return route.abort();
  });
  await page.goto(base);await page.locator('#account-setup-form').waitFor();
  await page.getByLabel('账号',{exact:true}).fill('material_overview_fixture');
  await page.getByLabel('密码',{exact:true}).fill('synthetic-material-password');
  await page.getByLabel('再次输入密码',{exact:true}).fill('synthetic-material-password');
  await page.getByRole('button',{name:'创建账号并进入'}).click();await page.locator('#message-input').waitFor();
  await page.goto(base+'/#data');await page.locator('#import-form').waitFor();
  const overview=page.locator('[data-material-overview="'+ready.profile_id+'"]');
  await overview.waitFor();assert.equal(await page.locator('.material-overview').count(),1);
  const ordinary=page.locator('section.card').filter({has:page.getByRole('heading',{name:'个人画像与有效偏好',exact:true})});
  assert.ok((await ordinary.textContent()).includes('合成普通画像'));
  assert.ok(!(await ordinary.textContent()).includes('合成资料概览'));
  assert.equal(await overview.getByRole('heading',{name:'合成资料概览',exact:true}).count(),1);
  assert.equal(await overview.locator('strong').count(),1);assert.equal(await overview.locator('li').count(),2);
  assert.equal(await overview.locator('table').count(),1);assert.equal(await overview.locator('pre code').count(),1);
  assert.equal(await overview.locator('script,img,[onerror],a[href^="javascript:"]').count(),0);
  assert.ok((await overview.textContent()).includes('<script>window.materialScriptExecuted=true</script>'));
  assert.equal(await page.evaluate(()=>Boolean(window.materialHtmlExecuted||window.materialScriptExecuted||window.materialSourceExecuted)),false);
  assert.ok((await overview.getByRole('link',{name:'安全外链',exact:true}).getAttribute('rel')).includes('noopener'));
  report.checks.push('Reference overview appears only in 资料综述; headings/lists/tables/code render while raw HTML and javascript links remain inert');
  assert.equal(await overview.locator('details').getAttribute('open'),null);
  await overview.locator('summary').click();
  for(const role of ['user','source','assistant']){
    await overview.locator('[data-ref="import:'+ready.ids[role]+'"]').click();
    await page.getByRole('heading',{name:'证据与原文',exact:true}).waitFor();
    assert.ok((await page.locator('#modal .task-result').textContent()).includes(fixture[role]));
    assert.equal(await page.locator('#modal script,#modal img').count(),0);
    await page.locator('#modal').getByRole('button',{name:'关闭',exact:true}).click();
    await page.locator('#modal').waitFor({state:'hidden'});
  }
  report.checks.push('Collapsed evidence opens user/source/assistant records with unchanged original content and safe source display');
  for(const width of [1440,390,360]){
    await page.setViewportSize({width,height:width===1440?1000:844});
    const geometry=await page.evaluate(()=>({width:innerWidth,document:document.documentElement.scrollWidth,body:document.body.scrollWidth}));
    report.viewports.push(geometry);
    const screenshot=path.join(output,'materials-'+width+'.png');
    await page.screenshot({path:screenshot,animations:'disabled',fullPage:true});report.screenshots.push(path.relative(project,screenshot));
    assert.ok(geometry.document<=width+1&&geometry.body<=width+1,JSON.stringify(geometry));
  }
  report.checks.push('1440/390/360px layouts keep overview Markdown and expanded source references within viewport');
  await overview.getByRole('button',{name:'更正综述',exact:true}).click();await page.locator('#profile-form').waitFor();
  assert.equal(await page.locator('#profile-scope').inputValue(),'reference:personal-history');
  assert.equal(await page.locator('#profile-form input[name="confirmed"]').isChecked(),false);
  await page.locator('#profile-content').fill(fixture.overview+'\n\n更正：此处仍为历史资料索引，未确认计划完成。');
  await page.getByRole('button',{name:'保存此版本',exact:true}).click();await page.locator('#modal').waitFor({state:'hidden'});
  await overview.getByText('更正：此处仍为历史资料索引，未确认计划完成。',{exact:true}).waitFor();
  let saved=recordsSnapshot();assert.equal(saved.profile.version,2);assert.equal(saved.profile.scope,'reference:personal-history');assert.equal(saved.profile.confirmed,false);
  assert.deepEqual(new Set(saved.profile.source_refs),new Set(['import:'+ready.ids.user,'import:'+ready.ids.source,'import:'+ready.ids.assistant]));
  assert.equal(saved.global.length,1);assert.equal(saved.global[0].scope,'global');assert.ok(!saved.global.some(p=>p.item_id===ready.profile_id));assertOriginals(saved.imports);
  report.checks.push('Correction creates version 2 with reference scope, unconfirmed status, and evidence retained; actual global model context excludes it');
  await overview.getByRole('button',{name:'收起此综述',exact:true}).click();await overview.waitFor({state:'detached'});
  assert.equal(await page.locator('.material-overview').count(),0);
  await page.reload();await page.locator('#import-form').waitFor();assert.equal(await page.locator('.material-overview').count(),0);
  saved=recordsSnapshot();assert.equal(saved.profile.version,3);assert.equal(saved.profile.active,false);
  assert.deepEqual(saved.history.filter(p=>p.item_id===ready.profile_id).map(p=>p.version),[1,2,3]);assertOriginals(saved.imports);
  assert.ok(!saved.global.some(p=>p.item_id===ready.profile_id));
  report.checks.push('Hide creates inactive version 3 and remains hidden after reload; all source roles, original words, unknown dates, and prior overview versions remain');
  assert.deepEqual(report.page_errors,[]);assert.deepEqual(report.external_requests,[]);report.state='PASS';
})().catch(err=>{report.state='FAIL';report.error=err.stack||String(err);process.exitCode=1;}).finally(async()=>{
  if(browser)await browser.close();
  if(server&&server.exitCode===null){server.kill('SIGTERM');await until(()=>server.exitCode!==null||server.signalCode!==null).catch(()=>{});}
  fs.rmSync(root,{recursive:true,force:true});report.fixture_removed=!fs.existsSync(root);report.finished_at=new Date().toISOString();
  fs.writeFileSync(reportPath,JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report,null,2));
});
