"""Synthetic observation fixtures only: no production data or clock changes."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from dsh_personal.observation import analyze_observations, observation_report, TZ


START = dt.datetime(2026, 10, 22, 18, 55, 10, 670098, TZ)


def heartbeat(at, details=None):
    return {'at': at.isoformat(), 'kind': 'heartbeat',
            'details': json.dumps({'version': 'fixture', 'source_commit': 'fixture-source'})
            if details is None else details}


def regular_beats(start, end, seconds=120):
    result = []
    at = start
    while at <= end:
        result.append(heartbeat(at))
        at += dt.timedelta(seconds=seconds)
    return result


class ObservationAnalysisTests(unittest.TestCase):
    def analyze(self, rows, now, started=START):
        return analyze_observations(rows, started.isoformat() if started else None, now)

    def test_normal_unfinished_period_preserves_start_boundary_and_never_passes(self):
        now = START + dt.timedelta(hours=6)
        result = self.analyze(regular_beats(START, now), now)
        self.assertEqual(result['started_at'], START.isoformat())
        self.assertEqual(result['earliest_seven_full_days_end'], '2026-10-30T00:00:00+08:00')
        self.assertEqual(result['heartbeat_coverage']['status'], 'COVERED_WITHIN_THRESHOLD')
        self.assertEqual(result['heartbeat_gaps_over_180s'], [])
        self.assertEqual(result['natural_day_coverage']['completed_calendar_days'], 0)
        self.assertFalse(result['natural_day_coverage']['seven_consecutive_full_days_covered'])
        self.assertEqual(result['status'], 'OBSERVING_NOT_PASS')

    def test_internal_gap_is_retained_and_distinguished_from_boundaries(self):
        now = START + dt.timedelta(seconds=600)
        rows = [heartbeat(START), heartbeat(START + dt.timedelta(seconds=60)),
                heartbeat(START + dt.timedelta(seconds=540)), heartbeat(now)]
        result = self.analyze(rows, now)
        self.assertEqual([(g['kind'], g['seconds']) for g in result['heartbeat_gaps_over_180s']],
                         [('internal', 480)])
        self.assertEqual(result['heartbeat_coverage']['head_seconds'], 0)
        self.assertEqual(result['heartbeat_coverage']['tail_seconds'], 0)

    def test_stopped_service_reports_stale_tail_despite_clean_internal_intervals(self):
        last = START + dt.timedelta(seconds=240)
        now = last + dt.timedelta(seconds=601)
        result = self.analyze(regular_beats(START, last), now)
        self.assertEqual([(g['kind'], g['seconds']) for g in result['heartbeat_gaps_over_180s']],
                         [('tail', 601)])
        self.assertEqual(result['heartbeat_coverage']['status'], 'GAPS_DETECTED')
        self.assertEqual(result['heartbeat_coverage']['tail_seconds'], 601)

    def test_first_late_heartbeat_reports_missing_head(self):
        first = START + dt.timedelta(seconds=301)
        now = first + dt.timedelta(seconds=240)
        result = self.analyze(regular_beats(first, now), now)
        self.assertEqual([(g['kind'], g['seconds']) for g in result['heartbeat_gaps_over_180s']],
                         [('head', 301)])
        self.assertEqual(result['heartbeat_coverage']['head_seconds'], 301)

    def test_no_heartbeats_never_looks_covered_even_inside_threshold(self):
        for seconds in (60, 181):
            with self.subTest(seconds=seconds):
                now = START + dt.timedelta(seconds=seconds)
                result = self.analyze([{'at': now.isoformat(), 'kind': 'backup', 'details': '{}'}], now)
                self.assertEqual(result['heartbeat_coverage']['status'], 'NO_HEARTBEATS')
                self.assertTrue(result['heartbeat_coverage']['missing_heartbeat_evidence'])
                self.assertEqual(len(result['heartbeat_gaps_over_180s']), int(seconds > 180))

    def test_future_heartbeat_cannot_mask_tail_or_add_future_observed_day(self):
        now = START + dt.timedelta(minutes=10)
        future = now + dt.timedelta(days=1)
        result = self.analyze([heartbeat(START), heartbeat(future)], now)
        self.assertEqual(result['heartbeat_coverage']['valid_heartbeat_count'], 1)
        self.assertEqual(result['heartbeat_coverage']['tail_seconds'], 600)
        self.assertEqual(result['heartbeat_coverage']['status'], 'UNVERIFIABLE_TIMESTAMP_DATA')
        self.assertEqual(result['excluded_observations']['future_timestamp'], 1)
        self.assertNotIn(future.date().isoformat(), result['observed_days'])
        self.assertEqual(result['heartbeat_gaps_over_180s'][0]['kind'], 'tail')

    def test_invalid_and_naive_timestamps_are_explicit_not_silently_healthy(self):
        now = START + dt.timedelta(seconds=60)
        for value in ('invalid', '2026-10-22T18:55:10', None):
            with self.subTest(value=value):
                result = self.analyze([heartbeat(START), {'at': value, 'kind': 'heartbeat', 'details': '{}'}], now)
                self.assertEqual(result['heartbeat_coverage']['status'], 'UNVERIFIABLE_TIMESTAMP_DATA')
                self.assertEqual(result['excluded_observations']['invalid_timestamp'], 1)
                self.assertEqual(result['heartbeat_coverage']['timestamp_issues'][0]['kind'], 'invalid_timestamp')

    def test_missing_and_future_observation_start_cannot_establish_coverage(self):
        now = START + dt.timedelta(minutes=1)
        for start in (None, now + dt.timedelta(days=1)):
            with self.subTest(start=start):
                result = self.analyze([heartbeat(START)], now, start)
                self.assertEqual(result['status'], 'UNAVAILABLE_NOT_PASS')
                self.assertEqual(result['heartbeat_coverage']['status'], 'UNVERIFIABLE_TIMESTAMP_DATA')
                self.assertFalse(result['natural_day_coverage']['seven_consecutive_full_days_covered'])
                self.assertEqual(result['natural_day_coverage']['days'], [])

    def test_offsets_are_sorted_by_actual_instant_and_prestart_rows_excluded(self):
        now = START + dt.timedelta(seconds=120)
        rows = [heartbeat(now.astimezone(dt.timezone.utc)), heartbeat(START),
                heartbeat(START - dt.timedelta(days=1))]
        result = self.analyze(rows, now)
        self.assertEqual(result['heartbeat_gaps_over_180s'], [])
        self.assertEqual(result['heartbeat_coverage']['head_seconds'], 0)
        self.assertEqual(result['heartbeat_coverage']['tail_seconds'], 0)
        self.assertEqual(result['excluded_observations']['before_observation_start'], 1)
        self.assertEqual(result['observed_days'], ['2026-10-22'])

    def test_seven_complete_natural_days_are_measured_but_never_accepted_automatically(self):
        now = dt.datetime(2026, 10, 30, 0, 0, tzinfo=TZ)
        result = self.analyze(regular_beats(START, now), now)
        coverage = result['natural_day_coverage']
        self.assertEqual(coverage['completed_calendar_days'], 7)
        self.assertEqual(coverage['covered_full_days'], 7)
        self.assertEqual(coverage['longest_consecutive_covered_days'], 7)
        self.assertEqual(coverage['trailing_consecutive_covered_days'], 7)
        self.assertTrue(coverage['seven_consecutive_full_days_covered'])
        self.assertEqual(coverage['days'][0]['date'], '2026-10-23')
        self.assertEqual(coverage['days'][-1]['date'], '2026-10-29')
        self.assertEqual(coverage['acceptance'], 'NOT_AUTOMATIC_PASS')
        self.assertEqual(result['status'], 'PENDING_REVIEW')

    def test_gap_across_midnight_invalidates_both_full_days_without_resetting_start(self):
        now = dt.datetime(2026, 10, 30, 0, 0, tzinfo=TZ)
        missing_from = dt.datetime(2026, 10, 24, 23, 57, tzinfo=TZ)
        missing_to = dt.datetime(2026, 10, 25, 0, 3, tzinfo=TZ)
        rows = [row for row in regular_beats(START, now)
                if not missing_from <= dt.datetime.fromisoformat(row['at']) <= missing_to]
        result = self.analyze(rows, now)
        coverage = result['natural_day_coverage']
        self.assertEqual([day['date'] for day in coverage['days'] if day['status'] == 'GAPS_DETECTED'],
                         ['2026-10-24', '2026-10-25'])
        self.assertEqual(coverage['covered_full_days'], 5)
        self.assertEqual(coverage['longest_consecutive_covered_days'], 4)
        self.assertFalse(coverage['seven_consecutive_full_days_covered'])
        self.assertTrue(coverage['elapsed_boundary_reached'])
        self.assertEqual(result['started_at'], START.isoformat())
        self.assertEqual(result['status'], 'PENDING_REVIEW')

    def test_stale_tail_marks_each_completed_uncovered_day(self):
        now = dt.datetime(2026, 10, 25, 0, 0, tzinfo=TZ)
        result = self.analyze([heartbeat(START)], now)
        self.assertEqual(result['natural_day_coverage']['covered_full_days'], 0)
        self.assertEqual([day['gap_kinds'] for day in result['natural_day_coverage']['days']],
                         [['tail'], ['tail']])

    def test_corrupt_details_are_visible_without_crashing_or_fabricating_epoch(self):
        result = self.analyze([heartbeat(START, '{broken-json')], START)
        self.assertEqual(result['heartbeat_coverage']['metadata_issues'][0]['kind'], 'invalid_heartbeat_details')
        self.assertIsNone(result['version_epochs'][0]['version'])
        self.assertEqual(result['version_epochs'][0]['source_commit'], 'uncommitted-development')


class ReadOnlyObservationTests(unittest.TestCase):
    def fixture(self, root, started=True):
        path = root / 'personal.sqlite'
        with sqlite3.connect(path) as db:
            db.executescript('''CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE observations(at TEXT,kind TEXT,details TEXT);
                CREATE TABLE activities(routine TEXT,business_date TEXT,scheduled_at TEXT,
                triggered_at TEXT,ready_at TEXT,status TEXT,notification_state TEXT);''')
            if started:
                db.execute('INSERT INTO meta VALUES (?,?)', ('observation_started', START.isoformat()))
            db.execute('INSERT INTO meta VALUES (?,?)', ('last_backup', 'fixture-backup-marker'))
            row = heartbeat(START)
            db.execute('INSERT INTO observations VALUES (?,?,?)', (row['at'], row['kind'], row['details']))
            db.execute('INSERT INTO activities VALUES (?,?,?,?,?,?,?)',
                       ('A1', '2026-10-22', 'fixture', 'fixture', 'fixture', 'ready', 'accepted'))
        return path

    def snapshot(self, root):
        return {path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns,
                            path.stat().st_mode) for path in root.iterdir() if path.is_file()}

    def test_existing_database_is_unchanged_and_legacy_projection_is_preserved(self):
        with tempfile.TemporaryDirectory(prefix='observe-fixture-') as temp:
            root = Path(temp)
            self.fixture(root)
            before = self.snapshot(root)
            with patch('dsh_personal.core.Store', side_effect=AssertionError('Must never initialise Store')):
                result = observation_report(root, now=START + dt.timedelta(seconds=60))
            self.assertEqual(self.snapshot(root), before)
            self.assertEqual(result['backup'], 'fixture-backup-marker')
            self.assertEqual(result['activities'][0]['business_date'], '2026-10-22')
            self.assertNotIn('read_error', result)
            self.assertEqual(result['heartbeat_coverage']['status'], 'COVERED_WITHIN_THRESHOLD')

    def test_missing_start_is_reported_without_initializing_meta(self):
        with tempfile.TemporaryDirectory(prefix='observe-fixture-') as temp:
            root = Path(temp)
            self.fixture(root, started=False)
            before = self.snapshot(root)
            result = observation_report(root, now=START)
            self.assertEqual(result['status'], 'UNAVAILABLE_NOT_PASS')
            self.assertEqual(self.snapshot(root), before)

    def test_missing_database_and_directory_are_not_created(self):
        with tempfile.TemporaryDirectory(prefix='observe-fixture-') as temp:
            root = Path(temp) / 'missing-directory'
            result = observation_report(root, now=START)
            self.assertFalse(root.exists())
            self.assertEqual(result['status'], 'UNAVAILABLE_NOT_PASS')
            self.assertFalse(result['read_error']['database_exists'])

    def test_cli_missing_database_returns_structured_error_without_side_effects(self):
        with tempfile.TemporaryDirectory(prefix='observe-fixture-') as temp:
            root = Path(temp)
            script = Path(__file__).resolve().parents[1] / 'scripts' / 'observe.py'
            result = subprocess.run([sys.executable, str(script), '--data', str(root)],
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stderr, '')
            self.assertEqual(json.loads(result.stdout)['status'], 'UNAVAILABLE_NOT_PASS')
            self.assertEqual(list(root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
