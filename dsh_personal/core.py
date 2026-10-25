"""Transactional business layer. The DSH host owns native session execution."""
from __future__ import annotations
import contextlib, datetime as dt, hashlib, json, os, re, sqlite3, uuid
from pathlib import Path
from zoneinfo import ZoneInfo
TZ = ZoneInfo('Asia/Shanghai')
UTC = dt.timezone.utc
ROUTINES = {'A1':'21:00','A2':'21:30','A3':'08:30'}
LABELS = {'A1':'每日一学','A2':'每日反思','A3':'每日打卡','CHAT':'日常交流'}
def uid(): return str(uuid.uuid4())
def dumps(value): return json.dumps(value,ensure_ascii=False,separators=(',',':'))
def obj(row): return dict(row) if row else None

class BudgetRequestTooLarge(RuntimeError):
    """A complete request cannot fit even an otherwise empty budget period."""


class Store:
    def __init__(self, path, clock=None):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True)
        self.clock=clock or (lambda: dt.datetime.now(UTC))
        self.initialize()
    def now(self):
        n=self.clock(); return n.astimezone(UTC) if n.tzinfo else n.replace(tzinfo=UTC)
    def stamp(self): return self.now().astimezone(UTC).isoformat()
    def today(self): return self.now().astimezone(TZ).date().isoformat()
    @contextlib.contextmanager
    def db(self, write=False):
        c=sqlite3.connect(self.path,timeout=30,isolation_level=None)
        c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');c.execute('PRAGMA busy_timeout=30000');c.execute('PRAGMA secure_delete=ON')
        if write:c.execute('BEGIN IMMEDIATE')
        try:
            yield c
            if write:c.commit()
        except BaseException:
            if write:c.rollback()
            raise
        finally:c.close()
    def initialize(self):
        with self.db() as c:
            c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA synchronous=FULL')
            c.executescript('''
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS activities(id TEXT PRIMARY KEY,user_id TEXT NOT NULL DEFAULT 'owner',routine TEXT NOT NULL,business_date TEXT NOT NULL,status TEXT NOT NULL,session_id TEXT,session_state TEXT NOT NULL DEFAULT 'pending',version INTEGER NOT NULL DEFAULT 0,correction_version INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,scheduled_at TEXT,triggered_at TEXT,ready_at TEXT,origin TEXT NOT NULL,notification_state TEXT NOT NULL DEFAULT 'page_only',bookmarked INTEGER NOT NULL DEFAULT 0,current_step TEXT,UNIQUE(user_id,routine,business_date));
            CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,activity_id TEXT NOT NULL REFERENCES activities(id),submission_id TEXT UNIQUE,role TEXT NOT NULL,body TEXT NOT NULL,received_at TEXT NOT NULL,processing_state TEXT NOT NULL,native_id TEXT,error TEXT,deleted INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS schedules(routine TEXT PRIMARY KEY,time TEXT NOT NULL,weekdays TEXT NOT NULL,paused_until TEXT,updated_at TEXT NOT NULL,source TEXT NOT NULL,effective_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS schedule_events(id TEXT PRIMARY KEY,routine TEXT NOT NULL,before_json TEXT,after_json TEXT NOT NULL,source TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS natural_requests(id TEXT PRIMARY KEY,body TEXT NOT NULL,business_date TEXT NOT NULL,result TEXT,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS task_requests(id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,task_id TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS context_sources(session_id TEXT NOT NULL,source_ref TEXT NOT NULL,PRIMARY KEY(session_id,source_ref));
            CREATE TABLE IF NOT EXISTS deletion_context_refs(message_id TEXT NOT NULL,ref TEXT NOT NULL,PRIMARY KEY(message_id,ref));
            CREATE TABLE IF NOT EXISTS native_questions(activity_id TEXT PRIMARY KEY,questions TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS message_targets(message_id TEXT PRIMARY KEY,call_id TEXT NOT NULL,question_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reflection_steps(activity_id TEXT NOT NULL,line TEXT NOT NULL,state TEXT NOT NULL,source_ids TEXT NOT NULL,PRIMARY KEY(activity_id,line));
            CREATE TABLE IF NOT EXISTS summaries(activity_id TEXT PRIMARY KEY REFERENCES activities(id),content TEXT NOT NULL,input_version INTEGER NOT NULL,correction_version INTEGER NOT NULL,created_at TEXT NOT NULL,state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS corrections(id TEXT PRIMARY KEY,activity_id TEXT NOT NULL,fields TEXT NOT NULL,source TEXT NOT NULL,created_at TEXT NOT NULL,version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,target TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,run_after TEXT NOT NULL,lease_until TEXT,error TEXT,UNIQUE(kind,target,version));
            CREATE TABLE IF NOT EXISTS notifications(id TEXT PRIMARY KEY,activity_id TEXT NOT NULL,kind TEXT NOT NULL,state TEXT NOT NULL,run_after TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,receipt TEXT,error TEXT,created_at TEXT NOT NULL,UNIQUE(activity_id,kind));
            CREATE TABLE IF NOT EXISTS usage(id TEXT PRIMARY KEY,kind TEXT NOT NULL,day TEXT NOT NULL,month TEXT NOT NULL,reserved INTEGER NOT NULL,actual INTEGER,state TEXT NOT NULL,created_at TEXT NOT NULL,receipt TEXT);
            CREATE TABLE IF NOT EXISTS usage_details(reservation_id TEXT PRIMARY KEY REFERENCES usage(id),model TEXT NOT NULL,output_limit_field TEXT NOT NULL,output_limit INTEGER NOT NULL,stream INTEGER NOT NULL,started_at TEXT NOT NULL,finished_at TEXT,reported_total INTEGER,usage_json TEXT NOT NULL,response_state TEXT NOT NULL,usage_state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,goal TEXT NOT NULL,owner TEXT NOT NULL,inputs TEXT NOT NULL,deliverable TEXT NOT NULL,acceptance TEXT NOT NULL,source_activity_id TEXT,status TEXT NOT NULL,run_at TEXT NOT NULL,interval_days INTEGER,version INTEGER NOT NULL DEFAULT 0,result TEXT,error TEXT,created_at TEXT NOT NULL,authorization TEXT NOT NULL,catchup_policy TEXT NOT NULL DEFAULT 'before_deadline',deadline TEXT);
            CREATE TABLE IF NOT EXISTS task_runs(id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),version INTEGER NOT NULL,status TEXT NOT NULL,session_id TEXT,body TEXT,created_at TEXT NOT NULL,finished_at TEXT,UNIQUE(task_id,version));
            CREATE TABLE IF NOT EXISTS task_reviews(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,version INTEGER NOT NULL,action TEXT NOT NULL,input TEXT NOT NULL,actor TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tombstones(id TEXT PRIMARY KEY,target_type TEXT NOT NULL,target_id TEXT NOT NULL,fingerprint TEXT,created_at TEXT NOT NULL,UNIQUE(target_type,target_id));
            CREATE TABLE IF NOT EXISTS attachments(id TEXT PRIMARY KEY,activity_id TEXT,task_id TEXT,message_id TEXT,name TEXT NOT NULL,path TEXT NOT NULL,sha256 TEXT NOT NULL,size INTEGER NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY AUTOINCREMENT,at TEXT NOT NULL,kind TEXT NOT NULL,details TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS msg_activity ON messages(activity_id,received_at);
            CREATE INDEX IF NOT EXISTS msg_received ON messages(received_at);
            ''')
            from .directives import initialize as initialize_directives
            from .task_requirements import initialize as initialize_requirements
            initialize_directives(c)
            initialize_requirements(c)
            from .learning_guard import initialize as initialize_learning_guard
            initialize_learning_guard(c)
            from .learning_schedule import initialize as initialize_learning_schedule
            initialize_learning_schedule(c)
            from .study import initialize as initialize_study
            initialize_study(c)
            c.execute("INSERT OR IGNORE INTO meta VALUES('schema_version','1')")
            c.execute("INSERT OR IGNORE INTO meta VALUES('started_on',?)",(self.today(),))
            c.execute("INSERT OR IGNORE INTO meta VALUES('observation_started',?)",(self.stamp(),))
            c.execute("INSERT OR IGNORE INTO meta VALUES('isolated','false')")
            for r,t in ROUTINES.items():
                c.execute('INSERT OR IGNORE INTO schedules VALUES(?,?,?,NULL,?,?,?)',(r,t,dumps(list(range(7))),self.stamp(),'SPEC 1.1 用户确认每日默认','1970-01-01T00:00:00+00:00'))
        os.chmod(self.path,0o600)
    def meta(self,key,default=None):
        with self.db() as c:r=c.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        return r[0] if r else default
    def set_meta(self,key,value):
        with self.db(True) as c:c.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(key,str(value)))
    def observe(self,kind,details):
        with self.db(True) as c:c.execute('INSERT INTO observations(at,kind,details) VALUES(?,?,?)',(self.stamp(),kind,dumps(details)))
    def _enqueue(self,c,kind,target,version=0):
        c.execute('INSERT OR IGNORE INTO jobs(id,kind,target,version,run_after) VALUES(?,?,?,?,?)',(uid(),kind,target,version,self.stamp()))
    def ensure_activity(self,routine,day=None,origin='manual',scheduled_at=None):
        if routine not in {*ROUTINES,'CHAT'}:raise ValueError('未知活动类型')
        day=day or self.today();dt.date.fromisoformat(day)
        if day>self.today():raise ValueError('请选择今天或历史日期')
        with self.db(True) as c:
            a=c.execute('SELECT * FROM activities WHERE routine=? AND business_date=?',(routine,day)).fetchone()
            if a:
                if origin=='manual':
                    from .learning_schedule import activate_manual
                    activate_manual(c,self,a['id'])
                    a=c.execute('SELECT * FROM activities WHERE id=?',(a['id'],)).fetchone()
                return obj(a)
            aid=uid();now=self.stamp();historical=day<self.today()
            content={'A2':'今天在博士学位这条主线上，有哪些进展、认识或约束？可以只谈当前最想谈的一项，也可以同时谈科研创业和文化事业。','A3':'昨晚到今早睡得怎么样？睡觉时间、起床时间或主观质量，任选一项说说即可。若用 1–5 分：1 很差，5 很好；运动和情绪选填。','CHAT':'这里可以记录想法、讨论问题或明确委托任务。','A1':'学习材料正在准备；生成完成后会保留出处与实际生成时间。'}[routine]
            if routine in {'A2', 'A3'}:
                from .conversation_style import initial_prompt
                content = initial_prompt(routine)
            ready=None if routine=='A1' else now
            c.execute('INSERT INTO activities(id,routine,business_date,status,created_at,scheduled_at,triggered_at,ready_at,origin,notification_state,current_step) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(aid,routine,day,'preparing' if routine=='A1' else 'awaiting_reply',now,scheduled_at,now,ready,'backfill' if historical else origin,'historical_silent' if historical else ('recovery_silent' if origin=='recovery' else 'page_only'),'doctorate' if routine=='A2' else None))
            if routine!='A1' or origin=='manual':
                c.execute('INSERT INTO messages VALUES(?,?,NULL,?,?,?, ?,NULL,NULL,0)',(uid(),aid,'assistant',content,now,'local_prompt'))
            self._enqueue(c,'session',aid)
            if routine=='A1':self._enqueue(c,'learning',aid)
            if origin=='scheduled' and not historical:
                c.execute('INSERT INTO notifications VALUES(?,?,?,?,?,0,NULL,NULL,?)',(uid(),aid,'first','pending',(self.now()+dt.timedelta(seconds=10)).isoformat(),now))
            return obj(c.execute('SELECT * FROM activities WHERE id=?',(aid,)).fetchone())
    def activity(self,aid):
        with self.db() as c:
            a=obj(c.execute('SELECT * FROM activities WHERE id=?',(aid,)).fetchone())
            if not a:raise KeyError('活动不存在')
            messages=[obj(r) for r in c.execute('SELECT * FROM messages WHERE activity_id=? AND deleted=0 ORDER BY received_at,rowid',(aid,))]
            s=c.execute('SELECT * FROM summaries WHERE activity_id=?',(aid,)).fetchone()
            corrections=[{**obj(r),'fields':json.loads(r['fields'])} for r in c.execute('SELECT * FROM corrections WHERE activity_id=? ORDER BY version',(aid,))]
            attachments=[obj(r) for r in c.execute('SELECT * FROM attachments WHERE activity_id=?',(aid,))]
            qs=c.execute('SELECT questions FROM native_questions WHERE activity_id=?',(aid,)).fetchone()
            reflection=[{**obj(r),'source_ids':json.loads(r['source_ids'])} for r in c.execute('SELECT * FROM reflection_steps WHERE activity_id=?',(aid,))]
        history=[]
        with self.db() as c:
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='records_profile'").fetchone() and not c.execute("SELECT 1 FROM records_meta WHERE key='quarantine' AND value='1'").fetchone():
                for row in c.execute("SELECT p.* FROM records_profile p WHERE scope=? AND active=1 AND version=(SELECT MAX(version) FROM records_profile q WHERE q.item_id=p.item_id)",('activity:'+aid,)):
                    try:payload=json.loads(row['content'])
                    except (ValueError,TypeError):continue
                    history.append({**payload,'id':row['item_id'],'source_refs':json.loads(row['source_refs']),'recorded_at':row['imported_at'],'version':row['version']})
        processing=json.loads(self.meta('learning_processing:'+aid,'null'))
        if processing and (self.meta('isolated')=='true' or a['session_id']!=processing.get('session_id') or a['session_state']!='ready' or a['status']!='preparing' or a['ready_at'] or any(m['role']=='user' for m in messages)):processing=None
        if a['routine']=='A1' and self.meta('isolated')!='true':
            from .learning_guard import public_status
            blocked=json.loads(self.meta('learning_guard_wait:'+aid,'null'))
            if blocked and (blocked.get('session_id')!=a['session_id'] or a['session_state']!='ready'):blocked=None
            if blocked and not blocked.get('requirements_changed') and any(m['role']=='user' and m['id'] not in blocked.get('source_user_ids',[]) for m in messages):blocked=None
            from .learning_schedule import processing as schedule_processing
            processing=public_status(self,aid) or processing or blocked or schedule_processing(self,aid)
        a['display_status']=a['status']
        if a['routine']=='A1' and not a['ready_at'] and processing:
            state=processing.get('status')
            if state in ('rejected','failed','cancelled','delayed','recovery_hold'):
                a['display_status']='learning_'+('cancelled' if state=='recovery_hold' else state)
        return {'activity':a,'messages':messages,'summary':json.loads(s['content']) if s else None,'summary_state':s['state'] if s else 'pending','corrections':corrections,'attachments':attachments,'reflection':reflection,'questions':json.loads(qs[0]) if qs else [],'historical_records':history,'processing':processing}
    def submit(self,aid,submission_id,body,call_id=None,question_id=None):
        if not isinstance(body,str) or not body.strip() or len(body)>100000:raise ValueError('请输入 1–100000 字符')
        if not isinstance(submission_id,str) or not 8<=len(submission_id)<=128:raise ValueError('缺少稳定提交 ID')
        if bool(call_id)!=bool(question_id):raise ValueError('回答原生问题须同时指定问题批次与问题ID')
        now=self.stamp()
        with self.db(True) as c:
            prior=c.execute('SELECT * FROM messages WHERE submission_id=?',(submission_id,)).fetchone()
            if prior:
                if prior['activity_id']!=aid or prior['body']!=body or prior['deleted']:raise ValueError('提交 ID 已被其他内容使用或已删除')
                target=c.execute('SELECT * FROM message_targets WHERE message_id=?',(prior['id'],)).fetchone()
                if (target['call_id'] if target else None)!=call_id or (target['question_id'] if target else None)!=question_id:raise ValueError('提交ID已有不同问题目标')
                return {'receipt':prior['id'],'activity_id':aid,'processing_state':prior['processing_state'],'received_at':prior['received_at'],'duplicate':True}
            a=c.execute('SELECT * FROM activities WHERE id=?',(aid,)).fetchone()
            if not a:raise KeyError('活动不存在')
            mid=uid()
            c.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,NULL,NULL,0)',(mid,aid,submission_id,'user',body,now,'pending'))
            from .feedback import capture as capture_feedback
            capture_feedback(c,mid,now)
            if call_id:c.execute('INSERT INTO message_targets VALUES(?,?,?)',(mid,call_id,question_id))
            # A brief relevant response completes a basic sleep record; discussion stays open.
            status='basic_complete' if a['routine']=='A3' and re.search(r'睡|起|醒|困|梦|\d[:：]\d|分|质量',body) else 'partial'
            c.execute('UPDATE activities SET version=version+1,status=? WHERE id=?',(status,aid))
            if a['routine']=='A2':
                dimensions={'doctorate':['博士','实验','论文'],'startup':['创业','产品','商业'],'culture':['文化','阅读','写作','作品']}
                mentioned=[line for line,words in dimensions.items() if any(word in body for word in words)]
                if re.search(r'科研(?!创业)',body) and 'doctorate' not in mentioned:mentioned.insert(0,'doctorate')
                if not mentioned and re.search(r'无进展|未投入|不想谈|今天|我|完成|认识|遇到',body):mentioned=[a['current_step'] or 'doctorate']
                for line in mentioned:
                    prev=c.execute('SELECT source_ids FROM reflection_steps WHERE activity_id=? AND line=?',(aid,line)).fetchone()
                    refs=json.loads(prev[0]) if prev else [];refs.append(mid)
                    state='skipped' if re.search(r'不想谈|跳过这一',body) else 'answered'
                    c.execute('INSERT OR REPLACE INTO reflection_steps VALUES(?,?,?,?)',(aid,line,state,dumps(refs)))
                done=[r[0] for r in c.execute('SELECT line FROM reflection_steps WHERE activity_id=?',(aid,))]
                if len(done)==3:c.execute("UPDATE activities SET status='basic_complete' WHERE id=?",(aid,))
                # Keep current dimension while user is explicitly exploring it.
                if mentioned and not re.search(r'深入|继续聊|展开|[?？]|为什么|如何',body):
                    nxt=next((line for line in dimensions if line not in done),a['current_step'])
                    c.execute('UPDATE activities SET current_step=? WHERE id=?',(nxt,aid))
            self._enqueue(c,'handoff',mid)
            if a['routine'] in {'A2','A3'}:self._enqueue(c,'summary',aid,a['version']+1)
            return {'receipt':mid,'activity_id':aid,'processing_state':'pending','received_at':now,'duplicate':False}
    def summary_snapshot(self,aid):
        view=self.activity(aid);a=view['activity']
        if a['routine'] not in {'A2','A3'}:raise ValueError('学习与日常聊天不进入定向摘要')
        return {'activity_id':aid,'routine':a['routine'],'business_date':a['business_date'],'input_version':a['version'],'correction_version':a['correction_version'],'messages':[m for m in view['messages'] if m['role']=='user'],'corrections':view['corrections']}
    def summarize_literal(self,aid):
        """Source-preserving partial record, explicitly distinct from semantic model summary."""
        s=self.summary_snapshot(aid)
        fields=({'progress':None,'insights':None,'constraints':None,'decisions':None,'next_steps':None,'assistance':None} if s['routine']=='A2' else {'sleep_time':None,'wake_time':None,'quality':None,'exercise_actual':None,'exercise_plan':None,'mood':None})
        # Never infer missing values. Keep original statements for correction and later semantic extraction.
        content={'business_date':s['business_date'],'method':'source_record_pending_semantic_summary','fields':fields,'statements':[{'message_id':m['id'],'text':m['body'],'received_at':m['received_at']} for m in s['messages']],'suggestions':[],'source_ids':[m['id'] for m in s['messages']]}
        if s['routine']=='A3':
            for m in s['messages']:
                b=m['body']
                times=re.search(r'(\d{1,2}[:：]\d{2})\s*(?:睡|入睡).*?(\d{1,2}[:：]\d{2})\s*起',b)
                if times:fields.update(sleep_time=times[1].replace('：',':'),wake_time=times[2].replace('：',':'))
                score=re.search(r'([1-5])\s*分',b)
                if score:fields['quality']=int(score[1])
                elif re.search(r'睡得|睡眠|睡的',b):fields['quality']=b
        return self.commit_summary(s,content,'partial_source_record')
    def commit_summary(self,snapshot,content,state='ready'):
        with self.db(True) as c:
            a=c.execute('SELECT * FROM activities WHERE id=?',(snapshot['activity_id'],)).fetchone()
            if not a or a['version']!=snapshot['input_version'] or a['correction_version']!=snapshot['correction_version']:return False
            content=dict(content);content.setdefault('fields',{})
            for r in c.execute('SELECT fields FROM corrections WHERE activity_id=? ORDER BY version',(a['id'],)):
                content['fields'].update({k:v for k,v in json.loads(r[0]).items() if k!='chronicle_entry'})
            content['correction_version']=a['correction_version']
            c.execute('INSERT OR REPLACE INTO summaries VALUES(?,?,?,?,?,?)',(a['id'],dumps(content),a['version'],a['correction_version'],self.stamp(),state))
            return True
    def correct(self,aid,fields,source):
        if not isinstance(fields,dict) or not fields or not source:raise ValueError('更正需要字段与用户来源')
        self.activity(aid)
        with self.db(True) as c:
            c.execute('UPDATE activities SET correction_version=correction_version+1 WHERE id=?',(aid,))
            a=c.execute('SELECT * FROM activities WHERE id=?',(aid,)).fetchone()
            c.execute('INSERT INTO corrections VALUES(?,?,?,?,?,?)',(uid(),aid,dumps(fields),source,self.stamp(),a['correction_version']))
            self._enqueue(c,'summary',aid,a['version']+a['correction_version']*1000000)
        self.summarize_literal(aid)
        return self.activity(aid)
    def control(self,aid,action,**kw):
        self.activity(aid)
        with self.db(True) as c:
            if action in {'skip','end','continue'}:
                c.execute('UPDATE activities SET status=? WHERE id=?',({'skip':'skipped','end':'user_ended','continue':'awaiting_reply'}[action],aid))
            elif action=='bookmark':c.execute('UPDATE activities SET bookmarked=? WHERE id=?',(bool(kw.get('value',True)),aid))
            elif action=='snooze':
                when=dt.datetime.fromisoformat(kw['remind_at'])
                if when.tzinfo is None:when=when.replace(tzinfo=TZ)
                if when<=self.now():raise ValueError('提醒时间应在未来')
                c.execute("INSERT INTO notifications VALUES(?,?, 'snooze','pending',?,0,NULL,NULL,?) ON CONFLICT(activity_id,kind) DO UPDATE SET state='pending',run_after=excluded.run_after,attempts=0,receipt=NULL,error=NULL",(uid(),aid,when.astimezone(UTC).isoformat(),self.stamp()))
            else:raise ValueError('未知活动操作')
        return self.activity(aid)
    def schedules(self):
        with self.db() as c:rows=[obj(r) for r in c.execute('SELECT * FROM schedules ORDER BY routine')]
        for r in rows:r['weekdays']=json.loads(r['weekdays']);r['next_at']=self.next_schedule(r)
        return rows
    def next_schedule(self,s):
        now=self.now().astimezone(TZ)
        for offset in range(3700):
            day=now.date()+dt.timedelta(days=offset)
            candidate=dt.datetime.combine(day,dt.time.fromisoformat(s['time']),TZ)
            if candidate>now and day.weekday() in s['weekdays'] and (not s['paused_until'] or day.isoformat()>s['paused_until']):return candidate.isoformat()
        return None
    def change_schedule(self,routine,time,weekdays,paused_until,source,actor='user'):
        if actor!='user' or not source:raise PermissionError('只有明确用户输入可以更改日程')
        if routine not in ROUTINES:raise ValueError('未知例程')
        dt.time.fromisoformat(time)
        if not weekdays or any(type(x)!=int or x not in range(7) for x in weekdays):raise ValueError('请选择至少一个星期日')
        if paused_until:dt.date.fromisoformat(paused_until)
        with self.db(True) as c:
            before=obj(c.execute('SELECT * FROM schedules WHERE routine=?',(routine,)).fetchone())
            after={'routine':routine,'time':time,'weekdays':sorted(set(weekdays)),'paused_until':paused_until or None}
            c.execute('UPDATE schedules SET time=?,weekdays=?,paused_until=?,updated_at=?,source=?,effective_at=? WHERE routine=?',(time,dumps(after['weekdays']),paused_until or None,self.stamp(),source,self.stamp(),routine))
            c.execute('INSERT INTO schedule_events VALUES(?,?,?,?,?,?)',(uid(),routine,dumps(before),dumps(after),source,self.stamp()))
        return next(s for s in self.schedules() if s['routine']==routine)
    def tick(self,recovery=False):
        if self.meta('isolated')=='true':return []
        from .learning_schedule import tick as learning_tick
        learning_tick(self,recovery=recovery)
        now=self.now().astimezone(TZ);created=[]
        for s in self.schedules():
            when=dt.datetime.combine(now.date(),dt.time.fromisoformat(s['time']),TZ)
            if now.weekday() not in s['weekdays'] or (s['paused_until'] and self.today()<=s['paused_until']) or now<when:continue
            # A schedule edited after today's slot resumes at the next future slot.
            if dt.datetime.fromisoformat(s['effective_at'])>when:continue
            origin='recovery' if recovery or (now-when).total_seconds()>90 else 'scheduled'
            created.append(self.ensure_activity(s['routine'],self.today(),origin,when.isoformat()))
        return created
    def gaps(self,end=None,days=7):
        end=dt.date.fromisoformat(end or self.today());start=self.meta('started_on');rows=[]
        with self.db() as c:
            for n in range(days):
                day=(end-dt.timedelta(days=n));ds=day.isoformat()
                for s in self.schedules():
                    a=c.execute('SELECT id,status,routine FROM activities WHERE routine=? AND business_date=? AND NOT EXISTS (SELECT 1 FROM learning_releases r WHERE r.activity_id=activities.id AND r.activated_at IS NULL)',(s['routine'],ds)).fetchone()
                    # Historical policy is reconstructed from recorded changes, not today's frequency.
                    first=c.execute('SELECT before_json FROM schedule_events WHERE routine=? AND created_at>? ORDER BY created_at LIMIT 1',(s['routine'],dt.datetime.combine(day,dt.time.max,TZ).astimezone(UTC).isoformat())).fetchone()
                    hist=json.loads(first[0]) if first else s
                    weekdays=hist['weekdays'];weekdays=json.loads(weekdays) if isinstance(weekdays,str) else weekdays
                    status=a['status'] if a else ('before_start' if ds<start else 'paused' if hist.get('paused_until') and ds<=hist['paused_until'] else 'not_scheduled' if day.weekday() not in weekdays else 'missing')
                    if a and a['routine']=='A1' and status=='awaiting_reply':status='可补学／继续讨论'
                    display=self.activity(a['id'])['activity'].get('display_status',status) if a and a['routine']=='A1' and status=='preparing' else status
                    rows.append({'date':ds,'routine':s['routine'],'status':status,'display_status':display,'activity_id':a['id'] if a else None})
        return rows
    def natural(self,body,submission_id=None):
        request_id=submission_id or hashlib.sha256((self.today()+body).encode()).hexdigest()
        with self.db(True) as c:
            prior=c.execute('SELECT * FROM natural_requests WHERE id=?',(request_id,)).fetchone()
            if prior and prior['body']!=body:raise ValueError('自然语言请求ID已用于其他内容')
            if prior and prior['result']:return json.loads(prior['result'])
            base_day=prior['business_date'] if prior else self.today()
            c.execute('INSERT OR IGNORE INTO natural_requests VALUES(?,?,?,NULL,?)',(request_id,body,base_day,self.stamp()))
        routine='A3' if re.search(r'睡眠|打卡|睡',body) else 'A2' if '反思' in body else 'A1' if re.search(r'学习|补学',body) else None
        # Parse separate explicit dated clauses without duplicating one statement into several facts.
        if re.search(r'补|继续.*(?:昨天|前天|\d{4}-)',body) and routine:
            parts=re.split(r'[；;\n]+',body);out=[]
            for part in parts:
                dates=re.findall(r'\d{4}-\d{2}-\d{2}|昨天|前天|今天',part)
                if len(dates)!=1:return {'message':'请为每段内容明确一个日期；尚未保存任何分段。'}
                if dates[0] not in {'昨天','前天','今天'}:
                    dt.date.fromisoformat(dates[0])
                    if dates[0]>self.today():return {'message':'补记日期不能在未来；尚未保存任何分段。'}
            for index,part in enumerate(parts):
                found=re.findall(r'\d{4}-\d{2}-\d{2}|昨天|前天|今天',part)
                if len(found)!=1:return {'message':'请为每段内容明确一个日期，例如“补记昨天的睡眠：睡得一般；补记前天的睡眠：很好”。'}
                raw=found[0];day=(dt.date.fromisoformat(base_day)-dt.timedelta(days={'今天':0,'昨天':1,'前天':2}[raw])).isoformat() if raw in {'今天','昨天','前天'} else raw
                r='A2' if '反思' in part else 'A1' if '补学' in part else routine
                a=self.ensure_activity(r,day);out.append(a)
                content=re.split(r'[:：]',part,maxsplit=1)
                if len(content)==2 and content[1].strip():self.submit(a['id'],str(uuid.uuid5(uuid.NAMESPACE_URL,request_id+':'+str(index)+':'+day+':'+r)),content[1].strip())
            result={'activity':out[0],'activities':out,'message':'已定位 '+ '、'.join(a['business_date']+' '+LABELS[a['routine']] for a in out)+'（Asia/Shanghai）'}
            with self.db(True) as c:c.execute('UPDATE natural_requests SET result=? WHERE id=?',(dumps(result),request_id))
            return result
        return {'message':'请明确活动与日期，例如“补记昨天的睡眠”或“继续 2026-10-20 的反思”。日程调整和明确委托可使用对应控件。'}
    def journal(self,day=None,query=''):
        with self.db() as c:
            rows=[obj(r) for r in c.execute('SELECT m.*,a.business_date,a.routine FROM messages m JOIN activities a ON a.id=m.activity_id WHERE m.deleted=0 AND NOT EXISTS (SELECT 1 FROM learning_releases r WHERE r.activity_id=a.id AND r.activated_at IS NULL) AND m.body LIKE ? ORDER BY m.received_at,m.rowid',('%'+query+'%',))]
        with self.db() as c:
            for t in c.execute('SELECT * FROM tasks'):
                rows.append({'id':'task:'+t['id'],'role':'user','body':t['authorization'],'received_at':t['created_at'],'activity_id':t['source_activity_id'],'routine':'TASK','business_date':dt.datetime.fromisoformat(t['created_at']).astimezone(TZ).date().isoformat(),'source_ref':'task:'+t['id']})
            for r in c.execute('SELECT * FROM task_runs WHERE body IS NOT NULL'):
                rows.append({'id':'run:'+r['id'],'role':'assistant','body':r['body'],'received_at':r['finished_at'] or r['created_at'],'source_ref':'task:'+r['task_id'],'routine':'TASK'})
            for r in c.execute('SELECT * FROM task_reviews'):
                rows.append({'id':'review:'+r['id'],'role':'user','body':r['input'],'received_at':r['created_at'],'source_ref':'task:'+r['task_id'],'routine':'TASK'})
        return sorted([{**r,'source':'instance'} for r in rows if (not day or dt.datetime.fromisoformat(r['received_at']).astimezone(TZ).date().isoformat()==day) and query.lower() in r['body'].lower()],key=lambda r:r['received_at'])
    def delete_message(self,mid,source):
        if not source:raise ValueError('删除须有明确用户操作来源')
        from .deletion import apply_message_deletion
        with self.db(True) as c:
            m=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
            if not m:raise KeyError('原文不存在')
            affected=apply_message_deletion(c,mid,self.stamp())
        return {'deleted':mid,'native_cleanup_pending':True,'affected':affected,'message':'本地原文及相关派生内容已失效；原生清理完成前暂停相关上下文，其他用户原话保留。'}
    def reserve(self,estimate,kind='background',*,minimum_daily_remaining=0):
        if type(estimate)!=int or estimate<=0:raise ValueError('无效预留')
        if type(minimum_daily_remaining)!=int or minimum_daily_remaining<0:raise ValueError('无效保留额度')
        if kind!='background' and minimum_daily_remaining:raise ValueError('保留额度仅适用于后台任务')
        with self.db(True) as c:
            # Sample after acquiring the write lock. A request waiting across
            # Shanghai midnight must reserve against the new day's budget, and
            # its day/month and receipt timestamp must use the same instant.
            now=self.now();today=now.astimezone(TZ).date().isoformat();month=today[:7]
            policy=c.execute("SELECT value FROM meta WHERE key='background_limit_enforced'").fetchone()
            # Only an explicit persisted opt-out disables the local quota. Keep
            # reservations and receipts in both modes, including unknown usage.
            enforced=not policy or policy['value']!='false'
            if kind=='background' and enforced:
                day=c.execute("SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) FROM usage WHERE kind='background' AND day=?",(today,)).fetchone()[0]
                mon=c.execute("SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) FROM usage WHERE kind='background' AND month=?",(month,)).fetchone()[0]
                limits={'daily_tokens':60000,'monthly_tokens':1000000}
                limits.update({row['key']:int(row['value']) for row in c.execute(
                    "SELECT key,value FROM meta WHERE key IN ('daily_tokens','monthly_tokens')")})
                if minimum_daily_remaining and (estimate+minimum_daily_remaining>limits['daily_tokens'] or estimate>limits['monthly_tokens']):
                    raise BudgetRequestTooLarge('完整史料请求超出当前史官单次预算；原话保留，需按完整事件分组或明确调整预算后重试')
                if day+estimate>limits['daily_tokens'] or mon+estimate>limits['monthly_tokens']:raise RuntimeError('后台预算不足；原文仍可保存，日程保持不变')
                # A lower-priority caller may consume only the surplus above
                # this floor. Counts include all background reservations and
                # unknown usage, not just settled calls of the same alias.
                if day+estimate+minimum_daily_remaining>limits['daily_tokens']:
                    raise RuntimeError('个人史编修暂缓，为每日活动保留后台额度；原文仍可保存，日程保持不变')
            rid=uid();c.execute('INSERT INTO usage VALUES(?,?,?,?,?,NULL,?,?,NULL)',(rid,kind,today,month,estimate,'reserved',now.isoformat()))
        return rid
    def settle(self,rid,actual=None,receipt=None):
        with self.db(True) as c:
            if actual is not None and (type(actual)!=int or actual<0):raise ValueError('无效实际用量')
            c.execute('UPDATE usage SET actual=?,state=?,receipt=? WHERE id=?',(actual,'settled' if actual is not None else 'unknown',receipt,rid))
    def budget(self):
        with self.db() as c:
            rows=[obj(r) for r in c.execute('SELECT kind,day,month,SUM(COALESCE(actual,0)) AS settled,SUM(CASE WHEN actual IS NULL THEN reserved ELSE 0 END) AS reserved FROM usage GROUP BY kind,day,month')]
            settings={r['key']:r['value'] for r in c.execute("SELECT key,value FROM meta WHERE key IN ('daily_tokens','monthly_tokens','background_limit_enforced')")}
        enforced=settings.get('background_limit_enforced')!='false'
        daily=int(settings.get('daily_tokens','60000'));monthly=int(settings.get('monthly_tokens','1000000'))
        return {'background_limit_enforced':enforced,'daily_limit':daily if enforced else None,'monthly_limit':monthly if enforced else None,'configured_daily_limit':daily,'configured_monthly_limit':monthly,'today':self.today(),'usage':rows}
    def create_task(self,goal,owner='Agent',inputs='',deliverable='可读成果',acceptance='交付内容满足目标，保留来源及验证说明',run_at=None,interval_days=None,source_activity_id=None,**kwargs):
        if not goal.strip():raise ValueError('请填写明确委托目标')
        when=dt.datetime.fromisoformat(run_at) if run_at else self.now()
        if when.tzinfo is None:when=when.replace(tzinfo=TZ)
        if interval_days is not None and (type(interval_days)!=int or interval_days<1):raise ValueError('周期至少1天')
        policy=kwargs.get('catchup_policy','before_deadline');deadline=kwargs.get('deadline')
        if policy not in {'before_deadline','skip_missed'}:raise ValueError('未知错过任务处理规则')
        if deadline:
            deadline_dt=dt.datetime.fromisoformat(deadline)
            if deadline_dt.tzinfo is None:deadline_dt=deadline_dt.replace(tzinfo=TZ)
            deadline=deadline_dt.astimezone(UTC).isoformat()
        request_id=kwargs.get('submission_id')
        request_body=kwargs.get('request_body')
        fingerprint=hashlib.sha256(dumps([request_body] if request_body is not None else [goal,owner,inputs,deliverable,acceptance,run_at,interval_days,source_activity_id,policy,deadline]).encode()).hexdigest()
        if request_id is not None and (not isinstance(request_id,str) or not 8<=len(request_id)<=128):raise ValueError('缺少稳定任务提交ID')
        tid=uid()
        with self.db(True) as c:
            prior=c.execute('SELECT * FROM task_requests WHERE id=?',(request_id,)).fetchone() if request_id else None
            if prior:
                if prior['fingerprint']!=fingerprint:raise ValueError('任务提交ID已用于其他内容；请先核对原任务')
                tid=prior['task_id']
            else:
                c.execute('INSERT INTO tasks(id,goal,owner,inputs,deliverable,acceptance,source_activity_id,status,run_at,interval_days,created_at,authorization,catchup_policy,deadline) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(tid,goal,owner,inputs,deliverable,acceptance,source_activity_id,'queued',when.astimezone(UTC).isoformat(),interval_days,self.stamp(),request_body or goal,policy,deadline))
                if request_id:c.execute('INSERT INTO task_requests VALUES(?,?,?,?)',(request_id,fingerprint,tid,self.stamp()))
        return self.task(tid)
    def task(self,tid):
        with self.db() as c:
            t=obj(c.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone())
            if not t:raise KeyError('任务不存在')
            t['deliveries']=[obj(r) for r in c.execute('SELECT * FROM task_runs WHERE task_id=? ORDER BY version',(tid,))]
            t['reviews']=[obj(r) for r in c.execute('SELECT * FROM task_reviews WHERE task_id=? ORDER BY created_at',(tid,))]
            from .task_requirements import snapshot
            t['requirements_version']=snapshot(c,tid)['requirements_version']
            t['requirements_history']=[obj(r) for r in c.execute('SELECT * FROM task_requirement_revisions WHERE task_id=? ORDER BY requirements_version',(tid,))]
        return t
    def tasks(self):
        with self.db() as c:ids=[r[0] for r in c.execute('SELECT id FROM tasks ORDER BY created_at DESC')]
        return [self.task(t) for t in ids]
    def claim_task(self,tid):
        with self.db(True) as c:
            t=c.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
            if not t or t['status'] in {'running','cancelled','permission_required','needs_input'}:return None
            if t['status']=='recovery_hold':return None
            from .task_requirements import snapshot
            requirements=snapshot(c,tid)
            version=t['version']+1;rid=uid()
            c.execute("UPDATE tasks SET status='running',version=?,error=NULL WHERE id=?",(version,tid))
            c.execute('INSERT INTO task_runs(id,task_id,version,status,created_at) VALUES(?,?,?,?,?)',(rid,tid,version,'running',self.stamp()))
        return {'run_id':rid,'task_id':tid,'version':version,'requirements':requirements}
    def deliver_task(self,tid,version,body,session_id=None):
        with self.db(True) as c:
            t=c.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
            if t['version']!=version or t['status']!='running':return False
            c.execute("UPDATE task_runs SET status='awaiting_acceptance',body=?,session_id=?,finished_at=? WHERE task_id=? AND version=?",(body,session_id,self.stamp(),tid,version))
            next_at=(self.now()+dt.timedelta(days=t['interval_days'])).isoformat() if t['interval_days'] else t['run_at']
            c.execute("UPDATE tasks SET status='awaiting_acceptance',result=?,run_at=? WHERE id=?",(body,next_at,tid))
        return True
    def review_task(self,task_id,version,action,input,actor='user'):
        if actor!='user' or not input.strip():raise PermissionError('验收须保留用户原始输入')
        if action not in {'accept','revise','cancel'}:raise ValueError('未知验收操作')
        with self.db(True) as c:
            t=c.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
            if not t:raise KeyError('任务不存在')
            if t['version']!=version:raise ValueError('成果已更新，请核对当前版本再验收')
            if action!='cancel' and t['status'] not in {'awaiting_acceptance','accepted'}:raise ValueError('当前版本尚未交付')
            c.execute('INSERT INTO task_reviews VALUES(?,?,?,?,?,?,?)',(uid(),task_id,version,action,input,actor,self.stamp()))
            state={'accept':'accepted','revise':'revision','cancel':'cancelled'}[action]
            c.execute('UPDATE tasks SET status=?,run_at=CASE WHEN ?=\'revision\' THEN ? ELSE run_at END WHERE id=?',(state,state,self.stamp(),task_id))
            c.execute('UPDATE task_runs SET status=? WHERE task_id=? AND version=?',(state,task_id,version))
        return self.task(task_id)
