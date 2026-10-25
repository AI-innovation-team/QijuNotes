// Targeted integration checks against the isolated, no-worker QA server only.
const { chromium } = require('../.runtime/browser/node_modules/playwright');
const fs = require('fs');
const { execFileSync } = require('child_process');
const assert = require('assert/strict');
const base = 'http://127.0.0.1:18880';
const phase = process.env.UI_FOLLOWUP_PHASE === 'before-cleanup' ? 'before-cleanup' : 'final';
const output = `evidence/P1/ui-followup-${phase}.json`;
const report = { at: new Date().toISOString(), kind: 'isolated_local_chromium_not_real_phone', base, checks: [], page_errors: [] };

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: (process.env.QIJU_CHROMIUM_PATH || chromium.executablePath()), args: ['--no-sandbox'] });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.on('pageerror', e => report.page_errors.push(e.message));
    const presentedReplies = [];
    page.on('response', async response => {
      if (response.url() !== base + '/api/action') return;
      const payload = response.request().postDataJSON();
      if (payload?.op === 'presented') {
        try { presentedReplies.push({ activity_id: payload.activity_id, lease_present: Boolean(payload.lease), response: await response.json() }); } catch {}
      }
    });
    await page.goto(base);
    await page.getByLabel('访问密钥').fill(fs.readFileSync('.runtime/qa/access-token', 'utf8').trim());
    await page.getByRole('button', { name: '打开起居注' }).click();
    await page.getByText('把今天，慢慢记下来。').waitFor();
    await page.waitForFunction(() => Boolean(liveLease));
    const initial = await page.evaluate(async () => (await fetch('/api/state')).json());
    const lastWeekDate = new Date(initial.today + 'T12:00:00+08:00');
    lastWeekDate.setUTCDate(lastWeekDate.getUTCDate() - 7);
    const previousDate = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(lastWeekDate);
    const marker = `隔离UI周切换夹具 ${Date.now()}`;
    for (const [day, label] of [[initial.today, '本周'], [previousDate, '上周']]) {
      const result = await page.evaluate(async payload => {
        const response = await fetch('/api/action', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-DSH-Client': 'web' }, body: JSON.stringify(payload) });
        return { status: response.status, body: await response.json() };
      }, { op: 'growth_save', line: 'doctorate', kind: 'action', content: { text: marker + label }, business_date: day, user_input: marker + label, source_refs: [] });
      assert.equal(result.status, 200);
    }
    await page.goto(base + '/#growth');
    const weekly = page.locator('section.card:has(button[data-action="growth-week"])');
    await weekly.getByText(marker + '本周', { exact: true }).waitFor();
    assert.equal(await weekly.getByText(marker + '上周', { exact: true }).count(), 0);
    const previousResponse = page.waitForResponse(r => r.url().includes('/api/state?') && new URL(r.url()).searchParams.get('week_date') === previousDate);
    await weekly.getByRole('button', { name: '上一完整周', exact: true }).click();
    const previousState = await (await previousResponse).json();
    await weekly.getByText(marker + '上周', { exact: true }).waitFor();
    assert.equal(await weekly.getByText(marker + '本周', { exact: true }).count(), 0);
    assert.equal(previousState.weekly_review.label, '上一完整周');
    assert.equal(await page.locator('.growth-columns').getByText(marker + '本周', { exact: true }).count(), 1);
    assert.equal(await page.locator('.growth-columns').getByText(marker + '上周', { exact: true }).count(), 1);
    await weekly.getByRole('button', { name: '＋ 补充上一完整周评价与拟调整事项' }).click();
    assert.equal(await page.getByLabel('归属日期').inputValue(), previousState.weekly_review.end);
    await page.locator('#modal [data-action="modal-close"]').first().click();
    report.checks.push({ name: 'previous_complete_week_real_UI', status: 'PASS', requested_date: previousDate, week_start: previousState.weekly_review.start, week_end: previousState.weekly_review.end, global_growth_unfiltered: true, review_date_defaults_to_week_end: true });

    // A scheduled first-notice fixture is seeded only in .runtime/qa. No worker,
    // model, webhook, production database or real schedule is changed.
    const fixture = JSON.parse(execFileSync('python3', ['-c', `
from pathlib import Path
from datetime import timedelta
import json
from dsh_personal.core import Store,uid
p=Path('.runtime/qa/personal.sqlite').resolve()
assert p.parent==Path('.runtime/qa').resolve()
s=Store(p);a=s.ensure_activity('A2',s.today(),origin='scheduled')
with s.db(True) as c:
 c.execute("INSERT INTO notifications VALUES(?,?,'first','pending',?,0,NULL,NULL,?) ON CONFLICT(activity_id,kind) DO UPDATE SET state='pending',receipt=NULL,error=NULL,run_after=excluded.run_after",(uid(),a['id'],(s.now()+timedelta(minutes=10)).isoformat(),s.stamp()))
 c.execute("UPDATE activities SET notification_state='page_only' WHERE id=?",(a['id'],))
print(json.dumps({'id':a['id'],'date':a['business_date']}))
`], { encoding: 'utf8' }));
    await page.getByTitle('刷新已保存状态').click();
    await page.evaluate(id => { location.hash = '#activity/' + encodeURIComponent(id); }, fixture.id);
    await page.getByLabel('输入消息').waitFor();
    await page.waitForFunction(async id => {
      const response = await fetch('/api/activity?id=' + encodeURIComponent(id));
      return (await response.json()).activity.notification_state === 'foreground_presented';
    }, fixture.id, { timeout: 10000 });
    const accepted = presentedReplies.filter(x => x.activity_id === fixture.id && x.response.suppressed === true);
    assert.ok(accepted.some(x => x.lease_present), 'actual suppression needs a live SSE lease');
    const stored = JSON.parse(execFileSync('python3', ['-c', `
import sqlite3,json
c=sqlite3.connect('.runtime/qa/personal.sqlite');c.row_factory=sqlite3.Row
r=c.execute("SELECT n.state,n.receipt FROM notifications n WHERE n.activity_id=? AND n.kind='first'",(${JSON.stringify(fixture.id)},)).fetchone()
print(json.dumps({'state':r['state'],'receipt_present':bool(r['receipt'])}))
`], { encoding: 'utf8' }));
    assert.equal(stored.state, 'foreground_presented');
    assert.equal(stored.receipt_present, true);
    report.checks.push({ name: 'visible_recent_interaction_live_SSE_suppresses_first_notification', status: 'PASS', server_suppressed: true, durable_notification_state: stored.state, lease_sent: true, external_delivery_claimed: false });

    await page.goto(base + '/#tasks');
    await page.getByRole('button', { name: '＋ 新建委托', exact: true }).click();
    const taskForm = page.locator('#task-form');
    const submissionId = await taskForm.locator('[name="submission_id"]').inputValue();
    assert.ok(submissionId);
    await page.getByLabel('希望完成什么').fill('隔离浏览器网络失败夹具，不发送到执行服务');
    const attemptedTasks = [];
    await page.route('**/api/action', async route => {
      const payload = route.request().postDataJSON();
      if (payload?.op === 'task_create') { attemptedTasks.push(payload); await route.abort('failed'); }
      else await route.continue();
    });
    for (let attempt = 0; attempt < 2; attempt++) {
      await taskForm.locator('button[type="submit"]').click();
      await page.waitForFunction(() => !document.querySelector('#task-form button[type="submit"]').disabled);
      assert.equal(await taskForm.locator('[name="submission_id"]').inputValue(), submissionId);
    }
    assert.equal(attemptedTasks.length, 2);
    assert.equal(attemptedTasks[0].submission_id, submissionId);
    assert.deepEqual(attemptedTasks[1], attemptedTasks[0]);
    await page.locator('#modal [data-action="modal-close"]').first().click();
    await page.getByRole('button', { name: '＋ 新建委托', exact: true }).click();
    assert.notEqual(await page.locator('#task-form [name="submission_id"]').inputValue(), submissionId);
    report.checks.push({ name: 'task_form_retry_preserves_submission_identity_and_content', status: 'PASS', attempts: 2, new_form_gets_new_identity: true, scope: 'browser_failed_network_requests_only_backend_persistence_tested_separately' });

    await page.locator('#modal [data-action="modal-close"]').first().click();
    await page.goto(base + '/#history');
    await page.getByText('把漏下的日子，接着聊。').waitFor();
    const recentDates = await page.locator('.history-days input[name="history-date"]').evaluateAll(nodes => nodes.map(node => node.value));
    assert.equal(recentDates.length, 7);
    assert.equal(new Set(recentDates).size, 7);
    assert.equal(recentDates.at(-1), initial.today);
    const historyDate = '2026-09-01';
    await page.getByLabel('其他日期', { exact: true }).fill(historyDate);
    await page.getByRole('button', { name: '加入选择', exact: true }).click();
    await page.locator(`.history-table [data-routine="A3"][data-date="${historyDate}"]`).waitFor();
    await page.screenshot({ path: 'evidence/P1/history-entry.png', fullPage: true });
    await page.locator(`.history-table [data-routine="A3"][data-date="${historyDate}"]`).click();
    await page.getByLabel('输入消息').waitFor();
    const historicalId = decodeURIComponent(new URL(page.url()).hash.split('/')[1]);
    const historic = await page.evaluate(async id => (await fetch('/api/activity?id=' + encodeURIComponent(id))).json(), historicalId);
    assert.equal(historic.activity.business_date, historyDate);
    assert.equal(historic.activity.routine, 'A3');
    assert.equal(historic.activity.notification_state, 'historical_silent');
    await page.goto(base + '/#history');
    await page.getByLabel('用自然语言选择历史活动').fill('补记 2026-09-01 的睡眠');
    await page.getByRole('button', { name: '打开对应活动', exact: true }).click();
    await page.getByLabel('输入消息').waitFor();
    assert.equal(decodeURIComponent(new URL(page.url()).hash.split('/')[1]), historicalId);
    const current = await page.evaluate(async () => (await fetch('/api/state')).json());
    const priorTodaySleep = initial.activities.find(a => a.routine === 'A3');
    if (priorTodaySleep) assert.equal(current.activities.find(a => a.routine === 'A3').id, priorTodaySleep.id);
    report.checks.push({ name: 'T26.entry_recent_arbitrary_and_natural_history_same_activity', status: 'PASS', recent_dates: recentDates, arbitrary_date: historyDate, routine: 'A3', same_activity_id: historicalId, historical_notification_state: historic.activity.notification_state, original_business_date: historic.activity.business_date, actual_created_at: historic.activity.created_at, today_activity_unchanged: Boolean(priorTodaySleep) });
    assert.deepEqual(report.page_errors, []);
    report.status = 'PASS';
  } catch (error) {
    report.status = 'FAIL'; report.error = String(error.stack || error); process.exitCode = 1;
  } finally {
    await browser.close();
    fs.writeFileSync(output, JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify(report, null, 2));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
