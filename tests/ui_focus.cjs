// T11.focus only: a new A2 notice must not interrupt an A1 draft.
// This test writes labelled fixtures only to the isolated 18880 no-worker QA instance.
const { chromium } = require('../.runtime/browser/node_modules/playwright');
const fs = require('fs');
const { execFileSync } = require('child_process');
const assert = require('assert/strict');
const base = 'http://127.0.0.1:18880';
const report = { at: new Date().toISOString(), kind: 'isolated_local_chromium_not_real_phone', acceptance_id: 'T11.focus', base, page_errors: [] };

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: (process.env.QIJU_CHROMIUM_PATH || chromium.executablePath()), args: ['--no-sandbox'] });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.on('pageerror', error => report.page_errors.push(error.message));
    const receipts = [];
    page.on('response', async response => {
      if (response.url() !== base + '/api/action') return;
      const payload = response.request().postDataJSON();
      if (payload?.op === 'presented') {
        try { receipts.push({ activity_id: payload.activity_id, lease_present: Boolean(payload.lease), response: await response.json() }); } catch {}
      }
    });
    await page.goto(base);
    await page.getByLabel('访问密钥').fill(fs.readFileSync('.runtime/qa/access-token', 'utf8').trim());
    await page.getByRole('button', { name: '打开起居注' }).click();
    await page.locator('[data-action="open-routine"][data-routine="A1"]').click();
    await page.getByLabel('输入消息').waitFor();
    // Reload gives a fresh SSE connection with no previous in-memory seen IDs.
    await page.reload();
    await page.getByLabel('输入消息').waitFor();
    await page.waitForFunction(() => Boolean(liveLease));
    await page.evaluate(() => {
      window.__qaFocusNotices = [];
      eventStream.addEventListener('activity', event => window.__qaFocusNotices.push(JSON.parse(event.data).id));
    });
    const draft = '隔离 T11 焦点夹具：正在学习中思考的未发送文字。';
    await page.getByLabel('输入消息').fill(draft);
    await page.getByLabel('输入消息').focus();
    const before = await page.evaluate(() => ({ route: location.hash, focused: document.activeElement.id, draft: document.querySelector('#message-input').value, routine: currentActivity.activity.routine }));
    assert.equal(before.routine, 'A1');
    assert.equal(before.focused, 'message-input');

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
print(json.dumps({'id':a['id'],'date':a['business_date'],'ready_at':a['ready_at']}))
`], { encoding: 'utf8' }));
    assert.ok(fixture.ready_at);
    await page.waitForFunction(id => window.__qaFocusNotices.includes(id), fixture.id, { timeout: 10000 });
    await page.waitForFunction(async id => {
      const response = await fetch('/api/activity?id=' + encodeURIComponent(id));
      return (await response.json()).activity.notification_state === 'foreground_presented';
    }, fixture.id, { timeout: 10000 });
    const after = await page.evaluate(() => ({ route: location.hash, focused: document.activeElement.id, draft: document.querySelector('#message-input').value, routine: currentActivity.activity.routine }));
    assert.deepEqual(after, before);
    assert.ok(receipts.some(row => row.activity_id === fixture.id && row.lease_present && row.response.suppressed === true));
    const durable = JSON.parse(execFileSync('python3', ['-c', `
import sqlite3,json
c=sqlite3.connect('.runtime/qa/personal.sqlite');c.row_factory=sqlite3.Row
r=c.execute("SELECT state,receipt FROM notifications WHERE activity_id=? AND kind='first'",(${JSON.stringify(fixture.id)},)).fetchone()
print(json.dumps({'state':r['state'],'receipt_present':bool(r['receipt'])}))
`], { encoding: 'utf8' }));
    assert.equal(durable.state, 'foreground_presented');
    assert.equal(durable.receipt_present, true);
    assert.deepEqual(report.page_errors, []);
    Object.assign(report, { status: 'PASS', fresh_SSE_activity_observed: true, active_routine: before.routine, arriving_routine: 'A2', route_unchanged: before.route === after.route, focus_unchanged: before.focused === after.focused, unsent_draft_unchanged: before.draft === after.draft, durable_notification_state: durable.state, live_lease_sent: true, server_suppressed: true, external_delivery_claimed: false });
  } catch (error) {
    report.status = 'FAIL'; report.error = String(error.stack || error); process.exitCode = 1;
  } finally {
    await browser.close();
    fs.writeFileSync('evidence/P1/ui-focus.json', JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify(report, null, 2));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
