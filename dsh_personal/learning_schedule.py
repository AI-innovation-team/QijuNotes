"""Prepare the next daily lesson privately; activate it using only local SQL."""
import datetime as dt
import hashlib
import json
from .core import TZ, dumps, uid

LEAD = dt.timedelta(hours=24)


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS learning_releases (
        activity_id TEXT PRIMARY KEY, due_at TEXT NOT NULL,
        activated_at TEXT, schedule_signature TEXT NOT NULL,
        created_at TEXT NOT NULL, reason TEXT)''')


def is_hidden(db, aid):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_releases'").fetchone():
        return False
    return bool(db.execute('SELECT 1 FROM learning_releases WHERE activity_id=? AND activated_at IS NULL', (aid,)).fetchone())


def _slot(schedule, day):
    date = dt.date.fromisoformat(day)
    weekdays = json.loads(schedule['weekdays']) if isinstance(schedule['weekdays'], str) else schedule['weekdays']
    due = dt.datetime.combine(date, dt.time.fromisoformat(schedule['time']), TZ)
    if (date.weekday() not in weekdays or (schedule['paused_until'] and day <= schedule['paused_until'])
            or dt.datetime.fromisoformat(schedule['effective_at']) > due):
        return None
    return due


def _signature(schedule):
    return hashlib.sha256(dumps({k: schedule[k] for k in ('time', 'weekdays', 'paused_until', 'effective_at')}).encode()).hexdigest()


def _activate(db, store, aid, due, *, manual=False, recovery=False):
    """Only this transaction makes a prepared activity visible to the user."""
    now = store.stamp()
    changed = db.execute('UPDATE learning_releases SET activated_at=?,reason=NULL WHERE activity_id=? AND activated_at IS NULL', (now, aid)).rowcount
    if not changed:
        return
    silent = manual or recovery or (store.now()-due).total_seconds() > 90 or due.astimezone(TZ).date().isoformat() < store.today()
    notification_state = 'page_only' if manual else 'recovery_silent' if silent else 'page_only'
    db.execute('UPDATE activities SET triggered_at=?,origin=?,notification_state=? WHERE id=?',
               (now, 'manual' if manual else 'recovery' if silent else 'scheduled', notification_state, aid))
    if not silent:
        # No notice exists before activation. ready_at is still required by both
        # notification transports, including the foreground SSE path.
        db.execute("INSERT OR IGNORE INTO notifications VALUES(?,?, 'first','pending',?,0,NULL,NULL,?)", (uid(), aid, now, now))
    from .learning_guard import release_approved
    published = release_approved(store, db, aid)
    if not published:
        reason = 'manual_preparation' if manual else 'not_ready_at_due'
        db.execute('UPDATE learning_releases SET reason=? WHERE activity_id=?', (reason, aid))
    db.execute('INSERT INTO observations(at,kind,details) VALUES(?,?,?)', (now, 'learning_schedule_activation', dumps({
        'activity_id': aid, 'scheduled_at': due.isoformat(), 'activated_at': now,
        'ready_at_activation': bool(published), 'manual': manual, 'notification_silent': silent,
        'delay_seconds': max(0, (store.now()-due).total_seconds())})))


def activate_manual(db, store, aid):
    row = db.execute('SELECT due_at,reason FROM learning_releases WHERE activity_id=? AND activated_at IS NULL', (aid,)).fetchone()
    if row:
        if row['reason'] not in ('source_deleted', 'recovery_hold'):
            _refresh_requirements(db, store, aid)
            db.execute("UPDATE jobs SET status='pending',run_after=? WHERE target=? AND status='schedule_hold'", (store.stamp(), aid))
        _activate(db, store, aid, dt.datetime.fromisoformat(row['due_at']), manual=True)


def _refresh_requirements(db, store, aid):
    from .learning_guard import _profiles, _preference_hash
    digest = _preference_hash(_profiles(db, store.today()))
    from .routine_settings import read_settings
    settings_version = read_settings(db, 'A1')['version']
    if settings_version:
        digest = hashlib.sha256(dumps([digest, settings_version]).encode()).hexdigest()
    key = 'learning_prepared_requirements:' + aid
    prior = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    if (prior and prior['value'] != digest) or (not prior and settings_version):
        # ensure_session rotates to the current requirement signature. A new job
        # version permits exactly one new initial generation per requirement set;
        # the native submission remains idempotent within its new session.
        version = int(hashlib.sha256(digest.encode()).hexdigest()[:15], 16)
        store._enqueue(db, 'learning', aid, version)
    db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, digest))


def tick(store, recovery=False):
    """Bounded, network-free work on the existing one-second scheduler."""
    if store.meta('isolated') == 'true':
        return
    now = store.now(); local = now.astimezone(TZ)
    with store.db(True) as db:
        if db.execute("SELECT 1 FROM meta WHERE key='isolated' AND value='true'").fetchone():
            return
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='records_meta'").fetchone() and db.execute("SELECT 1 FROM records_meta WHERE key='quarantine' AND value='1'").fetchone():
            return
        schedule = dict(db.execute("SELECT * FROM schedules WHERE routine='A1'").fetchone())
        # A requested topic may become available through catalog/night study.
        # Recheck at most once a minute, without model/network work in SQL.
        from .learning import topic_availability
        from .routine_settings import read_settings
        for gap_row in db.execute("SELECT key,value FROM meta WHERE key LIKE 'learning_topic_gap:%'").fetchall():
            gap=json.loads(gap_row['value']);aid=gap_row['key'].split(':',1)[1]
            if (now-dt.datetime.fromisoformat(gap['checked_at'])).total_seconds()<60:continue
            activity=db.execute('SELECT business_date FROM activities WHERE id=?',(aid,)).fetchone()
            if not activity:continue
            available_through=max(activity['business_date'],store.today())
            if not topic_availability(db,read_settings(db,'A1')['settings'],available_through):
                db.execute("UPDATE jobs SET status='pending',run_after=?,attempts=0 WHERE kind='learning' AND target=? AND status='material_wait'",(store.stamp(),aid))
                db.execute('DELETE FROM meta WHERE key=?',(gap_row['key'],))
            else:
                gap['checked_at']=store.stamp()
                db.execute('UPDATE meta SET value=? WHERE key=?',(dumps(gap),gap_row['key']))
        signature = _signature(schedule)
        for offset in (0, 1):
            day = (local.date()+dt.timedelta(days=offset)).isoformat(); due = _slot(schedule, day)
            if not due or not now < due <= now + LEAD:
                continue
            # Manual/history entries are authoritative and never replaced.
            if db.execute("SELECT 1 FROM activities WHERE routine='A1' AND business_date=?", (day,)).fetchone():
                continue
            aid = uid(); stamp = store.stamp()
            db.execute('''INSERT INTO activities(id,routine,business_date,status,created_at,scheduled_at,origin,notification_state)
                VALUES(?,'A1',?,'preparing',?,?,'scheduled_preparation','page_only')''', (aid, day, stamp, due.isoformat()))
            db.execute('INSERT INTO learning_releases VALUES(?,?,NULL,?,?,NULL)', (aid, due.isoformat(), signature, stamp))
            # No local placeholder, public message, delivery or notification.
            store._enqueue(db, 'session', aid); store._enqueue(db, 'learning', aid)
            _refresh_requirements(db, store, aid)
        rows = db.execute('''SELECT r.*,a.business_date,a.session_state FROM learning_releases r
            JOIN activities a ON a.id=r.activity_id WHERE r.activated_at IS NULL''').fetchall()
        for row in rows:
            if row['reason'] == 'recovery_hold':
                continue
            aid = row['activity_id']; due = _slot(schedule, row['business_date'])
            held = row['reason'] in ('schedule_changed', 'schedule_paused')
            deleted = db.execute("SELECT 1 FROM tombstones WHERE target_type='activity_derivatives' AND target_id=?", (aid,)).fetchone()
            if deleted or row['session_state'] == 'deletion_pending':
                db.execute("UPDATE learning_releases SET reason='source_deleted' WHERE activity_id=?", (aid,))
                continue
            if not due:
                db.execute("UPDATE learning_releases SET reason='schedule_changed' WHERE activity_id=?", (aid,))
                db.execute("UPDATE jobs SET status='schedule_hold' WHERE target=? AND kind IN ('session','learning','learning_review') AND status='pending'", (aid,))
                continue
            db.execute('UPDATE learning_releases SET due_at=?,schedule_signature=?,reason=CASE WHEN reason IN (\'schedule_changed\',\'schedule_paused\') THEN NULL ELSE reason END WHERE activity_id=?', (due.isoformat(), signature, aid))
            db.execute('UPDATE activities SET scheduled_at=? WHERE id=?', (due.isoformat(), aid))
            if held:
                db.execute("UPDATE jobs SET status='pending',run_after=? WHERE target=? AND status='schedule_hold'", (store.stamp(), aid))
            _refresh_requirements(db, store, aid)
            if now >= due:
                _activate(db, store, aid, due, recovery=recovery)


def processing(store, aid):
    with store.db() as db:
        row = db.execute('''SELECT r.*,a.ready_at FROM learning_releases r JOIN activities a ON a.id=r.activity_id
            WHERE r.activity_id=? AND r.activated_at IS NOT NULL''', (aid,)).fetchone()
        if row and not row['ready_at'] and row['reason'] == 'not_ready_at_due':
            return {'status': 'delayed', 'state': 'delayed', 'retryable': False,
                    'message': '本次课程未能按时通过检查，正文暂未发布；处理记录已保留。', 'scheduled_at': row['due_at']}
    return None
