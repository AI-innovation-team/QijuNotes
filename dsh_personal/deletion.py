"""Deletion of owned records and derived data; never erase unrelated user originals."""
import hashlib,json,sqlite3
from .core import dumps,uid

def apply_message_deletion(c,mid,stamp,fingerprint=None,original_body=None,*,source_refs=None):
    """Idempotent SQL-only cascade, shared by live deletion and old-snapshot restore.

    Returns identities for filesystem/native cleanup. The caller must hold an
    exclusive application checkpoint across SQL, records and file operations.
    """
    from .directive_cleanup import scrub_directives
    directive_sources=([mid] if mid else [ref[8:] for ref in (source_refs or []) if ref.startswith('message:')])
    cleaned=scrub_directives(c,directive_sources,stamp,source_refs=source_refs or ())
    m=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone() if source_refs is None else None
    empty={'activity_ids':[],'task_ids':[],'message_ids':[],'native_session_ids':[],'attachment_ids':[]}
    if not m and source_refs is None and not any(cleaned.values()):return empty
    if m:
        state=c.execute('SELECT session_state FROM activities WHERE id=?',(m['activity_id'],)).fetchone()
        if m['deleted'] and not original_body and state[0]!='deletion_pending':
            if not any(cleaned.values()):return empty
            # New directive data merged beside an already deleted source still
            # needs cleanup, without invalidating newer work in its activity.
            m=None
    body=original_body if original_body is not None else m['body'] if m else None
    fingerprint=fingerprint or (hashlib.sha256(body.encode()).hexdigest() if body else None)
    def tomb(kind,ident,fp=None):c.execute('INSERT OR IGNORE INTO tombstones VALUES(?,?,?,?,?)',(uid(),kind,ident,fp,stamp))
    if m:
        tomb('message',mid,fingerprint)
        c.execute("UPDATE messages SET body='',deleted=1,processing_state='deleted',error=NULL WHERE id=?",(mid,))
    aids=({m['activity_id']} if m else set())|set(cleaned['activity_ids']);task_ids=set(cleaned['task_ids']);native=set(cleaned['native_session_ids']);message_ids=set(cleaned['message_ids'])|({mid} if m else set());assets=set()
    # Explicit context provenance propagates deletion to sessions that received a
    # derived profile or a linked original. It is not inferred from message count.
    context_refs={'message:'+mid,'activity:'+m['activity_id']} if m else set(source_refs or (['message:'+mid] if mid else []))
    context_refs.update('task:'+tid for tid in task_ids)
    if not m:
        for ref in context_refs:tomb('context_ref',ref)
    tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'deletion_context_refs' in tables:
        context_refs.update(r[0] for r in c.execute('SELECT ref FROM deletion_context_refs WHERE message_id=?',(mid,)))
    if 'context_sources' in tables:
        for sid,ref in c.execute('SELECT session_id,source_ref FROM context_sources'):
            if ref in context_refs:native.add(sid)
    if native:
        for a in c.execute('SELECT id,session_id FROM activities'):
            if a['session_id'] in native:aids.add(a['id'])
        for r in c.execute("SELECT task_id,session_id FROM task_runs WHERE status!='source_deleted'"):
            if r['session_id'] in native:task_ids.add(r['task_id'])
    for aid in aids:
        a=c.execute('SELECT * FROM activities WHERE id=?',(aid,)).fetchone()
        if a['session_id']:native.add(a['session_id'])
        tomb('activity_derivatives',aid)
        # A feedback receipt depends only on its own input, not the native
        # conversation context. feedback.scrub below handles its exact source.
        for row in c.execute("SELECT id FROM messages WHERE activity_id=? AND role IN ('assistant','tool') AND processing_state!='local_prompt' AND id NOT LIKE 'feedback-receipt:%'",(aid,)).fetchall():
            tomb('derived_message',row['id']);message_ids.add(row['id'])
            c.execute("UPDATE messages SET body='',deleted=1,processing_state='deleted',error=NULL WHERE id=?",(row['id'],))
        c.execute('DELETE FROM summaries WHERE activity_id=?',(aid,))
        c.execute('DELETE FROM native_questions WHERE activity_id=?',(aid,))
        # Corrections are independently authored user originals. Lexical overlap
        # (especially short answers) is not evidence of a derivative relation.
        for row in c.execute('SELECT * FROM reflection_steps WHERE activity_id=?',(aid,)).fetchall():
            refs=[x for x in json.loads(row['source_ids']) if x!=mid]
            if refs:c.execute('UPDATE reflection_steps SET source_ids=? WHERE activity_id=? AND line=?',(dumps(refs),aid,row['line']))
            else:c.execute('DELETE FROM reflection_steps WHERE activity_id=? AND line=?',(aid,row['line']))
        c.execute("UPDATE jobs SET status='cancelled',error=NULL WHERE target=? OR target IN (SELECT id FROM messages WHERE activity_id=?)",(aid,aid))
        c.execute("UPDATE messages SET processing_state='deletion_hold',error='原话已保存；会话清理后未重放，如需执行请明确重新发送。' WHERE activity_id=? AND deleted=0 AND role='user' AND processing_state IN ('pending','awaiting_question_batch')",(aid,))
        c.execute("UPDATE activities SET session_state='deletion_pending',version=version+1 WHERE id=? AND session_state!='deletion_pending'",(aid,))
        task_ids.update(r[0] for r in c.execute('SELECT id FROM tasks WHERE source_activity_id=?',(aid,)))
    for tid in task_ids:
        tomb('task_derivatives',tid)
        t=c.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
        tomb('task_version',tid+':'+str(t['version']))
        c.execute("UPDATE tasks SET result=NULL,status='recovery_hold',error='关联原文已删除；旧成果失效，需明确重新委托后继续' WHERE id=?",(tid,))
        for r in c.execute('SELECT id,session_id,version FROM task_runs WHERE task_id=?',(tid,)):
            tomb('task_run',r['id']);tomb('task_version',tid+':'+str(r['version']))
            if r['session_id']:native.add(r['session_id'])
        c.execute("UPDATE task_runs SET body=NULL,status='source_deleted' WHERE task_id=?",(tid,))
    for row in c.execute('SELECT id,body,result FROM natural_requests').fetchall():
        if (body and body in row['body']) or any(aid in (row['result'] or '') for aid in aids):
            # This is a transport cache; its authoritative split messages remain.
            tomb('natural_request',row['id']);c.execute("UPDATE natural_requests SET body='',result=? WHERE id=?",(dumps({'message':'原请求涉及已删除资料；请明确提交新的内容。'}),row['id']))
    for row in c.execute('SELECT id FROM attachments WHERE message_id=?',(mid,)).fetchall():
        assets.add(row[0]);tomb('attachment',row[0])
    for sid in native:tomb('native_session',sid)
    from .learning import prune_learning_history
    from .learning_guard import scrub as scrub_learning_reviews
    scrub_learning_reviews(c,activity_ids=aids,message_ids=message_ids,source_refs=context_refs,stamp=stamp)
    prune_learning_history(c)
    from .chronicle import prune_chronicle_corrections
    prune_chronicle_corrections(c)
    from .feedback import scrub as scrub_feedback
    scrub_feedback(c,stamp)
    from .learning_editorial import scrub as scrub_learning_editorial
    scrub_learning_editorial(c)
    c.execute("INSERT OR REPLACE INTO meta VALUES('deletion_compaction_pending','true')")
    return {'activity_ids':sorted(aids),'task_ids':sorted(task_ids),'message_ids':sorted(message_ids),
            'native_session_ids':sorted(native),'attachment_ids':sorted(assets)}

def apply_source_invalidation(c,source_refs,stamp):
    """Invalidate sessions that received withdrawn imported/profile sources."""
    return apply_message_deletion(c,None,stamp,source_refs=source_refs)

def register_context(store,session_id,source_refs):
    with store.db(True) as c:
        c.executemany('INSERT OR IGNORE INTO context_sources VALUES(?,?)',[(session_id,ref) for ref in sorted(set(source_refs))])

def compact_deleted_data(store):
    """Requires caller checkpoint. Readers are not killed; unfinished cleanup is explicit."""
    try:
        with sqlite3.connect(store.path,timeout=.5,isolation_level=None) as c:
            c.execute('PRAGMA busy_timeout=500');c.execute('PRAGMA secure_delete=ON')
            if c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:return False
            c.execute('VACUUM')
            if c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:return False
        store.set_meta('deletion_compaction_pending','false')
        return True
    except sqlite3.OperationalError:return False

def deletion_plan(app,mid):
    from .directive_cleanup import directive_task_ids,requirement_version
    with app.store.db() as c:
        message=c.execute('SELECT activity_id,deleted FROM messages WHERE id=?',(mid,)).fetchone()
        if not message:raise KeyError('原文不存在')
        aid=message['activity_id']
        taskids=sorted({r[0] for r in c.execute('SELECT id FROM tasks WHERE source_activity_id=?',(aid,))}|directive_task_ids(c,mid))
    refs=app.records.dependent_refs(['message:'+mid,'activity:'+aid]+['task:'+t for t in taskids])
    with app.store.db() as c:
        known={r[0] for r in c.execute('SELECT DISTINCT session_id FROM context_sources')}
        native={r[0] for r in c.execute('SELECT session_id,source_ref FROM context_sources') if r[1] in refs}
        activities=[dict(r) for r in c.execute('SELECT id,session_id,version FROM activities')]
        runs=[dict(r) for r in c.execute('SELECT task_id,session_id FROM task_runs WHERE session_id IS NOT NULL')]
        # Older versions did not persist profile/context edges. Only when a
        # deleted profile could have been supplied globally, invalidate unknown
        # project sessions conservatively and disclose this in the plan.
        legacy=any(r.startswith('profile:') for r in refs)
        legacy_sids={r['session_id'] for r in activities+runs if r.get('session_id') and r['session_id'] not in known} if legacy else set()
        if legacy:legacy_sids.update(r[0] for r in c.execute("SELECT session_id FROM context_sources WHERE source_ref='legacy_profile_context'"))
        native.update(legacy_sids)
        aids={aid}|{a['id'] for a in activities if a['session_id'] in native}
        taskids=set(taskids)|{r['task_id'] for r in runs if r['session_id'] in native}
        taskids.update(r[0] for r in c.execute('SELECT id,source_activity_id FROM tasks') if r[1] in aids)
        native.update(a['session_id'] for a in activities if a['id'] in aids and a['session_id'])
        native.update(r['session_id'] for r in runs if r['task_id'] in taskids)
        assets=[r[0] for r in c.execute('SELECT id FROM attachments WHERE message_id=?',(mid,))]
        task_versions={r['id']:{'version':r['version'],'status':r['status'],'requirements_version':requirement_version(c,r['id'])} for r in c.execute('SELECT id,version,status FROM tasks') if r['id'] in taskids}
    plan={'message_id':mid,'activity_ids':sorted(aids),'task_ids':sorted(taskids),'native_session_ids':sorted(native),
          'attachment_ids':assets,'source_refs':refs,'legacy_session_ids':sorted(legacy_sids),
          'activity_versions':{a['id']:a['version'] for a in activities if a['id'] in aids},
          'task_versions':task_versions,
          'scope':'删除目标原话、直接附件和相关模型派生内容；关联任务暂停并失效旧成果。若该原话修订过任务需求，整个相关需求历史的文本副本与当前需求字段一并清除，修订编号保留。其他用户原话、独立修订、委托授权及评价原文保留，即使含相同文字；它们仍可用于后续对话。原生受影响会话离线清理后换新上下文，历史加密备份按保留期退出。'}
    plan['token']=hashlib.sha256(dumps(plan).encode()).hexdigest()
    return plan

def finish_affected_cleanup(app,affected):
    """Caller holds exclusive maintenance lock; clean files and rotate native IDs."""
    from pathlib import Path
    from .operations import _native_checkpoint
    from runtime.native_cleanup import purge_sessions
    s=app.store;result={}
    for path in (Path(app.data)/'cache').glob('*'):
        if path.is_file() and not path.is_symlink():path.unlink()
    native_error=None
    try:
        with _native_checkpoint(app,yield_lock=True) as checkpoint:
            native=purge_sessions(checkpoint['native'],affected['native_session_ids'],lock_fd=checkpoint['lock_fd'])
        with s.db(True) as c:
            for aid in affected['activity_ids']:
                a=c.execute('SELECT session_state FROM activities WHERE id=?',(aid,)).fetchone()
                if a and a[0]=='deletion_pending':
                    key='native_generation:'+aid;old=c.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
                    c.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(key,str(int(old[0])+1 if old else 1)))
                    c.execute("UPDATE activities SET session_id=NULL,session_state='pending' WHERE id=?",(aid,))
        result.update(native_cleanup_pending=False,native_cleanup=native)
    except Exception as exc:
        native_error=type(exc).__name__
        result.update(native_cleanup_pending=True,native_error=native_error)
    compacted=compact_deleted_data(s)
    result.update(sqlite_cleanup_pending=not compacted,remaining_user_originals_preserved=True,
                  message='目标原话及相关派生记录已处理；'+('原生清理仍待完成，相关会话保持隔离。' if native_error else '当前原生会话副本已逻辑清理，后续在新上下文继续。')+'历史加密备份按保留期退出；独立用户文件不自动删除。')
    return result

def protect_deletion_ledger(app,result):
    """Retryable protection; invoke after releasing the application checkpoint."""
    from .operations import protect_ledgers
    key=app.config().get('backup',{}).get('key_file')
    result['latest_ledger_protected']=False
    if key:
        try:protect_ledgers(app,key);result['latest_ledger_protected']=True
        except Exception:pass
    return result

def delete_owned_message(app,p):
    """Checkpointed maintenance for an explicit user deletion, never a background guess."""
    from pathlib import Path
    from .operations import maintenance_lock
    s=app.store;mid=p['message_id']
    completed=s.meta('deletion_done:'+mid)
    if completed:
        receipt=json.loads(completed)
        with maintenance_lock(app.data,exclusive=True):
            receipt['sqlite_cleanup_pending']=not compact_deleted_data(s) if s.meta('deletion_compaction_pending')=='true' else False
        protect_deletion_ledger(app,receipt)
        s.set_meta('deletion_done:'+mid,dumps(receipt))
        return receipt
    with maintenance_lock(app.data,exclusive=True):
        plan=deletion_plan(app,mid)
        if p.get('plan_token') and p['plan_token']!=plan['token']:raise ValueError('关联记录已经变化，请重新核对删除范围')
        # Establish owned dependencies before deleting the profiles themselves.
        with s.db(True) as c:
            c.executemany('INSERT OR IGNORE INTO deletion_context_refs VALUES(?,?)',[(mid,ref) for ref in plan['source_refs']])
            for sid in plan['legacy_session_ids']:
                ref='possible_legacy:'+mid
                c.execute('INSERT OR IGNORE INTO context_sources VALUES(?,?)',(sid,ref))
                c.execute('INSERT OR IGNORE INTO deletion_context_refs VALUES(?,?)',(mid,ref))
            affected=apply_message_deletion(c,mid,s.stamp())
            for ref in ['message:'+x for x in affected['message_ids']]+['activity:'+x for x in affected['activity_ids']]+['task:'+x for x in affected['task_ids']]:
                app.records.delete_source(ref,preserve_source=ref.startswith(('activity:','task:')),connection=c)
        result={'deleted':mid,'affected':affected}
        # Only directly message-owned attachments and application-managed cache.
        with s.db(True) as c:
            for ident in affected['attachment_ids']:
                row=c.execute('SELECT path FROM attachments WHERE id=?',(ident,)).fetchone()
                if row:
                    path=Path(app.data)/row[0]
                    if path.is_symlink() or not path.resolve().is_relative_to((Path(app.data)/'attachments').resolve()):raise ValueError('附件路径不属于本实例')
                    shared=c.execute('SELECT id FROM attachments WHERE path=? AND id!=?',(row[0],ident)).fetchall()
                    if any(other[0] not in affected['attachment_ids'] for other in shared):raise ValueError('附件文件还有独立原文引用，清理保持待处理')
                    path.unlink(missing_ok=True);c.execute('DELETE FROM attachments WHERE id=?',(ident,))
        result.update(finish_affected_cleanup(app,affected))
    protect_deletion_ledger(app,result)
    if not result['native_cleanup_pending']:s.set_meta('deletion_done:'+mid,dumps(result))
    return result
