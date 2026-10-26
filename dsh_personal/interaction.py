"""Conservative explicit commands. Ordinary discussion never changes schedules."""
import datetime as dt, hashlib, json, re
from .core import ROUTINES, TZ, uid

# Full-message commands only. Quoted examples, questions, and ordinary discussion
# must reach the conversation worker rather than mutate persistent settings.
_PREFIX=r'(?:请)?(?:把)?\s*'
_ROUTINE_ALIASES={'每日一学':'A1','学习':'A1','每日反思':'A2','反思':'A2',
                  '睡眠打卡':'A3','每日打卡':'A3','睡眠':'A3','打卡':'A3',
                  'A1':'A1','A2':'A2','A3':'A3'}
_ALIAS='(?:'+'|'.join(_ROUTINE_ALIASES)+')'
_NAMES={'A1':'每日一学','A2':'每日反思','A3':'睡眠打卡'}
_QUESTION_END=re.compile(r'[？?]|(?:吗|怎么样|是否可以|能不能|可不可以)\s*[。！!]*$')


def _command_text(text):
    if _QUESTION_END.search(text):return None
    return text.rstrip('。！!').strip()


def _settings_matches(text):
    show=re.fullmatch(_PREFIX+r'(?:查看|显示)(?:当前)?打卡设置',text)
    learning=re.fullmatch(_PREFIX+r'每日一学(?:的)?(经典|古籍|历史|现代概念|现代)(?:主题|内容方向)\s*(?:改为|改成|设为)\s*(.+)',text,re.S)
    teaching=re.fullmatch(_PREFIX+r'每日一学(?:的)?教学要求\s*(?:改为|改成|设为)\s*(.+)',text,re.S)
    reset=re.fullmatch(_PREFIX+r'每日一学恢复自动选材',text)
    reflection=re.fullmatch(_PREFIX+r'每日反思(?:的)?方面\s*(?:改为|改成|设为)\s*(.+)',text,re.S)
    return show,learning,teaching,reset,reflection


def _settings_command(store,text,source,submission_id):
    text=_command_text(text)
    if text is None:return None
    show,learning,teaching,reset,reflection=_settings_matches(text)
    if not any((show,learning,teaching,reset,reflection)):return None
    from .routine_settings import REQUEST, read_settings, save_settings
    if submission_id and not show:
        replay=_replay_command(store,REQUEST+submission_id,source)
        if replay is not None:return replay
    with store.db() as db:
        a1=read_settings(db,'A1');a2=read_settings(db,'A2')
    if show:
        topics=a1['settings']['topics']
        lines=['当前已保存的打卡设置：','每日一学（版本 '+str(a1['version'])+'）：']
        lines.extend('• '+label+'：'+(topics.get(key) or '自动选材') for key,label in [('classic','经典主题'),('history','历史主题'),('modern','现代概念主题')])
        lines.append('• 教学要求：'+(a1['settings'].get('teaching_request') or '沿用已保存的长期学习要求'))
        lines.append('每日反思（版本 '+str(a2['version'])+'）方面：'+'、'.join(x['label'] for x in a2['settings']['dimensions']))
        schedules=store.schedules()
        lines.append('打卡时间（Asia/Shanghai）：')
        lines.extend('• '+_NAMES[s['routine']]+'：'+s['time']+'，'+_frequency(s)+'；下一次 '+str(s['next_at'] or '暂无') for s in schedules)
        return {'message':'\n'.join(lines),'routine_settings':{'A1':a1,'A2':a2},'schedules':schedules}
    routine='A2' if reflection else 'A1'
    current=a2 if reflection else a1
    # read_settings returns decoded JSON, but copy nested records explicitly so
    # callers and fixtures never observe partial edits before validation/commit.
    if reflection:
        labels=[x.strip() for x in re.split(r'[、，,；;\n]+',reflection[1]) if x.strip()]
        if not labels or len(labels)>12 or len(set(labels))!=len(labels) or any(len(x)>80 for x in labels):
            return {'message':'尚未修改。请用顿号列出 1—12 个不重复的反思方面，每个不超过 80 字，例如“每日反思方面改为 科研、健康、家庭”。'}
        known={x['label']:x for x in current['settings']['dimensions']}
        dimensions=[]
        for label in labels:
            if label in known:dimensions.append(dict(known[label]))
            else:dimensions.append({'id':'custom_'+hashlib.sha256(label.encode()).hexdigest()[:12],
                                    'label':label,'prompt':'今天在'+label+'方面有哪些进展、感受或需要一起推敲的事？','keywords':[label]})
        settings={'dimensions':dimensions}
    else:
        settings={'topics':dict(current['settings']['topics']),
                  'teaching_request':current['settings'].get('teaching_request','')}
        if learning:
            key={'经典':'classic','古籍':'classic','历史':'history','现代概念':'modern','现代':'modern'}[learning[1]]
            settings['topics'][key]=learning[2].strip()
        elif teaching:settings['teaching_request']=teaching[1].strip()
        else:settings['topics']={'classic':'','history':'','modern':''}
    try:return save_settings(store,routine,settings,current['version'],submission_id or uid(),source)
    except ValueError:
        # A parallel retry can pass the first replay lookup before the winning
        # request commits and then observe its newer settings version.
        if submission_id:
            replay=_replay_command(store,REQUEST+submission_id,source)
            if replay is not None:return replay
        raise


def _frequency(schedule):
    days=schedule['weekdays']
    frequency='每天' if days==list(range(7)) else '每周'+'、'.join('一二三四五六日'[day] for day in days)
    if schedule.get('paused_until'):frequency+='，暂停至 '+schedule['paused_until']+'（含当日）'
    return frequency


def _replay_command(store,key,source):
    with store.db() as db:
        if db.execute("SELECT 1 FROM meta WHERE key='isolated' AND value='true'").fetchone():
            raise PermissionError('恢复隔离中，不能更改打卡设置')
        cached=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        if not cached:return None
        saved=json.loads(cached[0])
        if saved.get('deleted'):raise ValueError('该请求来源已删除，请重新提交')
        if saved['result']['source'].strip()!=source.strip():raise ValueError('提交编号已用于其他设置')
        return {**saved['result'],'duplicate':True}


def _schedule_receipt(schedule):
    before=schedule['before']
    verb='已重放原变更回执' if schedule.get('duplicate') else '已更新'
    return {'message':f"{verb}{_NAMES[schedule['routine']]}：{before['time']} → {schedule['time']}（Asia/Shanghai）；{_frequency(schedule)}。原定下一次 {schedule['next_at'] or '暂无'}。" if schedule.get('duplicate') else
                      f"{verb}{_NAMES[schedule['routine']]}：{before['time']} → {schedule['time']}（Asia/Shanghai）；{_frequency(schedule)}。下一次 {schedule['next_at'] or '暂无'}。",
            'before':before,'schedule':schedule,'receipt':schedule.get('receipt'),'duplicate':bool(schedule.get('duplicate'))}


def is_local_control(body, active_routine=None):
    """Pure classification before storing a chat message; never execute a command.

    Invalid but recognized settings/times are controls too: their rejection
    receipt must not become a sleep or reflection answer. Questions and quoted
    examples stay ordinary messages. Keep grammar shared with command handlers.
    """
    raw=body.strip()
    if active_routine and raw in {'跳过本次','今天先不聊','跳过今天','今天到这里','结束本次','继续','继续本次'}:
        return True
    text=_command_text(raw)
    if text is None:return False
    if any(_settings_matches(text)):return True
    if re.fullmatch(_PREFIX+r'(?:所有|全部)打卡(?:的)?(?:时间)?\s*(?:改成|改为|改到|设为)\s*(.+)',text):return True
    return _schedule_parts(text,active_routine) is not None


def _schedule_parts(text,active_routine):
    match=re.fullmatch(_PREFIX+r'(?P<routine>'+_ALIAS+r')?\s*(?:的)?(?:提醒时间|打卡时间|时间)?\s*(?P<verb>改成|改为|改到|调整为|设为)\s*(?P<value>.+)',text)
    pause_match=re.fullmatch(_PREFIX+r'(?P<routine>'+_ALIAS+r')?\s*(?P<value>暂停到\s*\S+|恢复每天(?:\s*\S+)?)',text)
    selected=match or pause_match
    if not selected:return None
    routine=_ROUTINE_ALIASES.get(selected['routine']) or (active_routine if active_routine in ROUTINES else None)
    if not routine:return None
    value=selected['value'].strip()
    restore=value.startswith('恢复每天');pause_requested=value.startswith('暂停到')
    daily=re.fullmatch(r'每天(?:\s*(\d{1,2}:\d{1,2}))?',value)
    weekly=re.fullmatch(r'每周([一二三四五六日天、，,和及\s]+?)(?:\s*(\d{1,2}:\d{1,2}))?',value)
    time_like=bool(re.match(r'^\d{1,2}:',value))
    if not (restore or pause_requested or daily or weekly or time_like):return None
    return routine,value,restore,pause_requested,daily,weekly


def _schedule_command(store,text,source,active,submission_id):
    text=_command_text(text)
    if text is None:return None
    all_match=re.fullmatch(_PREFIX+r'(?:所有|全部)打卡(?:的)?(?:时间)?\s*(?:改成|改为|改到|设为)\s*(.+)',text)
    if all_match:
        at=all_match[1]
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',at):return {'message':'尚未修改。请使用 24 小时制 HH:MM，例如 20:00。'}
        return {'message':'尚未修改。请分别设置三类打卡，以便逐项保留变更回执：\n每日一学时间改为 '+at+'\n每日反思时间改为 '+at+'\n睡眠打卡时间改为 '+at}
    # Keep content-change commands out of the schedule grammar. Only a complete
    # time, weekday specification, pause date, or daily restore can enter here.
    parts=_schedule_parts(text,active['routine'] if active else None)
    if parts is None:return None
    routine,value,restore,pause_requested,daily,weekly=parts
    if submission_id:
        replay=_replay_command(store,'schedule_request:'+submission_id,source)
        if replay is not None:return _schedule_receipt(replay)
    current=next(s for s in store.schedules() if s['routine']==routine)
    weekdays=list(current['weekdays']);at=current['time'];pause=current['paused_until']
    requested_time=None
    if restore:
        suffix=value[len('恢复每天'):].strip()
        if suffix:requested_time=suffix
        weekdays=list(range(7));pause=None
    elif pause_requested:
        date=value[len('暂停到'):].strip()
        try:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',date):raise ValueError()
            dt.date.fromisoformat(date)
        except ValueError:return {'message':'尚未修改。请明确有效暂停截止日期 YYYY-MM-DD；暂停含该日，次日恢复原日程。'}
        pause=date
    elif daily:
        weekdays=list(range(7));requested_time=daily[1]
    elif weekly:
        weekdays=sorted(set('一二三四五六日'.index(ch) if ch!='天' else 6 for ch in weekly[1] if ch in '一二三四五六日天'))
        requested_time=weekly[2]
    else:requested_time=value
    if requested_time is not None:
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',requested_time):return {'message':'尚未修改。请使用 24 小时制 HH:MM，例如“每日反思时间改为 21:40”。'}
        at=requested_time
    try:
        schedule=store.change_schedule(routine,at,weekdays,pause,source,
                                       submission_id=submission_id,expected_updated_at=current['updated_at'])
    except ValueError:
        if submission_id:
            replay=_replay_command(store,'schedule_request:'+submission_id,source)
            if replay is not None:return _schedule_receipt(replay)
        raise
    return _schedule_receipt(schedule)


def command(store,body,activity_id=None,submission_id=None):
    text=body.strip();active=store.activity(activity_id)['activity'] if activity_id else None
    settings=_settings_command(store,text,body,submission_id)
    if settings is not None:return settings
    if re.match(r'^(补记|补填|补学|把.*继续聊完|继续.*(?:昨天|前天|\d{4}-))',text):return store.natural(text,submission_id)
    if active and text in {'跳过本次','今天先不聊','跳过今天','今天到这里','结束本次','继续','继续本次'}:
        action='skip' if text in {'跳过本次','今天先不聊','跳过今天'} else 'continue' if text.startswith('继续') else 'end'
        outcome=store.control(activity_id,action,submission_id=submission_id,source=body)
        control=outcome.get('control_receipt',{})
        prefix='已重放原操作回执：' if control.get('duplicate') else '已'
        return {'message':prefix+{'skip':'跳过本次，明日计划保持不变','end':'收束本轮聊天，之后仍可继续','continue':'继续原活动'}[action],'activity_control':control}
    scheduled=_schedule_command(store,text,body,active,submission_id)
    if scheduled is not None:return scheduled
    if active and text.startswith('稍后提醒'):
        when=re.search(r'\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}',text)
        if when:stamp=dt.datetime.fromisoformat(when[0]).replace(tzinfo=TZ)
        elif '明早' in text:stamp=dt.datetime.combine(store.now().astimezone(TZ).date()+dt.timedelta(days=1),dt.time(8,30),TZ)
        else:return {'message':'请指定提醒日期与时间，或说“稍后提醒，明早”。'}
        store.control(activity_id,'snooze',remind_at=stamp.isoformat());return {'message':'已设一次提醒：'+stamp.isoformat()+'（Asia/Shanghai）'}
    accepted=re.fullmatch(r'任务\s*([\w-]+)\s*(?:第|v)(\d+)版?\s*(验收通过|要求返修|取消)[：:]?\s*(.*)',text)
    if accepted:
        task=store.review_task(accepted[1],int(accepted[2]),{'验收通过':'accept','要求返修':'revise','取消':'cancel'}[accepted[3]],body)
        return {'message':f"已记录任务 {task['id']} 第 {task['version']} 版的用户操作：{accepted[3]}",'task':task}
    explicit=re.match(r'^(?:请)?(?:帮我)?(?:创建任务|委托任务|立即执行|明天完成)[：:\s]*(.+)',text,re.S)
    if explicit:
        run_at=None
        if text.startswith('明天完成'):
            run_at=dt.datetime.combine(store.now().astimezone(TZ).date()+dt.timedelta(days=1),dt.time(8,30),TZ).isoformat()
        task=store.create_task(explicit[1],source_activity_id=activity_id,run_at=run_at,submission_id=submission_id,request_body=body)
        return {'message':'明确委托已建卡'+('并排期 '+run_at if run_at else '，服务端将执行')+'；成果交付后由你验收。','task':task}
    return None
