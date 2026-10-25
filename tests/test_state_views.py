"""Fresh chat navigation avoids unrelated private payloads without caching them."""
import datetime as dt
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from dsh_personal.core import TZ, dumps
from dsh_personal.server import Application, create_server


class ChatStateViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Application(Path(self.temp.name), clock=lambda: dt.datetime(2026, 10, 23, 22, tzinfo=TZ))
        self.app.store.ensure_activity('CHAT', '2026-10-23')
        self.app.store.ensure_activity('A1', '2026-10-20')
        self.app.records.import_apply('kimi', json.dumps({'id': 'state-view-fixture', 'messages': [
            {'id': 'user-1', 'role': 'user', 'content': '这是资料展示合成记录。', 'created_at': '2026-10-23T10:00:00+08:00'}]}))
        message = self.app.records.imported_messages()[0]
        self.import_id = message['message_id']
        self.app.records.add_profile(dumps({'title': '合成旧平台已讲材料', 'body': '保留项目侧栏来源说明'}),
            item_id='delivery-fixture', scope='learning_delivery', source_refs=['import:' + self.import_id])
        self.app.records.add_profile('合成画像正文', scope='global')

    def test_chat_keeps_sidebar_but_does_not_build_unrelated_modules(self):
        full = self.app.state()
        with patch.object(self.app, 'history', side_effect=AssertionError('history should not be constructed')), \
             patch.object(self.app.records, 'imported_messages', side_effect=AssertionError('raw transcript should not be constructed')), \
             patch.object(self.app.records, 'weekly_review', side_effect=AssertionError('weekly review should not be constructed')), \
             patch.object(self.app.records, 'growth', side_effect=AssertionError('growth should not be constructed')), \
             patch.object(self.app.calendar, 'events', side_effect=AssertionError('calendar should not be constructed')), \
             patch.object(self.app, 'config', side_effect=AssertionError('notification/storage diagnostics unnecessary')):
            chat = self.app.state(view='chat')
        for key in ['today', 'date', 'activities', 'recent_activities', 'routine_projects', 'schedules', 'budget', 'chronicle_settings']:
            self.assertEqual(full[key], chat[key], key)
        self.assertTrue(full['profiles'])
        self.assertTrue(full['journal'])
        for key in ['chronicle', 'profiles', 'journal', 'imports', 'calendar_events', 'tasks']:
            self.assertEqual([], chat[key], key)
        self.assertEqual({'items': []}, chat['growth'])
        self.assertIsNone(chat['weekly_review'])
        self.assertFalse(chat['health']['isolated'])

    def test_sidebar_delivery_is_fresh_after_source_deletion(self):
        first = self.app.state(view='chat')
        self.assertEqual(1, len(first['routine_projects'][0]['historical_deliveries']))
        self.app.records.delete_imported(self.import_id)
        after = self.app.state(view='chat')
        self.assertEqual([], after['routine_projects'][0]['historical_deliveries'])
        self.assertEqual([], self.app.state()['routine_projects'][0]['historical_deliveries'])

    def test_chat_honors_both_instance_and_records_restore_quarantine(self):
        self.app.store.set_meta('isolated', 'true')
        isolated = self.app.state(view='chat')
        self.assertTrue(isolated['health']['isolated'])
        self.assertEqual([], isolated['activities'])
        self.assertNotIn('routine_projects', isolated)
        self.app.store.set_meta('isolated', 'false')
        with self.app.store.db(True) as db:
            db.execute("INSERT OR REPLACE INTO records_meta VALUES('quarantine','1')")
        isolated = self.app.state(view='chat')
        self.assertTrue(isolated['health']['isolated'])
        self.assertEqual([], isolated['profiles'])
        self.assertNotIn('routine_projects', isolated)

    def test_http_view_still_requires_session_and_rejects_unknown_mode(self):
        server = create_server(self.app, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.app.sessions['isolated-state-test'] = time.time() + 60
        def request(path, authenticated=False):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=5)
            try:
                connection.request('GET', path, headers={'Cookie': 'dsh_session=isolated-state-test'} if authenticated else {})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()
        try:
            self.assertEqual(401, request('/api/state?view=chat')[0])
            status, body = request('/api/state?view=chat', True)
            self.assertEqual(200, status)
            self.assertEqual([], body['profiles'])
            self.assertEqual(400, request('/api/state?view=other', True)[0])
            self.assertTrue(request('/api/state', True)[1]['profiles'])
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
