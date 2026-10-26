"""Private A1 candidates and transactional publication after source review.

This module performs no network calls. Candidate text is deliberately separate
from messages: activity views, journals and readable exports cannot see drafts.
All mutation entry points recheck provenance, session and saved requirements.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

STATES = {'pending', 'reviewing', 'approved', 'rejected', 'cancelled', 'recovery_hold', 'deleted'}
TERMINAL = {'approved', 'cancelled', 'deleted', 'recovery_hold'}
JSON_FIELDS = {'turn', 'manifest', 'preferences', 'source_refs', 'user_message_ids', 'user_hashes', 'audit'}
PUBLIC = {
    'pending': '学习回复已生成，正在等待核对史料与分析边界。',
    'reviewing': '正在核对学习回复中的史实、理论分析与现实类比。',
    'rejected': '这份学习回复暂未通过来源核对，尚未发布；可以重试核对。',
    'cancelled': '本轮学习回复所依据的上下文已改变，旧稿未发布。',
    'recovery_hold': '恢复的学习核对记录已隔离，尚未发布新内容。',
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS learning_reviews (
        id TEXT PRIMARY KEY, activity_id TEXT NOT NULL, session_id TEXT NOT NULL,
        end_seq INTEGER NOT NULL, assistant_seq INTEGER NOT NULL,
        message_id TEXT NOT NULL UNIQUE, job_version INTEGER NOT NULL,
        mode TEXT NOT NULL CHECK(mode IN ('initial','followup')),
        status TEXT NOT NULL CHECK(status IN
            ('pending','reviewing','approved','rejected','cancelled','recovery_hold','deleted')),
        turn TEXT NOT NULL, draft TEXT NOT NULL, revised_body TEXT,
        draft_hash TEXT NOT NULL, published_hash TEXT,
        manifest TEXT NOT NULL, manifest_hash TEXT NOT NULL,
        preferences TEXT NOT NULL, preferences_hash TEXT NOT NULL,
        source_refs TEXT NOT NULL, user_message_ids TEXT NOT NULL,
        user_hashes TEXT NOT NULL, audit TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, attempt_token TEXT,
        reason TEXT, received_at TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(session_id,end_seq), UNIQUE(activity_id,job_version))''')
    db.execute('CREATE INDEX IF NOT EXISTS learning_reviews_activity ON learning_reviews(activity_id,created_at)')


def _exists(db, table):
    return bool(db.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?', (table,)).fetchone())


def _decode(row):
    if row is None:
        return None
    value = dict(row)
    for key in JSON_FIELDS:
        value[key] = json.loads(value[key])
    return value


def _profiles(db, today):
    if not _exists(db, 'records_profile'):
        return []
    if db.execute("SELECT 1 FROM records_meta WHERE key='quarantine' AND value='1'").fetchone():
        return []
    from .records import RecordsStore
    rows = [RecordsStore._decode_profile(r) for r in db.execute('''SELECT p.* FROM records_profile p
        WHERE p.version=(SELECT MAX(q.version) FROM records_profile q WHERE q.item_id=p.item_id)
        AND p.active=1 AND p.scope IN ('global','routine:A1')''')]
    return [p for p in rows if (p['confirmed'] or p.get('evidence_status') == 'source_attested')
            and p['category'] != 'hypothesis' and (not p['valid_until'] or p['valid_until'][:10] >= today)]


def _preference_hash(rows):
    # Operational timestamps and UI-only fields do not change the requirements.
    keys = ('item_id', 'version', 'category', 'scope', 'content', 'active', 'confirmed',
            'valid_until', 'source_refs', 'evidence_status')
    normalized = []
    for row in rows:
        item = {key: row.get(key) for key in keys}
        item['active'] = bool(item['active'])
        item['confirmed'] = bool(item['confirmed'])
        item['source_refs'] = sorted(item['source_refs'] or [])
        normalized.append(item)
    return _hash(sorted(normalized, key=lambda p: (p['item_id'], p['version'])))


def _manifest_hash(manifest):
    # Publishing a previous reviewed answer changes delivery, not its sources.
    return _hash({key: value for key, value in manifest.items() if key != 'delivery'})


def _deleted_ref(db, ref):
    kind, _, identifier = ref.partition(':')
    identifier = identifier or ref
    if _exists(db, 'records_tombstones') and db.execute(
            'SELECT 1 FROM records_tombstones WHERE object_id IN (?,?)', (identifier, ref)).fetchone():
        return True
    types = {'message': ('message', 'derived_message'), 'native_session': ('native_session',),
             'attachment': ('attachment',), 'learning_review': ('learning_review',)}.get(kind, ())
    if types and db.execute('SELECT 1 FROM tombstones WHERE target_id=? AND target_type IN (' +
                            ','.join('?' for _ in types) + ')', (identifier, *types)).fetchone():
        return True
    if db.execute("SELECT 1 FROM tombstones WHERE target_type='context_ref' AND target_id=?", (ref,)).fetchone():
        return True
    if kind == 'message':
        row = db.execute('SELECT deleted FROM messages WHERE id=?', (identifier,)).fetchone()
        return row is None or bool(row['deleted'])
    return False


def _valid(db, row, today):
    if row['status'] == 'deleted':
        return 'deleted'
    if db.execute("SELECT 1 FROM tombstones WHERE target_type='learning_review' AND target_id=?", (row['id'],)).fetchone():
        return 'source_deleted'
    if db.execute("SELECT 1 FROM meta WHERE key='isolated' AND value='true'").fetchone():
        return 'isolated'
    if _exists(db, 'records_meta') and db.execute(
            "SELECT 1 FROM records_meta WHERE key='quarantine' AND value='1'").fetchone():
        return 'isolated'
    refs = row['source_refs'] + ['native_session:' + row['session_id'], 'message:' + row['message_id']]
    # The candidate's not-yet-public message is expected to be absent.
    for ref in refs:
        if ref == 'message:' + row['message_id']:
            if db.execute("SELECT 1 FROM tombstones WHERE target_id=? AND target_type IN ('message','derived_message')", (row['message_id'],)).fetchone():
                return 'source_deleted'
        elif _deleted_ref(db, ref):
            return 'source_deleted'
    current = db.execute('SELECT * FROM activities WHERE id=?', (row['activity_id'],)).fetchone()
    if not current or current['routine'] != 'A1' or current['session_id'] != row['session_id'] or current['session_state'] != 'ready':
        return 'session_changed'
    from .learning import source_identity_frozen
    from .routine_settings import read_settings
    if row['mode']=='initial' and not source_identity_frozen(db,row['activity_id']) and row['manifest'].get('routine_settings_version', 0) != read_settings(db, 'A1')['version']:
        return 'preferences_changed'
    for mid, digest in row['user_hashes'].items():
        source = db.execute("SELECT body FROM messages WHERE id=? AND activity_id=? AND role='user' AND deleted=0",
                            (mid, row['activity_id'])).fetchone()
        if not source or _hash(source['body']) != digest:
            return 'source_changed'
    source = db.execute('SELECT value FROM meta WHERE key=?', ('learning_selection:' + row['activity_id'],)).fetchone()
    try:
        # Old A1 sessions may predate persisted lesson selections. They can
        # receive source-bounded replies with an explicitly empty packet; a
        # subsequently installed packet invalidates that earlier snapshot.
        current_manifest = json.loads(source['value']) if source else {'cards': []}
        if _manifest_hash(current_manifest) != row['manifest_hash']:
            return 'source_changed'
    except (ValueError, TypeError, AttributeError):
        return 'source_changed'
    if _preference_hash(_profiles(db, today)) != row['preferences_hash']:
        return 'preferences_changed'
    return None


def enqueue(store, activity, turn, draft, manifest, preferences, source_refs=(), user_message_ids=(), mode='initial'):
    """Persist one completed native turn without exposing any of its text.

    Existing public messages belong to the pre-guard history and are not adopted
    or modified. Repeated ingestion returns the original immutable candidate.
    """
    if mode not in ('initial', 'followup') or not isinstance(draft, str) or not draft.strip():
        raise ValueError('A completed, nonempty learning candidate is required')
    if not isinstance(manifest, dict) or not isinstance(preferences, list):
        raise ValueError('Source and preference snapshots are required')
    sid = turn.get('session_id') or activity.get('session_id')
    end_seq, assistant_seq = turn.get('end_seq'), turn.get('assistant_seq')
    if not sid or any(type(seq) is not int or seq < 0 for seq in (end_seq, assistant_seq)) or assistant_seq > end_seq:
        raise ValueError('Stable native turn and assistant sequence numbers are required')
    cid = 'learning-review:' + sid + ':' + str(end_seq)
    mid = 'dsh:' + sid + ':' + str(assistant_seq)
    version = int(hashlib.sha256(cid.encode()).hexdigest()[:15], 16)
    now = store.stamp()
    refs = list(source_refs) + ['activity:' + activity['id'], 'native_session:' + sid]
    user_ids = list(dict.fromkeys(user_message_ids))
    refs += ['message:' + value for value in user_ids]
    for p in preferences:
        refs += ['profile:' + p['item_id']] + list(p.get('source_refs') or [])
    refs = sorted(set(refs))
    with store.db(True) as db:
        old = _decode(db.execute('SELECT * FROM learning_reviews WHERE id=?', (cid,)).fetchone())
        if old:
            return old
        if db.execute('SELECT 1 FROM messages WHERE id=?', (mid,)).fetchone():
            return None
        user_hashes = {}
        for source_id in user_ids:
            source = db.execute("SELECT body FROM messages WHERE id=? AND activity_id=? AND role='user' AND deleted=0", (source_id, activity['id'])).fetchone()
            if source:
                user_hashes[source_id] = _hash(source['body'])
        row = dict(id=cid, activity_id=activity['id'], session_id=sid, end_seq=end_seq,
                   assistant_seq=assistant_seq, message_id=mid, job_version=version, mode=mode,
                   status='pending', turn=turn, draft=draft, revised_body=None,
                   draft_hash=_hash(draft), published_hash=None, manifest=manifest,
                   manifest_hash=_manifest_hash(manifest), preferences=preferences,
                   preferences_hash=_preference_hash(preferences), source_refs=refs,
                   user_message_ids=user_ids, user_hashes=user_hashes, audit=[], attempts=0,
                   attempt_token=None, reason=None, received_at=turn.get('received_at') or now,
                   created_at=now, updated_at=now)
        reason = _valid(db, row, store.today())
        if len(user_hashes) != len(user_ids):
            reason = 'source_deleted'
        if reason:
            row['status'], row['reason'] = 'cancelled', reason
        values = {key: _json(value) if key in JSON_FIELDS else value for key, value in row.items()}
        db.execute('INSERT INTO learning_reviews (' + ','.join(values) + ') VALUES (' + ','.join('?' for _ in values) + ')', tuple(values.values()))
        if reason == 'source_deleted':
            scrub(db, message_ids=[mid], stamp=now)
            return _decode(db.execute('SELECT * FROM learning_reviews WHERE id=?', (cid,)).fetchone())
        if row['status'] == 'pending':
            store._enqueue(db, 'learning_review', activity['id'], version)
        return row


def get_pending(store, aid, version=None, include_terminal=False):
    """Read one private candidate for its durable review job."""
    with store.db() as db:
        sql = 'SELECT * FROM learning_reviews WHERE activity_id=?'
        if not include_terminal:
            sql += " AND status IN ('pending','reviewing')"
        args = [aid]
        if version is not None:
            sql += ' AND job_version=?'; args.append(version)
        return _decode(db.execute(sql + ' ORDER BY rowid LIMIT 1', args).fetchone())


def snapshot(store, cid):
    with store.db() as db:
        row = _decode(db.execute('SELECT * FROM learning_reviews WHERE id=?', (cid,)).fetchone())
        if row:
            row['invalid_reason'] = _valid(db, row, store.today())
            row['still_valid'] = row['invalid_reason'] is None and (row['status'] not in TERMINAL or (row['status']=='approved' and row['published_hash'] is None))
        return row


def _cancel(db, row, reason, stamp):
    if reason == 'source_deleted':
        scrub(db, message_ids=[row['message_id']], stamp=stamp)
        return
    db.execute("UPDATE learning_reviews SET status='cancelled',reason=?,attempt_token=NULL,updated_at=? WHERE id=? AND status NOT IN ('deleted','recovery_hold') AND (status!='approved' OR published_hash IS NULL)", (reason, stamp, row['id']))
    db.execute("UPDATE jobs SET status='cancelled',lease_until=NULL,error=NULL WHERE kind='learning_review' AND target=? AND version=? AND status!='done'", (row['activity_id'], row['job_version']))


def record_attempt(store, cid, result=None, revised_body=None, attempt_token=None):
    """Start/resume an attempt, or append a private audit stage.

    A new attempt token fences late results from a previous worker after a lease
    or process restart. Pass that token when recording results and committing.
    """
    with store.db(True) as db:
        row = _decode(db.execute('SELECT * FROM learning_reviews WHERE id=?', (cid,)).fetchone())
        if not row or row['status'] not in ('pending', 'reviewing'):
            return None
        reason = _valid(db, row, store.today())
        if reason:
            _cancel(db, row, reason, store.stamp())
            return None
        if result is None:
            token = uuid4().hex
            db.execute("UPDATE learning_reviews SET status='reviewing',attempts=attempts+1,attempt_token=?,updated_at=? WHERE id=?", (token, store.stamp(), cid))
        else:
            if not attempt_token or attempt_token != row['attempt_token']:
                return None
            if row['status'] != 'reviewing':
                return None
            audit = row['audit'] + [{'at': store.stamp(), 'attempt': row['attempts'], 'result': result}]
            db.execute('UPDATE learning_reviews SET audit=?,revised_body=COALESCE(?,revised_body),updated_at=? WHERE id=?', (_json(audit), revised_body, store.stamp(), cid))
        return _decode(db.execute('SELECT * FROM learning_reviews WHERE id=?', (cid,)).fetchone())


def commit(store, cid, approved, revised_body=None, audit=None, attempt_token=None):
    """Atomically publish only a reviewed candidate whose inputs still match."""
    if type(approved) is not bool:
        raise ValueError('Review approval must be a boolean')
    with store.db(True) as db:
        row = _decode(db.execute('SELECT * FROM learning_reviews WHERE id=?', (cid,)).fetchone())
        if not row:
            return {'status': 'cancelled', 'published': False, 'message_id': None, 'reason': 'missing'}
        response = {'status': row['status'], 'published': False, 'message_id': row['message_id'], 'reason': row['reason']}
        if row['status'] in TERMINAL or row['status'] == 'rejected':
            return response
        if row['status'] != 'reviewing' or not attempt_token or attempt_token != row['attempt_token']:
            return {**response, 'reason': 'stale_attempt'}
        reason = _valid(db, row, store.today())
        if reason:
            _cancel(db, row, reason, store.stamp())
            return {**response, 'status': 'deleted' if reason == 'source_deleted' else 'cancelled', 'reason': reason}
        prior = db.execute('SELECT * FROM messages WHERE id=?', (row['message_id'],)).fetchone()
        if prior:
            _cancel(db, row, 'public_message_exists', store.stamp())
            return {**response, 'status': 'cancelled', 'reason': 'public_message_exists'}
        body = revised_body if revised_body is not None else (row['revised_body'] if row['revised_body'] is not None else row['draft'])
        if approved and (not isinstance(body, str) or not body.strip()):
            raise ValueError('Approved learning text must be nonempty')
        audits = row['audit'] + ([{'at': store.stamp(), 'attempt': row['attempts'], 'result': audit}] if audit is not None else [])
        status = 'approved' if approved else 'rejected'
        db.execute('''UPDATE learning_reviews SET status=?,revised_body=?,published_hash=?,audit=?,
            reason=?,attempt_token=NULL,updated_at=? WHERE id=?''',
                   (status, body if approved else revised_body or row['revised_body'], _hash(body) if approved else None,
                    _json(audits), None if approved else 'review_not_passed', store.stamp(), cid))
        from .learning_schedule import is_hidden
        staged = approved and row['mode'] == 'initial' and is_hidden(db, row['activity_id'])
        if staged:
            db.execute('UPDATE learning_reviews SET published_hash=NULL WHERE id=?', (cid,))
        elif approved:
            _publish(store, db, row, body)
        return {'status': status, 'published': approved and not staged, 'message_id': row['message_id'],
                'reason': None if approved else 'review_not_passed', 'mode': row['mode'], 'staged': bool(staged)}


def _publish(store, db, row, body):
    """Caller holds the activation/approval transaction; no network work."""
    published_at = store.stamp()
    db.execute("UPDATE learning_reviews SET published_hash=?,updated_at=? WHERE id=?", (_hash(body), published_at, row['id']))
    db.execute('''INSERT INTO messages (id,activity_id,submission_id,role,body,received_at,
        processing_state,native_id,error,deleted) VALUES (?,?,NULL,'assistant',?,?,'learning_reviewed',?,NULL,0)''',
               (row['message_id'], row['activity_id'], body, published_at, str(row['assistant_seq'])))
    if row['mode'] == 'initial':
        current = db.execute('SELECT value FROM meta WHERE key=?', ('learning_selection:' + row['activity_id'],)).fetchone()
        if current:
            manifest = json.loads(current['value'])
            # Production manifests carry prepared_at. The pure SQL
            # helper also confirms native identity and all source URLs.
            if 'prepared_at' in manifest:
                from .learning import _delivered_message
                if not _delivered_message(db, manifest, row['message_id']):
                    raise ValueError('Reviewed lesson lacks complete delivery evidence')
            if manifest.get('delivery') is None:
                manifest['delivery'] = {'message_id': row['message_id'], 'received_at': published_at}
                db.execute('UPDATE meta SET value=? WHERE key=?', (_json(manifest), 'learning_selection:' + row['activity_id']))
        db.execute("UPDATE activities SET ready_at=COALESCE(ready_at,?),status=CASE WHEN status='preparing' THEN 'awaiting_reply' ELSE status END WHERE id=?", (published_at, row['activity_id']))
        db.execute('DELETE FROM meta WHERE key=?', ('learning_processing:' + row['activity_id'],))
        db.execute("UPDATE jobs SET status='done',error=NULL,lease_until=NULL WHERE kind='learning' AND target=? AND status='failed'", (row['activity_id'],))
    db.execute("UPDATE learning_releases SET reason=NULL WHERE activity_id=? AND activated_at IS NOT NULL", (row['activity_id'],))
    return True


def release_approved(store, db, aid):
    """Publish an already audited initial lesson at its authorized activation."""
    from .learning_schedule import is_hidden
    if is_hidden(db, aid):
        return False
    row = _decode(db.execute("SELECT * FROM learning_reviews WHERE activity_id=? AND mode='initial' AND status='approved' AND published_hash IS NULL ORDER BY rowid DESC LIMIT 1", (aid,)).fetchone())
    if not row:
        return False
    reason = _valid(db, row, store.today())
    if reason:
        _cancel(db, row, reason, store.stamp())
        return False
    if db.execute('SELECT 1 FROM messages WHERE id=?', (row['message_id'],)).fetchone():
        return False
    body = row['revised_body'] or row['draft']
    if not body.strip():
        return False
    return _publish(store, db, row, body)

def public_status(store, aid, include_approved=False):
    with store.db() as db:
        row = db.execute('''SELECT r.* FROM learning_reviews r JOIN activities a ON a.id=r.activity_id
            WHERE r.activity_id=? AND r.session_id=a.session_id AND a.session_state='ready'
            ORDER BY r.rowid DESC LIMIT 1''', (aid,)).fetchone()
        if not row or (row['status'] not in PUBLIC and not (include_approved and row['status']=='approved')):
            return None
        known=set(json.loads(row['user_message_ids']))
        if any(r[0] not in known for r in db.execute("SELECT id FROM messages WHERE activity_id=? AND deleted=0 AND role='user' AND processing_state IN ('pending','accepted_by_dsh','awaiting_question_batch')",(aid,))):
            return None
        failed = db.execute("SELECT 1 FROM jobs WHERE kind='learning_review' AND target=? AND version=? AND status='failed'", (aid, row['job_version'])).fetchone()
        interrupted = row['status'] in ('pending', 'reviewing') and bool(failed)
        return {'state': row['status'], 'status': 'failed' if interrupted else row['status'],
                'message': '学习回复的来源核对暂未完成，可以重试核对；尚未发布新内容。' if interrupted else PUBLIC.get(row['status'],'本轮学习回复已经核对并发布，可直接继续提问。' if row['published_hash'] else '学习内容已通过核对，等待计划发布时间。'),
                'mode': row['mode'], 'retryable': row['status'] == 'rejected' or interrupted}


def retry(store, aid, *, candidate_id=None):
    """Recheck the retained candidate, never resubmit a user or native turn."""
    with store.db(True) as db:
        row = _decode(db.execute('SELECT * FROM learning_reviews WHERE activity_id=?'+(' AND id=?' if candidate_id else '')+' ORDER BY rowid DESC LIMIT 1', (aid,candidate_id) if candidate_id else (aid,)).fetchone())
        if not row:
            return False
        failed = db.execute("SELECT 1 FROM jobs WHERE kind='learning_review' AND target=? AND version=? AND status='failed'", (aid, row['job_version'])).fetchone()
        if row['status'] != 'rejected' and not (row['status'] in ('pending', 'reviewing') and failed):
            return False
        reason = _valid(db, row, store.today())
        if reason:
            _cancel(db, row, reason, store.stamp())
            return False
        audit=row['audit']+[{'at':store.stamp(),'attempt':row['attempts'],'result':{'stage':'explicit_retry'}}]
        db.execute("UPDATE learning_reviews SET status='pending',reason=NULL,attempt_token=NULL,audit=?,updated_at=? WHERE id=?", (_json(audit),store.stamp(), row['id']))
        store._enqueue(db, 'learning_review', aid, row['job_version'])
        db.execute("UPDATE jobs SET status='pending',attempts=0,lease_until=NULL,error=NULL,run_after=? WHERE kind='learning_review' AND target=? AND version=?", (store.stamp(), aid, row['job_version']))
        return True


def scrub(db, activity_ids=(), message_ids=(), source_refs=(), stamp=None):
    """Remove private derivatives alongside source deletion, including late jobs."""
    if not _exists(db, 'learning_reviews'):
        return []
    aids, mids = set(activity_ids), set(message_ids)
    refs = set(source_refs) | {'activity:' + v for v in aids} | {'message:' + v for v in mids}
    removed, now = [], stamp or _stamp()
    for raw in db.execute("SELECT * FROM learning_reviews WHERE status!='deleted'").fetchall():
        row = _decode(raw)
        if row['activity_id'] not in aids and row['message_id'] not in mids and not (set(row['source_refs']) & refs) and not (set(row['user_message_ids']) & mids):
            continue
        db.execute('''UPDATE learning_reviews SET status='deleted',draft='',revised_body=NULL,
            manifest='{}',preferences='[]',source_refs='[]',user_message_ids='[]',user_hashes='{}',
            turn='{}',audit='[]',attempt_token=NULL,reason='source_deleted',updated_at=? WHERE id=?''', (now, row['id']))
        db.execute("INSERT OR IGNORE INTO tombstones VALUES(?,?,?,?,?)", (uuid4().hex, 'learning_review', row['id'], row['draft_hash'], now))
        db.execute("UPDATE jobs SET status='cancelled',lease_until=NULL,error=NULL WHERE kind='learning_review' AND target=? AND version=?", (row['activity_id'], row['job_version']))
        # Idempotent even when normal derivative deletion already scrubbed it.
        db.execute("UPDATE messages SET body='',deleted=1,processing_state='deleted',error=NULL WHERE id=?", (row['message_id'],))
        db.execute('INSERT OR IGNORE INTO tombstones VALUES(?,?,?,?,?)', (uuid4().hex, 'derived_message', row['message_id'], row['published_hash'], now))
        removed.append(row['id'])
    return removed


def ledger(db):
    """Return recovery identity/state metadata, never candidate or audit text."""
    if not _exists(db, 'learning_reviews'):
        return []
    private = {'draft', 'revised_body', 'turn', 'manifest', 'preferences', 'audit', 'attempt_token'}
    return [{key: value for key, value in dict(row).items() if key not in private}
            for row in db.execute('SELECT * FROM learning_reviews ORDER BY rowid')]


def recover(db, entries=(), stamp=None):
    """Merge a monotonic ledger without manufacturing missing public messages.

    Restored pending work is held; it is not safe to replay it against newly
    restored context. Published history is preserved only when its public body
    is actually present and has the recorded digest.
    """
    initialize(db)
    now = stamp or _stamp()
    for item in entries:
        if not isinstance(item, dict) or item.get('status') not in STATES:
            raise ValueError('Invalid learning review recovery ledger')
        old = db.execute('SELECT * FROM learning_reviews WHERE id=?', (item['id'],)).fetchone()
        if old and old['status'] == 'deleted':
            continue
        if not old:
            values = dict(item)
            values.update(turn='{}', draft='', revised_body=None, manifest='{}', preferences='[]', audit='[]', attempt_token=None)
            keys = [r['name'] for r in db.execute('PRAGMA table_info(learning_reviews)')]
            db.execute('INSERT INTO learning_reviews (' + ','.join(keys) + ') VALUES (' + ','.join('?' for _ in keys) + ')', [values.get(k) for k in keys])
        elif item['status'] == 'deleted' or item['updated_at'] >= old['updated_at']:
            db.execute('UPDATE learning_reviews SET status=?,published_hash=?,reason=?,updated_at=? WHERE id=?', (item['status'], item.get('published_hash'), item.get('reason'), item['updated_at'], item['id']))
        if item['status'] == 'deleted':
            # Temporarily restore a nonterminal state so scrub also sanitizes a
            # newly inserted deletion ledger whose private fields are empty.
            db.execute("UPDATE learning_reviews SET status='cancelled' WHERE id=?", (item['id'],))
            scrub(db, message_ids=[item['message_id']], stamp=now)
    for raw in db.execute("SELECT * FROM learning_reviews WHERE status!='deleted'").fetchall():
        row = _decode(raw)
        erased = db.execute("SELECT 1 FROM tombstones WHERE target_type='learning_review' AND target_id=?", (row['id'],)).fetchone()
        erased = erased or db.execute("SELECT 1 FROM tombstones WHERE target_type IN ('message','derived_message') AND target_id=?", (row['message_id'],)).fetchone()
        if erased or any(_deleted_ref(db, ref) for ref in row['source_refs'] + ['native_session:' + row['session_id']]):
            scrub(db, message_ids=[row['message_id']], stamp=now)
            continue
        present = db.execute("SELECT body FROM messages WHERE id=? AND deleted=0 AND processing_state='learning_reviewed'", (row['message_id'],)).fetchone()
        intact = row['status'] == 'approved' and present and _hash(present['body']) == row['published_hash']
        if not intact and row['status'] not in ('rejected', 'cancelled'):
            db.execute("UPDATE learning_reviews SET status='recovery_hold',reason='restored_without_publishable_state',attempt_token=NULL,updated_at=? WHERE id=?", (now, row['id']))
        db.execute("UPDATE jobs SET status='recovery_hold',lease_until=NULL,error=NULL WHERE kind='learning_review' AND target=? AND version=? AND status NOT IN ('done','cancelled')", (row['activity_id'], row['job_version']))
    return ledger(db)
