"""Read-only real observation analysis. Never construct or initialise a Store."""
from __future__ import annotations

from contextlib import closing
import datetime as dt
import json
from pathlib import Path
import sqlite3
from urllib.parse import quote
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
UTC = dt.timezone.utc
GAP_LIMIT_SECONDS = 180
REQUIRED_FULL_DAYS = 7


def _aware(value):
    if not isinstance(value, str):
        raise ValueError('timestamp must be an ISO string')
    result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('timestamp has no explicit timezone')
    return result.astimezone(UTC)


def _gap(start, end, kind):
    return {'from': start.isoformat(), 'to': end.isoformat(),
            'seconds': (end - start).total_seconds(), 'kind': kind}


def analyze_observations(rows, started_at, now, *, activities=None, backup=None):
    """Pure calculation; test clocks are injected here, never into production."""
    if not isinstance(now, dt.datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('checked_at requires an aware datetime')
    now = now.astimezone(UTC)
    issues = []
    try:
        start = _aware(started_at)
    except (TypeError, ValueError, OverflowError):
        start = None
        issues.append({'kind': 'invalid_observation_start'})
    if start is not None and start > now:
        issues.append({'kind': 'future_observation_start', 'at': start.isoformat()})
    usable_start = start is not None and start <= now
    parsed_rows = []
    excluded = {'invalid_timestamp': 0, 'future_timestamp': 0, 'before_observation_start': 0}
    for row in rows:
        kind = row.get('kind')
        try:
            at = _aware(row.get('at'))
        except (TypeError, ValueError, OverflowError):
            excluded['invalid_timestamp'] += 1
            issues.append({'kind': 'invalid_timestamp', 'observation_kind': kind})
            continue
        if at > now:
            excluded['future_timestamp'] += 1
            issues.append({'kind': 'future_timestamp', 'observation_kind': kind, 'at': at.isoformat()})
            continue
        if usable_start and at < start:
            excluded['before_observation_start'] += 1
            continue
        parsed_rows.append((at, row))
    parsed_rows.sort(key=lambda item: item[0])
    beats = [(at, row) for at, row in parsed_rows if row.get('kind') == 'heartbeat']
    epochs, metadata_issues = [], []
    for at, row in beats:
        try:
            details = json.loads(row.get('details', '{}'))
            if not isinstance(details, dict):
                raise ValueError('heartbeat details must be an object')
        except (TypeError, ValueError):
            metadata_issues.append({'kind': 'invalid_heartbeat_details', 'at': at.isoformat()})
            details = {}
        version = {'version': details.get('version'),
                   'source_commit': details.get('source_commit', 'uncommitted-development')}
        if not epochs or any(epochs[-1][key] != value for key, value in version.items()):
            epochs.append({'since': at.isoformat(), **version})
    intervals = []
    if usable_start:
        if beats:
            intervals.append(_gap(start, beats[0][0], 'head'))
            intervals.extend(_gap(left[0], right[0], 'internal') for left, right in zip(beats, beats[1:]))
            intervals.append(_gap(beats[-1][0], now, 'tail'))
        else:
            intervals.append(_gap(start, now, 'no_heartbeats'))
    gaps = [item for item in intervals if item['seconds'] > GAP_LIMIT_SECONDS]
    heartbeat_issues = [issue for issue in issues if issue.get('observation_kind') == 'heartbeat'
                        or issue['kind'] in ('invalid_observation_start', 'future_observation_start')]
    coverage_status = ('UNVERIFIABLE_TIMESTAMP_DATA' if not usable_start or heartbeat_issues
                       else 'NO_HEARTBEATS' if not beats else 'GAPS_DETECTED' if gaps
                       else 'COVERED_WITHIN_THRESHOLD')
    local_start = start.astimezone(TZ) if start else None
    # Keep the established boundary: exclude the starting partial date, then
    # observe the next seven complete local dates. Do not reset this start.
    earliest_end = (dt.datetime.combine(local_start.date() + dt.timedelta(days=8), dt.time.min, TZ)
                    if local_start else None)
    completed_days = []
    if usable_start:
        day = local_start.date() + dt.timedelta(days=1)
        while day < now.astimezone(TZ).date():
            beginning = dt.datetime.combine(day, dt.time.min, TZ).astimezone(UTC)
            ending = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, TZ).astimezone(UTC)
            day_gaps = [gap for gap in gaps if _aware(gap['from']) < ending and _aware(gap['to']) > beginning]
            status = ('UNVERIFIABLE_TIMESTAMP_DATA' if heartbeat_issues else 'NO_HEARTBEATS' if not beats
                      else 'GAPS_DETECTED' if day_gaps else 'COVERED_WITHIN_THRESHOLD')
            completed_days.append({'date': day.isoformat(), 'status': status,
                                   'heartbeat_count': sum(beginning <= at < ending for at, _ in beats),
                                   'gap_kinds': sorted({gap['kind'] for gap in day_gaps})})
            day += dt.timedelta(days=1)
    run = longest = 0
    for day in completed_days:
        run = run + 1 if day['status'] == 'COVERED_WITHIN_THRESHOLD' else 0
        longest = max(longest, run)
    elapsed_reached = bool(usable_start and now >= earliest_end)
    return {
        'kind': 'actual_elapsed_time_only',
        'started_at': local_start.isoformat() if local_start else None,
        'earliest_seven_full_days_end': earliest_end.isoformat() if earliest_end else None,
        'checked_at': now.isoformat(),
        'elapsed_seconds': (now - start).total_seconds() if start else None,
        'observed_days': sorted({at.astimezone(TZ).date().isoformat() for at, _ in parsed_rows}),
        'version_epochs': epochs,
        # Existing field extended to include the previously missing boundaries.
        'heartbeat_gaps_over_180s': gaps,
        'heartbeat_coverage': {
            'status': coverage_status, 'threshold_seconds': GAP_LIMIT_SECONDS,
            'valid_heartbeat_count': len(beats),
            'first_heartbeat_at': beats[0][0].isoformat() if beats else None,
            'last_heartbeat_at': beats[-1][0].isoformat() if beats else None,
            'head_seconds': (beats[0][0] - start).total_seconds() if beats and usable_start else None,
            'tail_seconds': (now - beats[-1][0]).total_seconds() if beats else None,
            'max_interval_seconds': max((item['seconds'] for item in intervals), default=None),
            'missing_heartbeat_evidence': not bool(beats),
            'timestamp_issues': heartbeat_issues, 'metadata_issues': metadata_issues,
        },
        'natural_day_coverage': {
            'timezone': 'Asia/Shanghai', 'required_full_days': REQUIRED_FULL_DAYS,
            'starting_partial_date_excluded': local_start.date().isoformat() if local_start else None,
            'first_eligible_full_date': (local_start.date() + dt.timedelta(days=1)).isoformat() if local_start else None,
            'completed_calendar_days': len(completed_days),
            'covered_full_days': sum(day['status'] == 'COVERED_WITHIN_THRESHOLD' for day in completed_days),
            'longest_consecutive_covered_days': longest,
            'trailing_consecutive_covered_days': run,
            'elapsed_boundary_reached': elapsed_reached,
            'seven_consecutive_full_days_covered': elapsed_reached and longest >= REQUIRED_FULL_DAYS,
            'days': completed_days, 'acceptance': 'NOT_AUTOMATIC_PASS',
        },
        'excluded_observations': excluded, 'timestamp_issues': issues,
        'activities': activities or [], 'backup': backup,
        'status': ('PENDING_REVIEW' if elapsed_reached else 'OBSERVING_NOT_PASS') if usable_start else 'UNAVAILABLE_NOT_PASS',
        'note': '时间达到也不自动通过；须核对头部、内部和尾部心跳覆盖、版本、故障、真实回复、备份与用户体验。',
    }


def observation_report(data, *, now=None):
    """Read one mode=ro SQLite snapshot; never create a missing database."""
    path = Path(data).expanduser().resolve() / 'personal.sqlite'
    try:
        with closing(sqlite3.connect('file:' + quote(str(path), safe='/') + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            meta = {row['key']: row['value'] for row in db.execute(
                "SELECT key,value FROM meta WHERE key IN ('observation_started','last_backup')")}
            rows = [dict(row) for row in db.execute('SELECT at,kind,details FROM observations ORDER BY at')]
            activities = [dict(row) for row in db.execute(
                'SELECT routine,business_date,scheduled_at,triggered_at,ready_at,status,notification_state '
                'FROM activities ORDER BY business_date,routine')]
    except (sqlite3.Error, OSError) as exc:
        report = analyze_observations([], None, now or dt.datetime.now(UTC))
        report['read_error'] = {'type': type(exc).__name__, 'database_exists': path.is_file(),
                                'message': '无法只读加载观察数据库；未创建或修复任何数据。'}
        return report
    # Capture now after the snapshot so a heartbeat racing the open is not
    # classified as future merely because the diagnostic started first.
    return analyze_observations(rows, meta.get('observation_started'), now or dt.datetime.now(UTC),
                                activities=activities, backup=meta.get('last_backup'))
