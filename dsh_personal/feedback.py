"""Owner feedback receipts. Recording a problem never claims code was changed.

Rows live in the existing private meta ledger and refer to immutable user input.
Only the application/editorial operator may record a tested resolution; model
output is never parsed as authority to mark a report resolved.
"""
import hashlib
import json
import re

from .core import dumps, uid

PREFIX = 'feedback:report:'


def is_feedback(body):
    text = body.strip()
    if text.startswith(('>', '```', '“', '「', '"')):
        return False
    if re.match(r'^(?:问题|使用体验|体验|产品)?反馈\s*[:：]', text):
        return True
    return bool(re.search(r'起居|每日一学|网站|网页|界面|输入框|回复框|通知|登录', text)
                and re.search(r'不回(?:我)?(?:的)?话|不回复|太慢|很慢|卡住|卡顿|报错|打不开|没收到|防御套话|防御性写作|不流畅|体验.{0,3}差', text))


def capture(db, message_id, stamp):
    source = db.execute("SELECT * FROM messages WHERE id=? AND role='user' AND deleted=0", (message_id,)).fetchone()
    if not source or not is_feedback(source['body']):
        return None
    ident = hashlib.sha256(message_id.encode()).hexdigest()[:24]
    key = PREFIX + ident
    prior = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    if prior:
        return json.loads(prior[0])
    receipt = 'feedback-receipt:' + ident
    row = dict(id=ident, activity_id=source['activity_id'], message_id=message_id,
               body=source['body'], status='recorded', created_at=stamp, updated_at=stamp,
               resolution=None, evidence=[], receipt_id=receipt)
    db.execute('INSERT INTO meta VALUES(?,?)', (key, dumps(row)))
    body = '已记录这条问题反馈。可在侧栏“问题反馈”查看处理记录。\n\n当前状态：待排查。保存反馈不会自动修改或部署程序；完成修复后会记录实际结果和验证依据。'
    db.execute("INSERT INTO messages VALUES(?,?,NULL,'assistant',?,?,'local_control',NULL,NULL,0)",
               (receipt, source['activity_id'], body, stamp))
    return row


def scrub(db, stamp):
    """Recheck originals during live deletion and recovery, including missing rows."""
    for raw in db.execute('SELECT key,value FROM meta WHERE key LIKE ?', (PREFIX+'%',)).fetchall():
        row = json.loads(raw['value'])
        if row['status'] == 'deleted':
            continue
        source = db.execute('SELECT deleted FROM messages WHERE id=?', (row['message_id'],)).fetchone()
        tomb = db.execute("SELECT 1 FROM tombstones WHERE target_id=? AND target_type IN ('message','derived_message')", (row['message_id'],)).fetchone()
        receipt = db.execute('SELECT deleted FROM messages WHERE id=?', (row['receipt_id'],)).fetchone()
        if source and not source[0] and not tomb and receipt and not receipt[0]:
            continue
        row.update(body='', status='deleted', resolution=None, evidence=[], updated_at=stamp)
        db.execute('UPDATE meta SET value=? WHERE key=?', (dumps(row), raw['key']))
        db.execute("UPDATE messages SET body='',deleted=1,processing_state='deleted' WHERE id=?", (row['receipt_id'],))
        db.execute('INSERT OR IGNORE INTO tombstones VALUES(?,?,?,?,?)', (uid(), 'derived_message', row['receipt_id'], None, stamp))


def view(store):
    if store.meta('isolated') == 'true':
        return {'feedback': []}
    from .learning_schedule import is_hidden
    with store.db() as db:
        rows = [json.loads(r[0]) for r in db.execute('SELECT value FROM meta WHERE key LIKE ?', (PREFIX+'%',))]
        rows = [r for r in rows if r['status'] != 'deleted' and not is_hidden(db, r['activity_id'])
                and db.execute('SELECT 1 FROM messages WHERE id=? AND deleted=0', (r['message_id'],)).fetchone()
                and db.execute('SELECT 1 FROM messages WHERE id=? AND deleted=0', (r['receipt_id'],)).fetchone()]
    return {'feedback': sorted(rows, key=lambda r: r['created_at'], reverse=True)}


def resolve(store, ident, resolution, evidence):
    """Operator-only; deliberately absent from model tools and public actions."""
    if not isinstance(resolution, str) or not resolution.strip() or not isinstance(evidence, list) or not evidence or any(not isinstance(v, str) or not v.strip() for v in evidence):
        raise ValueError('处理结果须附实际验证依据')
    with store.db(True) as db:
        scrub(db, store.stamp())
        raw = db.execute('SELECT value FROM meta WHERE key=?', (PREFIX+ident,)).fetchone()
        if not raw:
            raise KeyError('反馈不存在')
        row = json.loads(raw[0])
        if row['status'] == 'deleted':
            raise ValueError('反馈来源已删除')
        row.update(status='resolved', resolution=resolution, evidence=evidence, updated_at=store.stamp())
        db.execute('UPDATE meta SET value=? WHERE key=?', (dumps(row), PREFIX+ident))
        return row


def ledger(db):
    # Recovery already holds original messages. Only the processing receipt is
    # needed to preserve a later outcome across restoring an earlier snapshot.
    return [{k: v for k, v in json.loads(raw[0]).items() if k != 'body'}
            for raw in db.execute('SELECT value FROM meta WHERE key LIKE ?', (PREFIX+'%',))]


def recover(db, entries, stamp):
    for incoming in entries:
        if not isinstance(incoming, dict) or incoming.get('status') not in {'recorded','in_progress','resolved','deleted'}:
            raise ValueError('反馈恢复状态无效')
        if not isinstance(incoming.get('evidence'), list) or any(not isinstance(x, str) for x in incoming['evidence']):
            raise ValueError('反馈恢复验证依据无效')
        ident=incoming['id'];key=PREFIX+ident
        expected=hashlib.sha256(incoming['message_id'].encode()).hexdigest()[:24]
        if ident!=expected or incoming['receipt_id']!='feedback-receipt:'+ident:
            raise ValueError('反馈恢复来源身份无效')
        old=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        row=json.loads(old[0]) if old else None
        rank={'recorded':0,'in_progress':1,'resolved':2,'deleted':3}
        if row and (row['status']=='deleted' or row['updated_at']>incoming['updated_at'] or
                    (row['updated_at']==incoming['updated_at'] and rank[row['status']]>=rank[incoming['status']])):
            continue
        source=db.execute("SELECT * FROM messages WHERE id=? AND role='user' AND deleted=0",(incoming['message_id'],)).fetchone()
        # Never fabricate a missing user input or a missing public receipt.
        value={**incoming,'body':source['body'] if source and source['activity_id']==incoming['activity_id'] else ''}
        if not value['body'] or incoming['status']=='deleted':
            value.update(body='',status='deleted',resolution=None,evidence=[])
        db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(key,dumps(value)))
    scrub(db,stamp)
