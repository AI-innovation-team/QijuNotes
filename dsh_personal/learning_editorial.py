"""Operator application of a separately reviewed, source-preserving correction.

There is no HTTP/model tool endpoint. Callers supply the actual model audit;
this layer rechecks it against current data under the application checkpoint.
"""
import hashlib
import json

from .core import dumps, uid
from . import learning_guard as guard
from .learning_content import (build_packet, audit_payload, premise_payload,
    reference_audit_payload, validate_reference_audit, COMBINED_REFERENCE_SYSTEM,
    AUDIT_REFERENCE_SYSTEM, PREMISE_REFERENCE_SYSTEM)


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def check(body, manifest, users, mode, reviews):
    payload=audit_payload(build_packet(manifest,users),body,mode)
    expected=[('full',COMBINED_REFERENCE_SYSTEM if mode=='followup' else AUDIT_REFERENCE_SYSTEM,payload)]
    focused=premise_payload(payload) if mode=='lesson' else None
    if focused is not None:
        expected.append(('premise',PREMISE_REFERENCE_SYSTEM,focused))
    if not isinstance(reviews,list) or len(reviews)!=len(expected):
        raise ValueError('缺少完整实际审稿记录')
    records=[]
    for supplied,(stage,system,current) in zip(reviews,expected):
        if supplied.get('stage')!=stage or supplied.get('policy_sha256')!=sha(system):
            raise ValueError('审稿策略或顺序已变化')
        checked=validate_reference_audit(reference_audit_payload(current),supplied.get('result'))
        if not checked['passed']:
            raise ValueError('编辑稿尚未通过当前来源核对：'+dumps(checked['issues']))
        records.append({**supplied,'checked':checked})
    return records


def publish_followup(store,cid,body,reviews,*,expected_body_sha256):
    from .operations import maintenance_lock
    with maintenance_lock(store.path.parent,exclusive=True):
        row=guard.snapshot(store,cid)
        if not row or row['mode']!='followup' or row['status']!='rejected' or not row['still_valid']:
            raise ValueError('原追问候选已变化，未发布编辑稿')
        old_body=row['revised_body'] or row['draft']
        if sha(old_body)!=expected_body_sha256:
            raise ValueError('原回复内容已变化')
        with store.db() as db:
            users=[dict(db.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()) for mid in row['user_message_ids']]
        checked=check(body,row['manifest'],users,'followup',reviews)
        if not guard.retry(store,row['activity_id'],candidate_id=cid):
            raise ValueError('追问状态不再允许继续')
        active=guard.record_attempt(store,cid)
        if not active:
            raise ValueError('追问来源已变化')
        token=active['attempt_token']
        guard.record_attempt(store,cid,{'stage':'operator_editorial_revision','previous_body':old_body,
            'previous_body_sha256':sha(old_body),'edited_body_sha256':sha(body),'reviews':checked},
            revised_body=body,attempt_token=token)
        result=guard.commit(store,cid,True,revised_body=body,attempt_token=token,
            audit={'stage':'publication_decision','method':'actual_model_review_and_operator_edit',
                   'checked':{'passed':True,'issues':[]}})
        if result.get('published'):
            with store.db(True) as db:
                db.execute("UPDATE jobs SET status='done',lease_until=NULL,error=NULL WHERE kind='learning_review' AND target=? AND version=?",(row['activity_id'],row['job_version']))
        return result


def publish_lesson_correction(store,original_id,body,reviews,*,expected_original_sha256,expected_preferences_hash):
    from .operations import maintenance_lock
    with maintenance_lock(store.path.parent,exclusive=True):
        with store.db(True) as db:
            original=db.execute("SELECT * FROM messages WHERE id=? AND deleted=0 AND role='assistant'",(original_id,)).fetchone()
            if not original or sha(original['body'])!=expected_original_sha256:
                raise ValueError('原课已变化或删除')
            aid=original['activity_id']
            activity=db.execute("SELECT * FROM activities WHERE id=? AND routine='A1' AND session_state='ready'",(aid,)).fetchone()
            if not activity or store.meta('isolated')=='true':
                raise ValueError('当前会话不可发布修订')
            if (guard._exists(db,'records_meta') and db.execute("SELECT 1 FROM records_meta WHERE key='quarantine' AND value='1'").fetchone()):
                raise ValueError('恢复记录处于隔离状态')
            if guard._preference_hash(guard._profiles(db,store.today()))!=expected_preferences_hash:
                raise ValueError('当前学习要求已变化，未发布旧编辑稿')
            raw=db.execute('SELECT value FROM meta WHERE key=?',('learning_selection:'+aid,)).fetchone()
            if not raw:
                raise ValueError('缺少原课来源快照')
            manifest=json.loads(raw[0])
            if (manifest.get('delivery') or {}).get('message_id')!=original_id:
                raise ValueError('指定消息不是该课原始交付')
            checked=check(body,manifest,[],'lesson',reviews)
            key='learning_editorial:'+original_id
            raw=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
            revisions=json.loads(raw[0]) if raw else []
            if revisions and revisions[-1]['body_sha256']==sha(body):
                return revisions[-1]
            version=len(revisions)+2;mid='learning-editorial:'+uid();stamp=store.stamp()
            public=f"## {activity['business_date']} 课程 · 修订版 {version}\n\n"+body
            db.execute("INSERT INTO messages VALUES(?,?,NULL,'assistant',?,?,'learning_edited',NULL,NULL,0)",(mid,aid,public,stamp))
            row={'message_id':mid,'original_id':original_id,'version':version,'created_at':stamp,
                 'original_sha256':expected_original_sha256,'body_sha256':sha(body),'public_body_sha256':sha(public),
                 'source_manifest_hash':guard._manifest_hash(manifest),'reason':'按用户反馈去除防御套话并校订教学内容'}
            revisions.append(row)
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(key,dumps(revisions)))
            db.execute('INSERT INTO meta VALUES(?,?)',('learning_editorial_audit:'+mid,dumps(checked)))
            return row


def scrub(db):
    for raw in db.execute("SELECT key,value FROM meta WHERE key LIKE 'learning_editorial:%'").fetchall():
        revisions=json.loads(raw['value'])
        retained=[]
        for row in revisions:
            present=all(db.execute('SELECT 1 FROM messages WHERE id=? AND deleted=0',(row[key],)).fetchone() for key in ('original_id','message_id'))
            if present:
                retained.append(row)
            else:
                db.execute('DELETE FROM meta WHERE key=?',('learning_editorial_audit:'+row['message_id'],))
        if retained:
            db.execute('UPDATE meta SET value=? WHERE key=?',(dumps(retained),raw['key']))
        else:
            db.execute('DELETE FROM meta WHERE key=?',(raw['key'],))
