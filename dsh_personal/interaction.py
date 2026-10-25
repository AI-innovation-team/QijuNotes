"""Conservative explicit commands. Ordinary discussion never changes schedules."""
import datetime as dt, re
from .core import ROUTINES, LABELS, TZ, uid

def command(store,body,activity_id=None,submission_id=None):
    text=body.strip();active=store.activity(activity_id)['activity'] if activity_id else None
    routine=next((r for r in ROUTINES if r in text),None)
    if not routine:
        routine='A3' if re.search(r'打卡|睡眠',text) else 'A2' if '反思' in text else 'A1' if re.search(r'每日一学|学习',text) else (active['routine'] if active and active['routine']!='CHAT' else None)
    if re.match(r'^(补记|补填|补学|把.*继续聊完|继续.*(?:昨天|前天|\d{4}-))',text):return store.natural(text,submission_id)
    if active and text in {'跳过本次','今天先不聊','跳过今天','今天到这里','结束本次','继续','继续本次'}:
        action='skip' if text in {'跳过本次','今天先不聊','跳过今天'} else 'continue' if text.startswith('继续') else 'end'
        store.control(activity_id,action);return {'message':'已'+{'skip':'跳过本次，明日计划保持不变','end':'收束本轮聊天，之后仍可继续','continue':'继续原活动'}[action]}
    if routine and re.search(r'^(?:请)?(?:把)?(?:A[123]|每日一学|学习|反思|睡眠|打卡)?\s*(?:(?:改成|改为|改到)\s*(?:\d{1,2}:\d{2}|每周[一二三四五六日天]|每天(?:\s*\d{1,2}:\d{2}|$))|暂停到|恢复每天)',text):
        current=next(s for s in store.schedules() if s['routine']==routine)
        weekdays=current['weekdays'];at=current['time'];pause=current['paused_until']
        if '恢复每天' in text:weekdays=list(range(7));pause=None
        if '暂停到' in text:
            date=re.search(r'\d{4}-\d{2}-\d{2}',text)
            if not date:return {'message':'请明确暂停截止日期 YYYY-MM-DD；暂停含该日，次日恢复原日程。'}
            pause=date[0]
        if '每天' in text:weekdays=list(range(7))
        if '每周' in text:
            dates=re.search(r'每周([一二三四五六日天、，,和及\s]+)',text)
            if not dates:return {'message':'请明确星期，例如“打卡改成每周二、四”。'}
            weekdays=sorted(set('一二三四五六日'.index(ch) if ch!='天' else 6 for ch in dates[1] if ch in '一二三四五六日天'))
        times=re.search(r'(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)',text)
        if times:at=f'{int(times[1]):02d}:{times[2]}'
        if not any(x in text for x in ['暂停到','恢复每天','每天','每周']) and not times:return {'message':'请明确新的时间 HH:MM 或每周哪些日子。'}
        s=store.change_schedule(routine,at,weekdays,pause,body)
        return {'message':f"已按明确指令更新{LABELS[routine]}，下一次 {s['next_at']}（Asia/Shanghai）；其他例程不变。",'schedule':s}
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
