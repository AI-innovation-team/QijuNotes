import base64
import datetime as dt
import http.server
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from dsh_personal.notifications import NotificationConfigError, publish_notification
from dsh_personal.server import Application
from scripts.prepare_ntfy import prepare

ARCHIVE = Path(__file__).resolve().parents[1] / '.runtime/ntfy/ntfy_2.32.0_linux_amd64.tar.gz'
PAYLOAD = {'title': '每日反思', 'message': '活动已就绪', 'url': 'http://192.0.2.10:18780/#activity/fixture-activity', 'idempotency_key': 'fixture-notification'}


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.token = self.root / 'publisher.token';self.token.write_text('tk_' + 'a' * 29);self.token.chmod(0o600)
        self.seen = []

    def server(self, handler):
        owner = self
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.seen.append({'body': body, 'authorization': self.headers.get('Authorization')})
                status, headers, response = handler(body)
                self.send_response(status)
                for key, value in headers.items(): self.send_header(key, value)
                self.end_headers();self.wfile.write(json.dumps(response).encode())
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        return 'http://127.0.0.1:' + str(server.server_port)

    def config(self, url):
        return {'backend': 'ntfy', 'url': url, 'topic': 'dsh-activities', 'token_file': str(self.token)}

    def test_webhook_contract_retained_and_extra_body_excluded(self):
        url = self.server(lambda _: (202, {}, {}))
        self.assertEqual(publish_notification({'url': url}, {**PAYLOAD, 'diary': 'never send'}), '202')
        self.assertEqual(self.seen[0]['body'], PAYLOAD)

    def test_ntfy_maps_chinese_link_sequence_and_minimal_receipt(self):
        url = self.server(lambda p: (200, {}, {'id': 'receipt123', 'event': 'message', 'topic': p['topic'], 'sequence_id': p['sequence_id'], 'message': 'not copied to receipt'}))
        receipt = json.loads(publish_notification(self.config(url), PAYLOAD))
        body = self.seen[0]['body']
        self.assertEqual(body['title'], '每日反思');self.assertEqual(body['message'], '活动已就绪')
        self.assertEqual(body['click'], PAYLOAD['url']);self.assertEqual(body['sequence_id'], PAYLOAD['idempotency_key'])
        self.assertEqual(set(body), {'topic','title','message','click','sequence_id','priority'})
        self.assertEqual(receipt['state'], 'accepted');self.assertNotIn('message', receipt)
        self.assertNotIn('tk_', json.dumps(receipt));self.assertEqual(self.seen[0]['authorization'], 'Bearer ' + self.token.read_text())

    def test_redirect_never_forwards_authentication_or_payload(self):
        destination = self.server(lambda p: (200, {}, p))
        redirect = self.server(lambda _: (307, {'Location': destination}, {}))
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            publish_notification(self.config(redirect), PAYLOAD)
        self.assertEqual(ctx.exception.code, 307);self.assertEqual(len(self.seen), 1)

    def test_unverifiable_2xx_is_unknown_and_worker_does_not_repeat(self):
        url = self.server(lambda _: (200, {}, {'id': 'unrelated'}))
        now=[dt.datetime(2026,10,22,4,tzinfo=dt.timezone.utc)]
        app = Application(self.root / 'app', lambda: now[0])
        app.worker.config = lambda: {'notification': self.config(url), 'public_url': 'http://192.0.2.10:18780'}
        a = app.store.ensure_activity('A2', origin='scheduled');now[0]+=dt.timedelta(seconds=11)
        app.worker.notifications();app.worker.notifications()
        self.assertEqual(len(self.seen), 1)
        self.assertEqual(app.store.activity(a['id'])['activity']['notification_state'], 'unknown')

    def test_invalid_configuration_refuses_before_send(self):
        for url in ('http://example.com', 'http://192.168.1.2', 'http://user:pass@127.0.0.1', 'http://127.0.0.1/topic', 'http://127.0.0.1?token=secret'):
            with self.subTest(url=url), self.assertRaises(NotificationConfigError):
                publish_notification(self.config(url), PAYLOAD)
        self.token.chmod(0o644)
        with self.assertRaises(NotificationConfigError): publish_notification(self.config('http://127.0.0.1'), PAYLOAD)
        self.token.chmod(0o600);link=self.root/'linked.token';link.symlink_to(self.token)
        with self.assertRaises(NotificationConfigError): publish_notification({**self.config('http://127.0.0.1'),'token_file':str(link)}, PAYLOAD)


@unittest.skipUnless(ARCHIVE.exists(), 'pinned ntfy archive required for actual integration')
class NtfyIntegrationTests(unittest.TestCase):
    def test_actual_ntfy_auth_acl_receipt_worker_once_and_prepare_no_overwrite(self):
        with tempfile.TemporaryDirectory(prefix='dsh-ntfy-test-') as temporary:
            root = Path(temporary)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0));port=sock.getsockname()[1]
            setup = prepare(root/'server', root/'secrets', root/'notify.service', '127.0.0.1', port, archive=ARCHIVE)
            self.assertFalse(setup['service_started']);self.assertFalse(setup['application_config_changed'])
            phone = json.loads(Path(setup['phone_credentials_file']).read_text())
            auth = 'Basic ' + base64.b64encode((phone['username']+':'+phone['password']).encode()).decode()
            publisher = 'Bearer ' + Path(setup['notification']['token_file']).read_text().strip()
            before = Path(setup['phone_credentials_file']).read_bytes()
            with self.assertRaisesRegex(ValueError, '拒绝覆盖'):
                prepare(root/'server', root/'secrets', root/'notify.service', '127.0.0.1', port, archive=ARCHIVE)
            self.assertEqual(Path(setup['phone_credentials_file']).read_bytes(), before)
            self.assertEqual(Path(setup['notification']['token_file']).stat().st_mode & 0o777, 0o600)
            self.assertEqual(Path(setup['phone_credentials_file']).stat().st_mode & 0o777, 0o600)
            env={key:value for key,value in os.environ.items() if not key.startswith('NTFY_')}
            process = subprocess.Popen([setup['binary'],'serve','--config',setup['server_config']],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=env)
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base=setup['notification']['url'];topic=setup['notification']['topic']
            def request(path, authorization=None, data=None):
                headers={'Authorization':authorization} if authorization else {}
                if data is not None:headers['Content-Type']='application/json';data=json.dumps(data).encode()
                return opener.open(urllib.request.Request(base+path,data,headers),timeout=3)
            try:
                for _ in range(100):
                    if process.poll() is not None:self.fail('isolated ntfy server exited')
                    try:
                        with request('/v1/health') as response:
                            if response.status==200:break
                    except OSError:time.sleep(.05)
                else:self.fail('isolated ntfy not ready')
                for args in [('/'+topic+'/json?poll=1',None,None),('/',None,{'topic':topic,'message':'forbidden'}),('/'+topic+'/json?poll=1',publisher,None),('/',auth,{'topic':topic,'message':'forbidden'}),('/other-topic/json?poll=1',auth,None)]:
                    with self.subTest(path=args[0],auth_role='present' if args[1] else 'anonymous'),self.assertRaises(urllib.error.HTTPError) as error:
                        request(*args)
                    self.assertIn(error.exception.code,(401,403))
                now=[dt.datetime(2026,10,22,4,tzinfo=dt.timezone.utc)]
                app=Application(root/'app',lambda:now[0])
                app.worker.config=lambda:{'notification':setup['notification'],'public_url':'http://192.0.2.10:18780'}
                a=app.store.ensure_activity('A2',origin='scheduled');now[0]+=dt.timedelta(seconds=11)
                app.worker.notifications();app.worker.notifications()
                with request('/'+topic+'/json?poll=1&since=all',auth) as response:
                    messages=[json.loads(line) for line in response.read().decode().splitlines() if line]
                self.assertEqual(len(messages),1);message=messages[0]
                self.assertEqual(message['title'],'每日反思');self.assertEqual(message['message'],'活动已就绪')
                self.assertEqual(message['click'],'http://192.0.2.10:18780/#activity/'+a['id'])
                with app.store.db() as db:row=dict(db.execute('SELECT * FROM notifications WHERE activity_id=?',(a['id'],)).fetchone())
                self.assertEqual(row['state'],'accepted');self.assertEqual(row['attempts'],1)
                receipt=json.loads(row['receipt']);self.assertEqual(receipt['message_id'],message['id'])
                self.assertEqual(receipt['sequence_id'],row['id']);self.assertEqual(message['sequence_id'],row['id'])
            finally:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait()


if __name__ == '__main__':
    unittest.main()
