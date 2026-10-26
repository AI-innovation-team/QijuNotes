"""Owner-configured curricula and reflection dimensions, versioned in SQLite.

Configuration is not a fact about the owner. Activity snapshots preserve the
questions in force when an activity began, including historical backfills.
"""
from __future__ import annotations
import copy
import hashlib
import json
import re
from .core import dumps, uid

PREFIX = 'routine_settings:'
REVISION = 'routine_settings_revision:'
REQUEST = 'routine_settings_request:'
REFLECTION = 'reflection_dimensions:'
DEFAULTS = {
    'A1': {'topics': {'classic': '', 'history': '', 'modern': ''}, 'teaching_request': ''},
    'A2': {'dimensions': [
        {'id': 'doctorate', 'label': '博士学位', 'prompt': '进展、认识、约束与下一步', 'keywords': ['博士', '实验', '论文']},
        {'id': 'startup', 'label': '科研创业', 'prompt': '产品、商业验证与下一步', 'keywords': ['创业', '产品', '商业']},
        {'id': 'culture', 'label': '文化事业', 'prompt': '阅读、写作、作品与体会', 'keywords': ['文化', '阅读', '写作', '作品']},
    ]},
}


def _text(value, limit, name, required=False):
    if not isinstance(value, str) or len(value.strip()) > limit or (required and not value.strip()):
        raise ValueError(f'{name}须为{1 if required else 0}–{limit}字文本')
    return value.strip()


def validate(routine, value):
    if routine not in DEFAULTS or not isinstance(value, dict):
        raise ValueError('仅支持每日一学和每日反思内容设置')
    if routine == 'A1':
        if set(value) - {'topics', 'teaching_request'}:
            raise ValueError('未知学习设置字段')
        topics = value.get('topics', {})
        if not isinstance(topics, dict) or set(topics) - {'classic', 'history', 'modern'}:
            raise ValueError('学习主题分类无效')
        return {'topics': {k: _text(topics.get(k, ''), 160, '主题') for k in DEFAULTS['A1']['topics']},
                'teaching_request': _text(value.get('teaching_request', ''), 4000, '教学要求')}
    if set(value) != {'dimensions'} or not isinstance(value['dimensions'], list) or not 1 <= len(value['dimensions']) <= 12:
        raise ValueError('请设置1–12个反思方面')
    dimensions = []
    for item in value['dimensions']:
        if not isinstance(item, dict) or set(item) - {'id', 'label', 'prompt', 'keywords'}:
            raise ValueError('反思方面格式无效')
        label = _text(item.get('label'), 80, '方面名称', True)
        ident = item.get('id') or 'custom_' + hashlib.sha256(label.encode()).hexdigest()[:12]
        if not isinstance(ident, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', ident):
            raise ValueError('反思方面编号无效')
        words = item.get('keywords', [label]) or [label]
        if not isinstance(words, list) or not 1 <= len(words) <= 20:
            raise ValueError('每个方面最多20个关键词')
        words = list(dict.fromkeys([_text(w, 80, '关键词', True) for w in words]))
        dimensions.append({'id': ident, 'label': label, 'prompt': _text(item.get('prompt', ''), 1000, '反思提示'), 'keywords': words})
    if len({x['id'] for x in dimensions}) != len(dimensions) or len({x['label'] for x in dimensions}) != len(dimensions):
        raise ValueError('反思方面的名称和编号不能重复')
    return {'dimensions': dimensions}


def read_settings(db, routine):
    if routine not in DEFAULTS:
        raise ValueError('未知内容设置')
    row = db.execute('SELECT value FROM meta WHERE key=?', (PREFIX + routine,)).fetchone()
    return json.loads(row[0]) if row else {'routine': routine, 'version': 0, 'settings': copy.deepcopy(DEFAULTS[routine]), 'updated_at': None}


def state(db):
    return {r: read_settings(db, r) for r in DEFAULTS}


def save_settings(store, routine, settings, expected_version, submission_id, source):
    settings = validate(routine, settings)
    if type(expected_version) is not int or expected_version < 0:
        raise ValueError('设置版本无效，请刷新后再试')
    submission_id = _text(submission_id, 200, '提交编号', True)
    source = _text(source, 12000, '本人设置来源', True)
    fingerprint = hashlib.sha256(dumps([routine, settings, expected_version, source]).encode()).hexdigest()
    request_key = REQUEST + submission_id
    with store.db(True) as db:
        if db.execute("SELECT 1 FROM meta WHERE key='isolated' AND value='true'").fetchone():
            raise PermissionError('恢复隔离中，不能更改内容设置')
        old = db.execute('SELECT value FROM meta WHERE key=?', (request_key,)).fetchone()
        if old:
            request = json.loads(old[0])
            if request.get('deleted'):
                raise ValueError('该请求来源已删除，请重新提交')
            if request['fingerprint'] != fingerprint:
                raise ValueError('提交编号已用于其他设置')
            return {**request['result'], 'duplicate': True}
        before = read_settings(db, routine)
        if before['version'] != expected_version:
            raise ValueError('设置已在其他窗口更新，请刷新后再修改')
        now = store.stamp(); version = before['version'] + 1
        mid = db.execute("SELECT id FROM messages WHERE submission_id=? AND role='user' AND deleted=0", (submission_id,)).fetchone()
        record = {'routine': routine, 'version': version, 'settings': settings, 'updated_at': now,
                  'source': source, 'source_message_id': mid[0] if mid else None}
        effective = ('尚未发布且未展开讨论的预备课程及之后的新课程；已发布或已讨论的课程保留原选材' if routine == 'A1'
                     else '之后新建的每日反思（含补填）；已开始的反思保留当时的方面，可继续深入')
        missing = []
        if routine == 'A1':
            from .learning import topic_availability
            missing = topic_availability(db, settings, store.today())
        name = '每日一学' if routine == 'A1' else '每日反思'
        labels={'classic':'经典古籍','history':'中国史与党史','modern':'现代概念'}
        detail = ('；'.join(f'{labels[k]}：{v or "自动选材"}' for k, v in settings['topics'].items()) + ('；教学要求：' + settings['teaching_request'] if settings['teaching_request'] else '')) if routine == 'A1' else '、'.join(d['label'] for d in settings['dimensions'])
        message = f'{name}设置已保存 v{expected_version} → v{version}：{detail}。生效范围：{effective}。'
        if missing:
            message += ' 当前部分主题缺少匹配的已取得原文，课程将保留待补材料状态；可从设置中的可用资料选题或清空主题恢复自动选材。'
        result = {**record, 'receipt': 'routine-settings:' + uid(), 'message': message, 'effective': effective,
                  'missing_topics': missing, 'duplicate': False}
        db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (PREFIX + routine, dumps(record)))
        db.execute('INSERT INTO meta VALUES(?,?)', (REVISION + routine + ':' + str(version), dumps(record)))
        db.execute('INSERT INTO meta VALUES(?,?)', (request_key, dumps({'fingerprint': fingerprint, 'result': result})))
        # A settings version participates in preparation jobs and publication
        # validity. The single scheduler performs generation outside this write.
        if routine == 'A1':
            from .learning_schedule import _refresh_requirements
            from .learning import source_identity_frozen
            for row in db.execute("SELECT id FROM activities WHERE routine='A1' AND ready_at IS NULL AND session_state!='deletion_pending'").fetchall():
                if not source_identity_frozen(db,row[0]):
                    _refresh_requirements(db, store, row[0])
                    db.execute("DELETE FROM meta WHERE key=?",('learning_topic_gap:'+row[0],))
                    db.execute("UPDATE jobs SET status='cancelled',error='主题设置已更新' WHERE kind='learning' AND target=? AND status='material_wait'",(row[0],))
        return result


def reflection_snapshot(db, aid, *, create=False):
    row = db.execute('SELECT value FROM meta WHERE key=?', (REFLECTION + aid,)).fetchone()
    if row:
        return json.loads(row[0])
    # Legacy activities always used the original three dimensions. Never apply
    # today's customized labels retroactively to those historical steps.
    value = read_settings(db, 'A2') if create else {'version': 0, 'settings': copy.deepcopy(DEFAULTS['A2'])}
    if create:
        db.execute('INSERT INTO meta VALUES(?,?)', (REFLECTION + aid, dumps(value)))
    return value


def ledger(db):
    return [dict(r) for r in db.execute("SELECT key,value FROM meta WHERE key LIKE 'routine_settings:%' OR key LIKE 'routine_settings_revision:%' OR key LIKE 'routine_settings_request:%' OR key LIKE 'routine_settings_display:%' OR key LIKE 'reflection_dimensions:%' OR key LIKE 'schedule_request:%' OR key LIKE 'activity_control_request:%'")]


def merge_ledger(db, rows):
    for row in rows:
        key = row.get('key', '')
        if not key.startswith((PREFIX, REVISION, REQUEST, REFLECTION, 'schedule_request:', 'routine_settings_display:', 'activity_control_request:')):
            continue
        value = json.loads(row['value'])
        old = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        if key.startswith(PREFIX):
            validate(value['routine'], value['settings'])
            if old and json.loads(old[0])['version'] >= value['version']:
                continue
        elif old:
            # Tombstoned request/revision content always dominates an old copy.
            if not value.get('deleted') or json.loads(old[0]).get('deleted'):
                continue
        db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, row['value']))


def scrub_deleted_sources(db, source_ids, stamp):
    """Remove copied commands and snapshots; version watermarks prevent revival."""
    deleted=set(source_ids)
    deleted.update(r[0] for r in db.execute("SELECT target_id FROM tombstones WHERE target_type IN ('message','derived_message')"))
    affected={'activity_ids':set(),'task_ids':set(),'message_ids':set(),'native_session_ids':set(),'attachment_ids':set()}
    rows=[(r['key'],json.loads(r['value'])) for r in db.execute("SELECT key,value FROM meta WHERE key LIKE 'routine_settings_revision:%'")]
    routines={value['routine'] for key,value in rows if value.get('source_message_id') in deleted and not value.get('deleted')}
    for routine in routines:
        current=read_settings(db,routine);watermark=current['version']
        # Later edits can carry fields copied from the withdrawn command. Clear
        # that lineage rather than reconstructing private text from older data.
        reset={'routine':routine,'version':watermark+1,'settings':copy.deepcopy(DEFAULTS[routine]),
               'updated_at':stamp,'source':'原设置来源已删除，已恢复默认内容设置','source_message_id':None}
        db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(PREFIX+routine,dumps(reset)))
        db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(REVISION+routine+':'+str(reset['version']),dumps(reset)))
        for key,value in rows:
            if value['routine']==routine and value['version']<=watermark:
                db.execute('UPDATE meta SET value=? WHERE key=?',(dumps({'routine':routine,'version':value['version'],'deleted':True}),key))
        for row in db.execute("SELECT key,value FROM meta WHERE key LIKE 'routine_settings_request:%'").fetchall():
            request=json.loads(row['value']);result=request.get('result',{})
            if result.get('routine')!=routine or result.get('version',0)>watermark:continue
            receipt=result.get('receipt')
            if receipt:
                from .directive_cleanup import scrub_receipt
                scrub_receipt(db,receipt,stamp,affected)
            db.execute('UPDATE meta SET value=? WHERE key=?',(dumps({'fingerprint':request['fingerprint'],'deleted':True}),row['key']))
        for row in db.execute("SELECT key,value FROM meta WHERE key LIKE 'routine_settings_display:%'").fetchall():
            display=json.loads(row['value'])
            if display.get('deleted') or not 0<display.get('versions',{}).get(routine,0)<=watermark:continue
            from .directive_cleanup import scrub_receipt
            scrub_receipt(db,display['receipt_id'],stamp,affected)
            db.execute('UPDATE meta SET value=? WHERE key=?',(dumps({'receipt_id':display['receipt_id'],'deleted':True}),row['key']))
        for row in db.execute("SELECT key,value FROM meta WHERE key LIKE 'reflection_dimensions:%' OR key LIKE 'learning_selection:%' OR key LIKE 'learning_selection_archive:%'").fetchall():
            value=json.loads(row['value'])
            if routine=='A2' and row['key'].startswith(REFLECTION) and value.get('version',0)<=watermark:
                if not value.get('version'):continue
                aid=row['key'][len(REFLECTION):];affected['activity_ids'].add(aid)
                db.execute('UPDATE meta SET value=? WHERE key=?',(dumps({'version':0,'settings':copy.deepcopy(DEFAULTS['A2']),'deleted':True}),row['key']))
                for message in db.execute("SELECT id FROM messages WHERE activity_id=? AND processing_state='local_prompt'",(aid,)).fetchall():
                    from .directive_cleanup import scrub_receipt
                    scrub_receipt(db,message[0],stamp,affected)
            elif routine=='A1' and row['key'].startswith(('learning_selection:','learning_selection_archive:')) and value.get('routine_settings_version',0)<=watermark:
                if not value.get('routine_settings_version'):continue
                affected['activity_ids'].add(value['activity_id'])
                if row['key'].startswith('learning_selection_archive:'):
                    db.execute('DELETE FROM meta WHERE key=?',(row['key'],))
                else:
                    value['routine_settings']=copy.deepcopy(DEFAULTS['A1'])
                    value['routine_settings_version']=reset['version']
                    db.execute('UPDATE meta SET value=? WHERE key=?',(dumps(value),row['key']))
        for aid in list(affected['activity_ids']):
            row=db.execute('SELECT session_id FROM activities WHERE id=?',(aid,)).fetchone()
            if row and row[0]:affected['native_session_ids'].add(row[0])
        if routine=='A1':
            db.execute("DELETE FROM meta WHERE key LIKE 'learning_topic_gap:%'")
    return affected
