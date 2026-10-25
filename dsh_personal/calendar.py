"""User-owned calendar, with durable opt-in reminders separate from daily routines."""
import datetime as dt
import hashlib
import json
from .core import TZ, UTC, dumps, uid


class Calendar:
    def __init__(self, store):
        self.store = store
        with store.db() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS calendar_events (
              id TEXT PRIMARY KEY, title TEXT NOT NULL, start_at TEXT NOT NULL,
              end_at TEXT NOT NULL, all_day INTEGER NOT NULL, location TEXT NOT NULL,
              notes TEXT NOT NULL, reminder_minutes INTEGER, status TEXT NOT NULL,
              version INTEGER NOT NULL, notification_state TEXT NOT NULL,
              reminder_at TEXT, notification_receipt TEXT, created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL, completed_at TEXT);
            CREATE TABLE IF NOT EXISTS calendar_requests (
              submission_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, event_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS calendar_changes (
              id TEXT PRIMARY KEY, event_id TEXT NOT NULL, version INTEGER NOT NULL,
              action TEXT NOT NULL, at TEXT NOT NULL, user_input TEXT NOT NULL);
            ''')

    @staticmethod
    def _time(value):
        try:
            at = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
            if at.tzinfo is None:
                raise ValueError()
            return at.astimezone(UTC)
        except (ValueError, TypeError, AttributeError):
            raise ValueError('日程时间须包含明确时区') from None

    @staticmethod
    def _text(value, name, limit, required=False):
        if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
            raise ValueError(name + '为空或过长')
        return value.strip()

    @staticmethod
    def _view(row):
        value = dict(row)
        value['all_day'] = bool(value['all_day'])
        return value

    def get(self, ident):
        with self.store.db() as c:
            row = c.execute('SELECT * FROM calendar_events WHERE id=?', (ident,)).fetchone()
        if not row:
            raise ValueError('日程不存在')
        return self._view(row)

    def events(self, month=None, *, start=None, end=None):
        if start is not None or end is not None:
            try:
                first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
                if not 0 <= (last-first).days <= 366:raise ValueError()
            except (ValueError, TypeError):
                raise ValueError('日程范围须为有效起止日期，最多367天') from None
            a = dt.datetime.combine(first, dt.time(), TZ).astimezone(UTC).isoformat()
            b = dt.datetime.combine(last+dt.timedelta(days=1), dt.time(), TZ).astimezone(UTC).isoformat()
            with self.store.db() as c:
                return [self._view(r) for r in c.execute('SELECT * FROM calendar_events WHERE start_at<? AND end_at>? ORDER BY start_at,id', (b,a))]
        month = month or self.store.today()[:7]
        try:
            start = dt.date.fromisoformat(month + '-01')
            end = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        except (ValueError, TypeError):
            raise ValueError('月份须为YYYY-MM') from None
        a = dt.datetime.combine(start, dt.time(), TZ).astimezone(UTC).isoformat()
        b = dt.datetime.combine(end, dt.time(), TZ).astimezone(UTC).isoformat()
        with self.store.db() as c:
            return [self._view(r) for r in c.execute('SELECT * FROM calendar_events WHERE start_at<? AND end_at>? ORDER BY start_at,id', (b, a))]

    def save(self, *, title, start_at, end_at, all_day=False, location='', notes='',
             reminder_minutes=None, event_id=None, expected_version=None, submission_id=None):
        title = self._text(title, '日程名称', 200, True)
        location = self._text(location, '地点', 500)
        notes = self._text(notes, '备注', 5000)
        start, end = self._time(start_at), self._time(end_at)
        if end <= start:
            raise ValueError('结束时间须晚于开始时间')
        if not isinstance(all_day, bool):
            raise ValueError('全天标记须为布尔值')
        if reminder_minutes is not None and (type(reminder_minutes) is not int or not 0 <= reminder_minutes <= 10080):
            raise ValueError('提醒须为0至10080分钟或关闭')
        when = start - dt.timedelta(minutes=reminder_minutes) if reminder_minutes is not None else None
        data = dict(title=title, start_at=start.isoformat(), end_at=end.isoformat(), all_day=all_day,
                    location=location, notes=notes, reminder_minutes=reminder_minutes)
        fingerprint = hashlib.sha256(dumps(data).encode()).hexdigest()
        now = self.store.stamp()
        with self.store.db(True) as c:
            if not event_id:
                submission_id = self._text(submission_id, '提交编号', 100, True)
                previous = c.execute('SELECT * FROM calendar_requests WHERE submission_id=?', (submission_id,)).fetchone()
                if previous:
                    if previous['fingerprint'] != fingerprint:
                        raise ValueError('同一提交编号不能改写日程')
                    return self._view(c.execute('SELECT * FROM calendar_events WHERE id=?',(previous['event_id'],)).fetchone())
            if when is not None and when <= self.store.now():
                raise ValueError('提醒时间已过去，请改为未来时间或关闭提醒')
            if event_id:
                if type(expected_version) is not int or expected_version < 1:
                    raise ValueError('缺少日程版本，请刷新核对')
                old = c.execute('SELECT * FROM calendar_events WHERE id=?', (event_id,)).fetchone()
                if not old or old['version'] != expected_version:
                    raise ValueError('日程已更新，请刷新后再修改')
                if old['notification_state'] == 'sending':
                    raise ValueError('提醒正在发送，请稍后再修改')
                if old['status'] != 'planned':
                    raise ValueError('已完成或已取消日程不能直接改写，请另建日程')
                version = old['version'] + 1
                c.execute('''UPDATE calendar_events SET title=?,start_at=?,end_at=?,all_day=?,location=?,notes=?,reminder_minutes=?,
                           version=?,notification_state=?,reminder_at=?,notification_receipt=NULL,updated_at=? WHERE id=?''',
                          (title,start.isoformat(),end.isoformat(),int(all_day),location,notes,reminder_minutes,version,
                           'pending' if when else 'off',when.isoformat() if when else None,now,event_id))
            else:
                event_id, version = uid(), 1
                c.execute('INSERT INTO calendar_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)',
                          (event_id,title,start.isoformat(),end.isoformat(),int(all_day),location,notes,reminder_minutes,
                           'planned',version,'pending' if when else 'off',when.isoformat() if when else None,None,now,now))
                c.execute('INSERT INTO calendar_requests VALUES(?,?,?)',(submission_id,fingerprint,event_id))
            c.execute('INSERT INTO calendar_changes VALUES(?,?,?,?,?,?)',
                      (uid(),event_id,version,'edit' if expected_version is not None else 'create',now,dumps(data)))
            return self._view(c.execute('SELECT * FROM calendar_events WHERE id=?',(event_id,)).fetchone())

    def change_status(self, event_id, expected_version, status):
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError('缺少日程版本，请刷新核对')
        if status not in ('completed','cancelled'):
            raise ValueError('日程只可明确标为完成或取消')
        with self.store.db(True) as c:
            old = c.execute('SELECT * FROM calendar_events WHERE id=?',(event_id,)).fetchone()
            if not old:
                raise ValueError('日程不存在')
            if old['status'] == status and old['version'] == expected_version + 1:
                return self._view(old)  # retry of the identical state transition
            if old['version'] != expected_version or old['status'] != 'planned':
                raise ValueError('日程状态已变化，请刷新核对')
            if old['notification_state'] == 'sending':
                raise ValueError('提醒正在发送，请稍后再修改')
            now = self.store.stamp()
            c.execute('UPDATE calendar_events SET status=?,version=version+1,notification_state=?,completed_at=?,updated_at=? WHERE id=?',
                      (status,'cancelled',now if status=='completed' else None,now,event_id))
            c.execute('INSERT INTO calendar_changes VALUES(?,?,?,?,?,?)',(uid(),event_id,old['version']+1,status,now,'用户明确标记日程'+status))
            return self._view(c.execute('SELECT * FROM calendar_events WHERE id=?',(event_id,)).fetchone())

    def recover_uncertain(self):
        with self.store.db(True) as c:
            c.execute("UPDATE calendar_events SET notification_state='unknown' WHERE notification_state='sending'")

    def reminders(self, config, publisher=None):
        from .notifications import publish_notification
        publisher = publisher or publish_notification
        now = self.store.stamp()
        with self.store.db(True) as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM calendar_events WHERE status='planned' AND notification_state='pending' AND reminder_at<=?",(now,))]
        for row in rows:
            # Do not replay an old appointment after a long outage.
            state = 'expired' if self._time(row['end_at']) <= self.store.now() else 'page_only' if not config.get('notification',{}).get('url') else 'sending'
            with self.store.db(True) as c:
                count = c.execute("UPDATE calendar_events SET notification_state=? WHERE id=? AND version=? AND notification_state='pending' AND status='planned'",(state,row['id'],row['version'])).rowcount
            if not count or state != 'sending':
                continue
            payload = {'title':'起居 · 日程提醒','message':'你安排的日程即将开始，请打开查看。',
                       'url':config.get('public_url','http://127.0.0.1:18780').rstrip('/')+'/#calendar/'+row['id'],
                       'idempotency_key':row['id']+'-'+str(row['version'])}
            try:
                receipt = publisher(config['notification'],payload)
                state = 'accepted'
            except Exception:
                state, receipt = 'unknown', '发送结果未确认；不自动重发，请核对通知或日程页'
            with self.store.db(True) as c:
                c.execute("UPDATE calendar_events SET notification_state=?,notification_receipt=? WHERE id=? AND version=? AND notification_state='sending'",(state,receipt,row['id'],row['version']))

    def completed_entries(self):
        with self.store.db() as c:
            rows=[dict(r) for r in c.execute("SELECT * FROM calendar_events WHERE status='completed'")]
        return [{'id':'calendar:'+r['id'],'kind':'calendar','title':'日程完成确认',
                 'body':'确认完成日程：'+r['title'],'business_date':self._time(r['completed_at']).astimezone(TZ).date().isoformat(),
                 'recorded_at':r['completed_at'],'status':'recorded','source_refs':['calendar:'+r['id']],
                 'activity_id':None,'links':[{'label':'打开日程','url':'#calendar/'+r['id']}]} for r in rows]
