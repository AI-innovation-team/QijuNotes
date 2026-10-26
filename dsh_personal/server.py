"""Authenticated project entry; immutable input receipts precede all model work."""
from __future__ import annotations
import argparse, base64, datetime as dt, fcntl, hashlib, hmac, ipaddress, json, os, secrets, signal, sqlite3, subprocess, threading, time, urllib.parse, uuid
from contextlib import ExitStack
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from . import __version__
from .core import Store, dumps, uid, TZ
from .records import RecordsStore
from .gateway import Gateway
from .worker import Worker
from .diagnostics import notification_readiness
from .auth import AuthStore, AuthError, RateLimitedError, AuthStorageError
from .calendar import Calendar
from .chronicle import Chronicle
from runtime.dsh_bridge import DSHBridge
ROOT=Path(__file__).resolve().parents[1]

def _hidden_ref(db, ref, seen=None):
    """Resolve provenance links without exposing an internally prepared lesson."""
    from .learning_schedule import is_hidden
    if not isinstance(ref,str):return False
    seen=set() if seen is None else seen
    if ref in seen:return False
    seen.add(ref)
    kind,_,ident=ref.partition(':')
    if kind=='activity':return is_hidden(db,ident)
    if kind in {'message','attachment','task','native_session','profile','growth'}:
        table,column={'message':('messages','id'),'attachment':('attachments','id'),
            'task':('tasks','id'),'native_session':('activities','session_id'),
            'profile':('records_profile','item_id'),'growth':('records_growth','item_id')}[kind]
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():return False
        for record in db.execute('SELECT * FROM '+table+' WHERE '+column+'=?',(ident,)):
            row=dict(record)
            if kind=='native_session' and is_hidden(db,row['id']):return True
            if _hidden_row(db,row,seen):return True
    return False

def _hidden_row(db,row,seen=None):
    from .learning_schedule import is_hidden
    if any(is_hidden(db,row.get(key)) for key in ('activity_id','source_activity_id') if row.get(key)):return True
    refs=[]
    for key in ('scope','source_ref'):
        if row.get(key):refs.append(row[key])
    values=row.get('source_refs',[])
    if isinstance(values,str):
        try:values=json.loads(values)
        except ValueError:values=[values]
    if isinstance(values,list):refs.extend(values)
    for key,kind in (('message_id','message'),('task_id','task')):
        if row.get(key):refs.append(kind+':'+row[key])
    if row.get('parent_id'):refs.append(row['parent_id'] if ':' in row['parent_id'] else 'growth:'+row['parent_id'])
    return any(_hidden_ref(db,ref,seen) for ref in refs)

class Application:
    def __init__(self,data,clock=None,web_root=None):
        self.web_root=Path(web_root).resolve() if web_root else ROOT/'web'
        self.data=Path(data).expanduser().resolve();self.data.mkdir(parents=True,exist_ok=True,mode=0o700);os.chmod(self.data,0o700)
        if (self.data/'upgrade-in-progress.json').exists():raise RuntimeError('升级切换尚未完成，拒绝自动启动；请核对release检查点或回退')
        existing=self.data/'personal.sqlite'
        if existing.exists():
            with sqlite3.connect('file:'+urllib.parse.quote(str(existing))+'?mode=ro',uri=True) as prior:
                try:version=prior.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                except sqlite3.DatabaseError:version=None
            if not version or version[0]!='1':raise RuntimeError('数据库格式不兼容；请先执行已验证的release迁移，不自动修改旧库')
        self.store=Store(self.data/'personal.sqlite',clock);self.records=RecordsStore(self.data/'personal.sqlite',clock)
        self.calendar=Calendar(self.store);self.chronicle=Chronicle(self.store,self.records)
        self.config_path=self.data/'config.json';self.sessions={};self.leases={};self.login_attempts={};self.auth=AuthStore(self.data)
        if not self.config_path.exists():
            self.config_path.write_text(dumps({'public_url':'http://127.0.0.1:18780','model':{},'notification':{},'backup':{}})+'\n');os.chmod(self.config_path,0o600)
        for name in ['access-token','gateway-key']:
            p=self.data/name
            if not p.exists():p.write_text(secrets.token_urlsafe(32));os.chmod(p,0o600)
        self.access_token=(self.data/'access-token').read_text().strip()
        self.gateway_key=(self.data/'gateway-key').read_text().strip()
        self.bridge=DSHBridge(dsh_home=self.data/'dsh')
        self.worker=Worker(self.store,self.bridge,self.records,self.config)
        self.worker.calendar=self.calendar
    def config(self):return json.loads(self.config_path.read_text())
    def session_valid(self,sid):
        if not sid:return False
        try:
            if self.auth.configured():return self.auth.valid_session(sid)
            return self.sessions.get(sid,0)>time.time()
        except AuthStorageError:return False
    def visible_rows(self,rows):
        with self.store.db() as db:return [row for row in rows if not _hidden_row(db,row)]
    def require_visible(self,ref):
        with self.store.db() as db:
            if _hidden_ref(db,ref):raise KeyError('活动尚未发布')
    def activity(self,aid):
        self.require_visible('activity:'+aid)
        return self.store.activity(aid)
    def _check_action_visibility(self,p):
        # Check before special deletion paths too; possession of an internal ID
        # is never permission to answer, attach to, revise or delete preparation.
        if p.get('op')=='ensure_activity':return
        refs=[]
        for key,kind in (('activity_id','activity'),('source_activity_id','activity'),
                         ('message_id','message'),('task_id','task'),('profile_id','profile'),('parent_id','growth')):
            if p.get(key):refs.append(kind+':'+p[key])
        if p.get('op') in {'profile_save','profile_disable','growth_save'}:
            ident=p.get('item_id') or p.get('id')
            if ident:refs.append(('growth:' if p['op']=='growth_save' else 'profile:')+ident)
        for key in ('source_ref','scope'):
            if isinstance(p.get(key),str):refs.append(p[key])
        if isinstance(p.get('source_refs'),str):refs.append(p['source_refs'])
        elif isinstance(p.get('source_refs'),list):refs.extend(p['source_refs'])
        if isinstance(p.get('source_ids'),list):refs.extend('message:'+mid for mid in p['source_ids'] if isinstance(mid,str))
        with self.store.db() as db:
            if any(_hidden_ref(db,ref) for ref in refs):raise KeyError('活动尚未发布')
    def display_activities(self, rows):
        # Only unresolved initial lessons need the richer processing view. Keep
        # scheduling state unchanged, and do not label a published lesson failed
        # because a later follow-up was rejected.
        for row in rows:
            row['display_status']=row['status']
            if row['routine']=='A1' and not row.get('ready_at'):
                row['display_status']=self.store.activity(row['id'])['activity']['display_status']
        return rows
    def state(self,day=None,query='',journal_mode='day',week_date=None,calendar_start=None,calendar_end=None,view='full'):
        if view not in ('full','chat'):raise ValueError('未知状态视图')
        day=day or self.store.today();dt.date.fromisoformat(day)
        if self.store.meta('isolated')=='true' or self.records.is_quarantined():
            return {'today':self.store.today(),'date':day,'activities':[],'bookmarks':[],'schedules':[],'gaps':[],'tasks':[],'journal':[],'profiles':[],'growth':[],'imports':[],'budget':self.store.budget(),'health':{'version':__version__,'isolated':True,'error':'恢复隔离中：请核对最新修订/删除与动作账本，再显式开放检索与执行'}}
        with self.store.db() as c:
            from .learning_schedule import is_hidden
            activities=[dict(r) for r in c.execute('SELECT * FROM activities WHERE business_date=? ORDER BY routine',(day,)) if not is_hidden(c,r['id'])]
            recent=[dict(r) for r in c.execute("SELECT id,routine,business_date,status,ready_at FROM activities WHERE routine='CHAT' ORDER BY business_date DESC,created_at DESC LIMIT 60") if not is_hidden(c,r['id'])]
            archived=[dict(r) for r in c.execute("SELECT id,routine,business_date,status,ready_at FROM activities WHERE routine IN ('A1','A2','A3') ORDER BY business_date DESC") if not is_hidden(c,r['id'])]
            pending=c.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('pending','failed')").fetchone()[0]
            errors=[dict(r) for r in c.execute("SELECT kind,error FROM jobs WHERE status='failed' LIMIT 10")]
        activities=self.display_activities(activities);archived=self.display_activities(archived)
        projects=[{'routine':r,'name':name,'count':sum(x['routine']==r for x in archived),'activities':[x for x in archived if x['routine']==r]} for r,name in [('A1','每日一学'),('A3','睡眠打卡'),('A2','每日反思')]]
        deliveries=[]
        for profile in self.visible_rows(self.records.list_profile(scope='learning_delivery')):
            try:payload=json.loads(profile['content'])
            except (ValueError,TypeError):continue
            deliveries.append({**payload,'id':profile['item_id'],'source_refs':profile['source_refs']})
        projects[0]['historical_deliveries']=deliveries
        if view=='chat':
            # Chat navigation needs the live sidebar, not every private module.
            # Return fresh empty modules instead of caching an earlier complete
            # response, so switching views cannot revive removed material.
            return {'today':self.store.today(),'date':day,'activities':activities,
                'recent_activities':recent,'routine_projects':projects,
                'chronicle':[],'chronicle_settings':self.chronicle.settings(),
                'calendar_events':[],'bookmarks':[],'schedules':self.store.schedules(),
                'gaps':[],'tasks':[],'weekly_review':None,'journal':[],
                'profiles':[],'growth':{'items':[]},'imports':[],
                'budget':self.store.budget(),'health':{'version':__version__,
                    'isolated':False,'model_error':self.store.meta('model_error'),
                    'pending':pending,'errors':errors,'worker_error':self.store.meta('worker_error')}}
        config=self.config()
        notification=notification_readiness(self.data,config)
        if notification['real_device_verified']:
            verified_at=dt.datetime.fromisoformat(notification['verified_at']).isoformat()
            notification_status=f"已通过一次真机锁屏接收及正确活动跳转（验证时间：{verified_at}）。{notification['note']}"
        elif notification['configured']:notification_status='通知通道已配置，待真机锁屏接收及活动跳转验证。'
        else:notification_status='仅页面可见，通知通道未配置。'
        journal_day=None if query or journal_mode=='all' else 'unknown' if journal_mode=='undated' else day
        imported=self.records.imported_messages(query=query,day=journal_day,limit=None)
        profiles=[p for p in self.records.list_profile() if p['scope']!='chronicle' and not p['scope'].startswith(('date_annotation','activity:','learning_delivery'))];growth=self.records.growth()
        growth['items']=self.visible_rows(growth['items']);growth['versions']=self.visible_rows(growth['versions'])
        if growth['current_state'] and not self.visible_rows([growth['current_state']]):growth['current_state']=None
        weekly=self.records.weekly_review(week_date or day)
        weekly['lines']={line:self.visible_rows(rows) for line,rows in weekly['lines'].items()}
        weekly['activity_evidence']=self.visible_rows(weekly['activity_evidence'])
        chronicle=self.history()
        from .routine_settings import state as routine_settings_state
        from .learning import source_options
        with self.store.db() as db:
            routine_settings=routine_settings_state(db)
            learning_source_options=source_options(db,self.store.today())
        return {'today':self.store.today(),'date':day,'activities':activities,'recent_activities':recent,'routine_projects':projects,'chronicle':chronicle,'routine_settings':routine_settings,'learning_source_options':learning_source_options,'chronicle_settings':self.chronicle.settings(),'calendar_events':self.calendar.events(day[:7],start=calendar_start,end=calendar_end),'bookmarks':self.bookmarks(),'schedules':self.store.schedules(),'gaps':self.store.gaps(day),'tasks':self.visible_rows(self.store.tasks()),'weekly_review':weekly,'journal':([] if journal_mode=='undated' else self.visible_rows(self.store.journal(journal_day,query)))+[{**m,'source':'import'} for m in imported],'profiles':self.visible_rows(profiles),'growth':growth,'imports':self.records.list_batches(),'budget':self.store.budget(),'health':{'version':__version__,'model':config.get('model',{}).get('name','待配置'),'model_error':self.store.meta('model_error'),'model_context':'仅当前会话及所选作用域的有效资料；历史不会全量发送','notification':notification_status,'backup':self.store.meta('last_backup','尚无备份'),'pending':pending,'errors':errors,'last_success':self.store.meta('model_last_success'),'storage_bytes':sum(p.stat().st_size for p in self.data.rglob('*') if p.is_file()),'isolated':self.store.meta('isolated')=='true','observation':{'started_at':self.store.meta('observation_started'),'required_days':7,'status':'观察中，未验收'},'worker_error':self.store.meta('worker_error')}}
    def bookmarks(self):
        from .learning_schedule import is_hidden
        with self.store.db() as c:rows=[dict(r) for r in c.execute('SELECT * FROM activities WHERE bookmarked=1 ORDER BY business_date DESC') if not is_hidden(c,r['id'])]
        return self.display_activities(rows)
    def history(self,day=None,query='',include_candidates=True):
        entries=self.chronicle.entries(day,query,include_candidates)
        for entry in self.calendar.completed_entries():
            entry['body']=self.chronicle.settings()['user_title']+entry['body']
            if (not day or day==entry['business_date']) and query.casefold() in entry['body'].casefold():entries.append(entry)
        return sorted(self.visible_rows(entries),key=lambda x:(x['business_date'] or '',x['recorded_at']),reverse=True)
    def source(self,ref):
        if self.store.meta('isolated')=='true' or self.records.is_quarantined():raise PermissionError('恢复隔离中，历史检索尚未开放')
        self.require_visible(ref)
        kind,_,ident=ref.partition(':')
        if kind=='activity':return self.store.activity(ident)
        if kind=='task':return self.store.task(ident)
        if kind=='profile':return self.records.get_profile(ident)
        if kind=='growth':return self.records.get_growth(ident)
        if kind=='calendar':return self.calendar.get(ident)
        if kind=='message':
            with self.store.db() as c:r=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(ident,)).fetchone()
            if r:return dict(r)
        if kind in {'import','message'}:
            for m in self.records.imported_messages(limit=None):
                if m.get('message_id',m.get('id'))==ident:return m
        raise KeyError('来源不存在或已删除')
    def action(self,p,session=None):
        from .operations import maintenance_lock
        self._check_action_visibility(p)
        if p.get('op')=='delete_message':
            if self.store.meta('isolated')=='true':raise PermissionError('恢复隔离中，不允许修改原文')
            from .deletion import delete_owned_message
            return delete_owned_message(self,p)
        if p.get('op')=='import_remove':
            if self.store.meta('isolated')=='true':raise PermissionError('恢复隔离中，不允许修改原文')
            from .import_cleanup import remove_import_batch
            return remove_import_batch(self,p)
        if p.get('op') in {'profile_save','profile_disable'}:
            with maintenance_lock(self.data,exclusive=True):return self._action(p,session)
        with maintenance_lock(self.data):return self._action(p,session)
    def _action(self,p,session=None):
        op=p.get('op');s=self.store
        self._check_action_visibility(p)
        if s.meta('isolated')=='true' and op not in {'export'}:raise PermissionError('恢复实例处于隔离核对状态；请先合并最新修订与动作账本')
        if op=='chronicle_settings':return self.chronicle.save_settings(p['user_title'])
        if op=='chronicle_compiled_revise':
            from .materials import revise_compiled
            return revise_compiled(self.records,p['profile_id'],p.get('body',''),p['expected_version'],p.get('excluded',False))
        if op=='chronicle_revise':return self.chronicle.revise_entry(p['activity_id'],p.get('body',''),p.get('source_ids',[]),p['expected_version'],excluded=p.get('excluded',False))
        if op=='chronicle_import_revise':return self.chronicle.confirm_import_entry(p['message_id'],p.get('body',''),p.get('expected_version',0),excluded=p.get('excluded',False))
        if op=='calendar_save':return self.calendar.save(**{k:v for k,v in p.items() if k!='op'})
        if op=='calendar_status':return self.calendar.change_status(p['event_id'],p['expected_version'],p['status'])
        if op=='ensure_activity':return {'activity':s.ensure_activity(p['routine'],p.get('date'))}
        if op=='message':
            prior_activity=s.activity(p['activity_id'])['activity']
            from .interaction import is_local_control
            control_message=is_local_control(p['body'],prior_activity['routine'])
            receipt=s.submit(p['activity_id'],p['submission_id'],p['body'],p.get('call_id'),p.get('question_id'),control_message=control_message)
            # Only first committed delivery interprets explicit commands; retry reuses original receipt.
            if not receipt['duplicate'] or receipt['processing_state']=='command_pending':
                from .interaction import command
                try:result=command(s,p['body'],p['activity_id'],p['submission_id'])
                except (ValueError,PermissionError) as error:
                    if not control_message:raise
                    result={'message':'未更新设置：'+str(error)}
                if result:
                    with s.db(True) as c:
                        control_id=result.get('receipt') if str(result.get('receipt','')).startswith('routine-settings:') else 'local-command:'+receipt['receipt']
                        c.execute("INSERT OR IGNORE INTO messages VALUES(?,?,NULL,'assistant',?,?, 'local_control',NULL,NULL,0)",(control_id,p['activity_id'],result['message'],s.stamp()))
                        if result.get('routine_settings'):
                            c.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('routine_settings_display:'+control_id,dumps({'receipt_id':control_id,'versions':{routine:record['version'] for routine,record in result['routine_settings'].items()}})))
                        c.execute("UPDATE jobs SET status='done' WHERE kind='handoff' AND target=?",(receipt['receipt'],))
                        c.execute("UPDATE messages SET processing_state='command_applied' WHERE id=?",(receipt['receipt'],))
                        if not control_message and prior_activity['routine'] in {'A2','A3'} and p['body'].strip() not in {'跳过本次','今天先不聊','跳过今天','今天到这里','结束本次','继续','继续本次'}:
                            for row in c.execute('SELECT line,source_ids FROM reflection_steps WHERE activity_id=?',(p['activity_id'],)).fetchall():
                                refs=[ref for ref in json.loads(row['source_ids']) if ref!=receipt['receipt']]
                                if refs:c.execute('UPDATE reflection_steps SET source_ids=? WHERE activity_id=? AND line=?',(dumps(refs),p['activity_id'],row['line']))
                                else:c.execute('DELETE FROM reflection_steps WHERE activity_id=? AND line=?',(p['activity_id'],row['line']))
                            c.execute('UPDATE activities SET status=?,current_step=? WHERE id=? AND version=?',(prior_activity['status'],prior_activity['current_step'],p['activity_id'],prior_activity['version']+1))
                            c.execute('UPDATE activities SET version=version+1 WHERE id=?',(p['activity_id'],))
                    receipt['command']=result
                    receipt['processing_state']='command_applied'
                elif control_message:
                    # A conservative detector must not strand a conversation if
                    # a future grammar revision declines its candidate.
                    with s.db(True) as c:
                        c.execute("UPDATE messages SET processing_state='pending' WHERE id=?",(receipt['receipt'],))
                        s._enqueue(c,'handoff',receipt['receipt'])
            return receipt
        if op=='activity_control':return s.control(p['activity_id'],p['action'],remind_at=p.get('remind_at'),value=p.get('value'))
        if op=='natural':
            from .interaction import command
            return command(s,p['body'],p.get('activity_id'),p.get('submission_id')) or s.natural(p['body'],p.get('submission_id'))
        if op=='routine_settings':
            from .routine_settings import save_settings
            return save_settings(s,p['routine'],p['settings'],p['expected_version'],p['submission_id'],'本人通过内容设置提交')
        if op=='schedule':return s.change_schedule(p['routine'],p['time'],p['weekdays'],p.get('paused_until'),p['source'],submission_id=p.get('submission_id'),expected_updated_at=p.get('expected_updated_at'))
        if op=='correction':return s.correct(p['activity_id'],p['fields'],p.get('source','用户在更正控件提交'))
        if op=='presented':
            lease=self.leases.get(p.get('lease'))
            if not lease or lease['session']!=session or not p.get('visible'):return {'suppressed':False}
            at=dt.datetime.fromisoformat(p['interaction_at'].replace('Z','+00:00'))
            elapsed=(s.now()-at).total_seconds()
            if not 0<=elapsed<=300:return {'suppressed':False}
            with s.db(True) as c:
                c.execute("UPDATE notifications SET state='foreground_presented',receipt=? WHERE activity_id=? AND kind='first' AND state='pending' AND EXISTS (SELECT 1 FROM activities a WHERE a.id=notifications.activity_id AND a.ready_at IS NOT NULL)",(dumps({'lease':p['lease'],'at':s.stamp()}),p['activity_id']))
                changed=c.execute('SELECT changes()').fetchone()[0]
                if changed:c.execute("UPDATE activities SET notification_state='foreground_presented' WHERE id=?",(p['activity_id'],))
            return {'suppressed':bool(changed)}
        if op=='task_create':return s.create_task(**{k:v for k,v in p.items() if k!='op'})
        if op=='task_review':
            task=s.review_task(p['task_id'],int(p['version']),p['action'],p['input'])
            if p['action']=='cancel':
                for run in task['deliveries']:
                    if run.get('session_id') and run['version']==int(p['version']):
                        try:self.bridge.cancel_processing(run['session_id'])
                        except Exception:
                            with s.db(True) as c:c.execute('UPDATE tasks SET error=? WHERE id=?',('已取消未来触发；原生计算取消结果未知，请核对',p['task_id']))
            return s.task(p['task_id'])
        if op=='task_run':
            with s.db(True) as c:
                t=c.execute('SELECT * FROM tasks WHERE id=?',(p['task_id'],)).fetchone()
                if not t or t['status']=='running':raise ValueError('任务不存在或正在执行')
                if t['status']=='cancelled':raise ValueError('任务已取消，请明确重新委托')
                c.execute("UPDATE tasks SET status='queued',run_at=? WHERE id=?",(s.stamp(),p['task_id']))
            return s.task(p['task_id'])
        if op in {'import_preview','import_apply'}:
            method=self.records.import_preview if op=='import_preview' else self.records.import_apply
            result=method(p['source'],p['content'],filename=p.get('filename',''),kind=p.get('kind','conversation'),scope=p.get('scope','imported'),selected_message_ids=p.get('selected_message_ids'),**({'preview_token':p.get('preview_token')} if op=='import_apply' else {}))
            return result
        if op=='import_removal_plan':
            from .import_cleanup import plan_import_removal
            return plan_import_removal(self,p['batch_id'])
        if op=='profile_save':
            opts=dict(content=p['content'],category=p.get('category','background'),scope=p.get('scope','global'),source_refs=p.get('source_refs') or ([p['source_ref']] if p.get('source_ref') else []),confirmed=bool(p.get('confirmed')),valid_until=p.get('valid_until'))
            if (p.get('item_id') or p.get('id')):
                old=self.records.get_profile((p.get('item_id') or p['id']));return self.records.revise_profile((p.get('item_id') or p['id']),p.get('expected_version',p.get('version',old['version'])),p.get('user_input') or p.get('source','用户修订画像'),**opts)
            return self.records.add_profile(**opts,user_input=p.get('user_input') or p.get('source') or p['content'])
        if op=='profile_disable':
            old=self.records.get_profile((p.get('item_id') or p['id']));return self.records.disable_profile((p.get('item_id') or p['id']),old['version'],'用户停用长期画像；不删除原话')
        if op=='growth_save':
            return self.records.put_growth(p['line'],'plan' if p['kind']=='revision' else p['kind'],p['content'],source_refs=p.get('source_refs') or ([p['source_ref']] if p.get('source_ref') else []),parent_id=p.get('parent_id'),item_id=(p.get('item_id') or p.get('id')),expected_version=p.get('expected_version',p.get('version')),user_input=p.get('user_input') or ('用户明确提交成长记录：'+dumps(p['content'])),business_date=p.get('business_date'))
        if op=='deletion_plan':
            from .deletion import deletion_plan
            return deletion_plan(self,p['message_id'])
        if op=='retry':
            from .learning_guard import public_status as review_status,retry as retry_review
            from .learning_schedule import is_hidden
            with s.db() as c:
                review_ids=[r[0] for r in c.execute("SELECT id FROM activities WHERE routine='A1'"+(" AND id=?" if p.get('activity_id') else ''),([p['activity_id']] if p.get('activity_id') else [])) if not is_hidden(c,r[0])]
            guarded={aid for aid in review_ids if review_status(s,aid,include_approved=True)}
            for aid in guarded:retry_review(s,aid)
            if p.get('activity_id') in guarded:
                return {'message':review_status(s,p['activity_id'],include_approved=True)['message']}
            with s.db(True) as c:
                # A learning preparation may fail before any user message exists.
                # Its original native request is reconciled by native_retry;
                # selecting it here must not synthesize a new user reply.
                query="SELECT DISTINCT a.id FROM activities a WHERE a.session_state='ready' AND (EXISTS (SELECT 1 FROM messages m WHERE m.activity_id=a.id AND m.processing_state='accepted_by_dsh') OR (a.routine='A1' AND a.status='preparing' AND a.ready_at IS NULL))"
                params=[]
                if p.get('activity_id'):query+=' AND a.id=?';params=[p['activity_id']]
                for row in c.execute(query,params).fetchall():
                    if row['id'] not in guarded and not is_hidden(c,row['id']):s._enqueue(c,'native_retry',row['id'],int(s.now().timestamp()))
            with s.db(True) as c:
                if p.get('activity_id'):
                    c.execute("UPDATE jobs SET status='pending',attempts=0,run_after=? WHERE status='failed' AND (target=? OR target IN (SELECT id FROM messages WHERE activity_id=?))",(s.stamp(),p['activity_id'],p['activity_id']))
                else:
                    for job in c.execute("SELECT id,target FROM jobs WHERE status='failed'").fetchall():
                        if job['target'] not in guarded and not any(_hidden_ref(c,kind+':'+job['target']) for kind in ('activity','message')):c.execute("UPDATE jobs SET status='pending',attempts=0,run_after=? WHERE id=?",(s.stamp(),job['id']))
            return {'message':'失败步骤已重新排队；原文不会重复提交'}
        if op=='cleanup':
            removed=0
            for pth in (self.data/'cache').glob('*'):
                if pth.is_file():pth.unlink();removed+=1
            return {'removed_regenerable_files':removed,'message':'只清理缓存；原文、待答问题和附件保留'}
        if op=='attachment_upload':
            raw=base64.b64decode(p['content_base64'],validate=True)
            if len(raw)>10_000_000:raise ValueError('附件上限 10 MB')
            if p.get('activity_id'):s.activity(p['activity_id'])
            aid=uid();directory=self.data/'attachments';directory.mkdir(exist_ok=True,mode=0o700)
            file=directory/aid;file.write_bytes(raw);os.chmod(file,0o600)
            name=Path(p['filename']).name
            with s.db(True) as c:c.execute('INSERT INTO attachments VALUES(?,?,?,?,?,?,?,?,?)',(aid,p.get('activity_id'),None,None,name,'attachments/'+aid,hashlib.sha256(raw).hexdigest(),len(raw),s.stamp()))
            return {'id':aid,'name':name,'size':len(raw)}
        raise ValueError('未知操作：'+str(op))

def create_server(app,host='127.0.0.1',port=18780):
    class Handler(BaseHTTPRequestHandler):
        server_version='DSHPersonal/1.1'
        def log_message(self,*args):pass
        def send(self,status,data,ctype='application/json; charset=utf-8',headers=None):
            raw=dumps(data).encode() if not isinstance(data,bytes) else data
            self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','same-origin');self.send_header('Content-Security-Policy',"default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
            for k,v in (headers or {}).items():self.send_header(k,v)
            self.end_headers()
            try:self.wfile.write(raw)
            except (BrokenPipeError,ConnectionResetError):pass
        def identity(self):
            jar=cookies.SimpleCookie()
            try:jar.load(self.headers.get('Cookie',''))
            except cookies.CookieError:return None
            token=jar.get('dsh_session');sid=token.value if token else None
            if app.session_valid(sid):return sid
            return None
        def local_account_setup(self):
            try:
                host=urllib.parse.urlsplit('http://'+self.headers.get('Host','')).hostname
                return ipaddress.ip_address(self.client_address[0]).is_loopback and host in {'localhost','127.0.0.1','::1'} and not any(self.headers.get(h) for h in ('Forwarded','X-Forwarded-For','X-Forwarded-Host'))
            except ValueError:return False
        def session_response(self,result):
            age=f"; Max-Age={result['max_age']}" if result['max_age'] is not None else ''
            secure='; Secure' if self.headers.get('Origin','').startswith('https://') else ''
            return self.send(200,{'ok':True},headers={'Set-Cookie':f"dsh_session={result['session_id']}; HttpOnly; SameSite=Strict; Path=/"+age+secure})
        def query(self):
            u=urllib.parse.urlsplit(self.path);return u.path,{k:v[-1] for k,v in urllib.parse.parse_qs(u.query).items()}
        def do_GET(self):
            path,q=self.query()
            if path=='/api/auth/status':
                try:
                    status=app.auth.status(self.identity());status['setup_available']=not status['configured'] and self.local_account_setup()
                    return self.send(200,status)
                except AuthStorageError:return self.send(503,{'error':'账号存储暂不可用，请在本机检查后重试。'})
            if path.startswith('/api/') and not self.identity():return self.send(401,{'error':'请先登录'})
            try:
                if path=='/api/state':return self.send(200,app.state(q.get('date'),q.get('q',''),q.get('journal_mode','day'),q.get('week_date'),q.get('calendar_start'),q.get('calendar_end'),q.get('view','full')))
                if app.store.meta('isolated')=='true' and path in {'/api/source','/api/activity','/api/attachment','/api/export','/api/calendar','/api/chronicle'}:return self.send(403,{'error':'恢复隔离中；最新账本核对前不开放历史资料'})
                if path=='/api/calendar':return self.send(200,app.calendar.events(q.get('month'),start=q.get('start'),end=q.get('end')))
                if path=='/api/feedback':
                    from .feedback import view
                    return self.send(200,view(app.store))
                if path=='/api/study':
                    from .study import view
                    return self.send(200,view(app.store))
                if path=='/api/chronicle':return self.send(200,app.history(q.get('date'),q.get('q','')))
                if path=='/api/source':return self.send(200,app.source(q['ref']))
                if path=='/api/activity':return self.send(200,app.activity(q['id']))
                if path=='/api/events':
                    sid=self.identity();lease=uid();app.leases[lease]={'session':sid}
                    self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Cache-Control','no-store');self.end_headers()
                    try:
                        self.wfile.write(('event: hello\ndata: '+dumps({'lease':lease})+'\n\n').encode());self.wfile.flush()
                        seen=set()
                        while app.session_valid(sid):
                            with app.store.db() as c:
                                from .learning_schedule import is_hidden
                                notices=[dict(r) for r in c.execute("SELECT a.id,a.routine,a.business_date,a.ready_at FROM activities a JOIN notifications n ON n.activity_id=a.id WHERE n.state='pending' AND n.kind='first' AND a.ready_at IS NOT NULL") if not is_hidden(c,r['id'])]
                            for notice in notices:
                                if notice['id'] not in seen:
                                    self.wfile.write(('event: activity\ndata: '+dumps(notice)+'\n\n').encode());seen.add(notice['id'])
                            self.wfile.write(b': connected\n\n');self.wfile.flush();time.sleep(2)
                    except (BrokenPipeError,ConnectionResetError):pass
                    finally:app.leases.pop(lease,None)
                    return
                if path=='/api/attachment':
                    app.require_visible('attachment:'+q['id'])
                    with app.store.db() as c:a=c.execute('SELECT * FROM attachments WHERE id=?',(q['id'],)).fetchone()
                    if not a:raise KeyError('附件不存在')
                    raw=(app.data/a['path']).read_bytes()
                    return self.send(200,raw,'application/octet-stream',{'Content-Disposition':"attachment; filename*=UTF-8''"+urllib.parse.quote(a['name'])})
                if path=='/api/export':
                    from .operations import export_bundle, markdown_export
                    if q.get('format')=='chronicle':
                        entries=app.history(include_candidates=False)
                        lines=['# '+app.chronicle.settings()['user_title']+'的个人史','']
                        for entry in entries:lines.extend(['## '+(entry['business_date'] or '日期未知'),entry['body'],'来源：'+', '.join(entry['source_refs']),''])
                        return self.send(200,'\n'.join(lines).encode(),'text/markdown; charset=utf-8',{'Content-Disposition':'attachment; filename="personal-history.md"'})
                    if q.get('format')=='markdown':return self.send(200,markdown_export(app).encode(),'text/markdown; charset=utf-8',{'Content-Disposition':'attachment; filename="diary.md"'})
                    return self.send(200,export_bundle(app),'application/zip',{'Content-Disposition':'attachment; filename="dsh-personal-export.zip"'})
                files={'/':'index.html','/index.html':'index.html','/app.js':'app.js','/theme.js':'theme.js','/styles.css':'styles.css','/style.css':'style.css','/manifest.json':'manifest.json','/favicon.svg':'icon.svg','/icon.svg':'icon.svg'}
                files['/qiju-avatar.png']='qiju-avatar.png'
                if path in {'/guide/materials','/guide/material-template','/guide/material-example','/guide/chatgpt-material-instructions','/guide/kimi-material-instructions','/guide/material-batch-example'}:
                    paths={'/guide/materials':'docs/MATERIAL_PREPARATION.md','/guide/material-template':'docs/templates/personal-material-template.md','/guide/material-example':'docs/templates/personal-material-example.json','/guide/chatgpt-material-instructions':'docs/templates/chatgpt-material-instructions.md','/guide/kimi-material-instructions':'docs/templates/kimi-material-instructions.md','/guide/material-batch-example':'docs/templates/personal-material-batch-example.json'}
                    return self.send(200,(ROOT/paths[path]).read_bytes(),'text/plain; charset=utf-8')
                if path not in files:return self.send(404,{'error':'不存在'})
                p=app.web_root/files[path]
                if not p.exists():return self.send(503,{'error':'界面正在部署'})
                ctype={'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8','.svg':'image/svg+xml','.json':'application/json','.png':'image/png'}[p.suffix]
                return self.send(200,p.read_bytes(),ctype)
            except (KeyError,ValueError) as exc:self.send(400,{'error':str(exc)})
            except Exception as exc:self.send(500,{'error':'读取失败：'+type(exc).__name__})
        def do_POST(self):
            path,_=self.query()
            try:
                # Custom header + exact origin blocks cross-site state mutation; no CORS support.
                if self.headers.get('X-DSH-Client')!='web':return self.send(403,{'error':'缺少同源客户端标记'})
                origin=self.headers.get('Origin')
                if origin and urllib.parse.urlsplit(origin).netloc!=self.headers.get('Host'):return self.send(403,{'error':'跨站请求被拒绝'})
                if self.headers.get('Content-Type','').split(';')[0]!='application/json':return self.send(415,{'error':'需要 JSON'})
                n=int(self.headers.get('Content-Length','0'))
                if not 0<n<=15_000_000:return self.send(413,{'error':'请求体大小无效'})
                if path in {'/api/login','/api/auth/setup','/api/auth/password'} and n>8192:return self.send(413,{'error':'账号请求过大'})
                p=json.loads(self.rfile.read(n))
                if not isinstance(p,dict):raise ValueError('请求需要是对象')
                if path=='/api/auth/setup':
                    if origin!='http://'+self.headers.get('Host','') or not self.local_account_setup():return self.send(403,{'error':'首次设置请在这台电脑打开本机网页完成。'})
                    result=app.auth.setup(p.get('username'),p.get('password'),remember=p.get('remember',True) is True)
                    app.sessions.clear()
                    return self.session_response(result)
                if path=='/api/login':
                    if app.auth.configured():
                        return self.session_response(app.auth.login(p.get('username'),p.get('password'),self.client_address[0],remember=p.get('remember',True) is True))
                    ip=self.client_address[0];attempts=[t for t in app.login_attempts.get(ip,[]) if t>time.time()-60]
                    if len(attempts)>=10:return self.send(429,{'error':'尝试过多，请一分钟后重试'})
                    app.login_attempts[ip]=attempts+[time.time()]
                    if not hmac.compare_digest(str(p.get('token','')),app.access_token):return self.send(401,{'error':'访问口令不正确'})
                    sid=secrets.token_urlsafe(32);app.sessions[sid]=time.time()+86400*7
                    secure='; Secure' if origin and origin.startswith('https://') else ''
                    return self.send(200,{'ok':True},{ } if False else 'application/json; charset=utf-8',{'Set-Cookie':f'dsh_session={sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age=604800'+secure})
                sid=self.identity()
                if not sid:return self.send(401,{'error':'请先登录'})
                if path=='/api/auth/password':
                    return self.session_response(app.auth.change_password(sid,p.get('current_password'),p.get('new_password'),remember=p.get('remember',True) is True,peer=self.client_address[0]))
                if path=='/api/logout':
                    if app.auth.configured():app.auth.logout(sid)
                    app.sessions.pop(sid,None);return self.send(200,{'ok':True},headers={'Set-Cookie':'dsh_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict'})
                if path!='/api/action':return self.send(404,{'error':'不存在'})
                return self.send(200,app.action(p,sid))
            except RateLimitedError as exc:self.send(429,{'error':str(exc)},headers={'Retry-After':str(exc.retry_after)})
            except AuthStorageError:self.send(503,{'error':'账号存储暂不可用，请在本机检查后重试。'})
            except AuthError as exc:self.send(401,{'error':str(exc)})
            except PermissionError as exc:self.send(403,{'error':str(exc)})
            except (KeyError,ValueError,TypeError) as exc:self.send(400,{'error':str(exc)})
            except Exception as exc:
                app.store.observe('http_error',{'type':type(exc).__name__})
                self.send(500,{'error':'操作未完成，请保留草稿并重试：'+type(exc).__name__})
    return ThreadingHTTPServer((host,port),Handler)

def run_servers(app,host,port,private_host=None,gateway_port=18782,no_worker=False):
    """Bind every entry point before allowing durable work to be claimed."""
    with ExitStack() as cleanup:
        server=create_server(app,host,port)
        cleanup.callback(server.server_close)
        private_server=None
        if private_host:
            private_server=create_server(app,private_host,port)
            cleanup.callback(private_server.server_close)
        gateway=Gateway(app.store,app.config_path,app.gateway_key).serve(gateway_port)
        cleanup.callback(gateway.server_close)
        cleanup.callback(gateway.shutdown)
        if private_server:
            threading.Thread(target=private_server.serve_forever,daemon=True).start()
            cleanup.callback(private_server.shutdown)
        if not no_worker:
            cleanup.callback(app.worker.stop.set)
            app.worker.start()
        print(f'DSH Personal {__version__} listening on http://{host}:{port}',flush=True)
        server.serve_forever()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--data',default=str(Path.home()/'.local/share/dsh-personal'));parser.add_argument('--host',default='127.0.0.1');parser.add_argument('--port',type=int,default=18780);parser.add_argument('--private-host');parser.add_argument('--no-worker',action='store_true');parser.add_argument('--gateway-port',type=int,default=18782);parser.add_argument('--web-root');args=parser.parse_args()
    app=Application(args.data,web_root=args.web_root)
    try:commit=subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True,stderr=subprocess.DEVNULL).strip()
    except (OSError,subprocess.SubprocessError):commit='uncommitted-development'
    app.store.set_meta('running_source_commit',commit)
    lock=(app.data/'application.lock').open('a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise SystemExit('另一个个人系统宿主正在写入该数据目录')
    from .diagnostics import boot_evidence
    app.store.observe('process_start',boot_evidence(commit))
    run_servers(app,args.host,args.port,args.private_host,args.gateway_port,args.no_worker)
if __name__=='__main__':main()
