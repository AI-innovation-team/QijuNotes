import copy,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from dsh_personal.diagnostics import readiness,boot_evidence,model_configuration_id,notification_configuration_id,notification_readiness
from dsh_personal.server import Application

class DiagnosticsTests(unittest.TestCase):
 def test_readiness_is_read_only_and_redacts_secret_values_and_urls(self):
  with tempfile.TemporaryDirectory() as temp:
   app=Application(temp);secret=Path(temp)/'private.key';secret.write_text('SECRET-MARKER');secret.chmod(0o600)
   app.config_path.write_text(json.dumps({'model':{'name':'model-name','base_url':'https://example.invalid/v1','api_key_file':str(secret)},'notification':{'url':'https://example.invalid/SECRET-IN-URL'},'backup':{}}))
   with app.store.db() as c:before=c.execute('SELECT COUNT(*) FROM observations').fetchone()[0]
   with patch('urllib.request.urlopen',side_effect=AssertionError('No network allowed')):
    result=readiness(temp)
   self.assertTrue(result['model']['configured']);self.assertNotIn('SECRET',json.dumps(result));self.assertFalse(result['model']['actual_provider_verified'])
   with app.store.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM observations').fetchone()[0],before)
 def test_empty_missing_data_never_initializes_a_database(self):
  with tempfile.TemporaryDirectory() as temp:
   result=readiness(temp);self.assertEqual(len(result['model']['missing_fields']),3)
   self.assertFalse((Path(temp)/'personal.sqlite').exists())
 def test_real_verification_requires_matching_receipt_and_unchanged_configuration(self):
  with tempfile.TemporaryDirectory() as temp:
   app=Application(temp);secret=Path(temp)/'provider.key';secret.write_text('fixture-private-key');secret.chmod(0o600)
   model={'base_url':'https://api.deepseek.com','name':'deepseek-flash','api_key_file':str(secret)}
   app.config_path.write_text(json.dumps({'model':model}))
   rid=app.store.reserve(100,'foreground')
   app.store.set_meta('provider_verification',json.dumps({'configuration_id':model_configuration_id(model),'receipt_id':rid,'verified_at':'fixture'}))
   self.assertFalse(readiness(temp)['model']['actual_provider_verified'])
   from dsh_personal.gateway import Gateway
   gateway=Gateway(app.store,app.config_path,'fixture')
   gateway.begin_receipt(rid,'deepseek-flash','max_tokens',32,False)
   with app.store.db(True) as db:
    db.execute("UPDATE usage SET state='settled',actual=21 WHERE id=?",(rid,))
    db.execute("UPDATE usage_details SET response_state='completed',usage_state='valid',reported_total=21 WHERE reservation_id=?",(rid,))
   self.assertTrue(readiness(temp)['model']['actual_provider_verified'])
   secret.write_text('a-different-fixture-private-key')
   self.assertFalse(readiness(temp)['model']['actual_provider_verified'])
   self.assertNotIn('fixture-private',json.dumps(readiness(temp)))
 def test_boot_records_observations_without_claiming_no_login_acceptance(self):
  with patch('subprocess.check_output',return_value='Sessions=\nLinger=yes\nState=lingering\n'):
   evidence=boot_evidence('source-fixture')
  self.assertEqual(evidence['user_sessions'],[]);self.assertEqual(evidence['acceptance'],'OBSERVATION_ONLY_NOT_PASS')
  with patch('subprocess.check_output',side_effect=OSError()):
   self.assertIsNone(boot_evidence('source-fixture')['user_sessions'])


class NotificationReadinessTests(unittest.TestCase):
    def fixture(self, directory):
        root = Path(directory)
        token = root / 'SECRET-PATH.token'
        token.write_text('tk_' + 'A' * 29)
        token.chmod(0o600)
        conf = {
            'public_url': 'http://192.0.2.10:18780',
            'notification': {
                'backend': 'ntfy', 'url': 'http://192.0.2.10:18783',
                'topic': 'dsh-activities', 'token_file': str(token),
            },
        }
        (root / 'config.json').write_text(json.dumps(conf))
        proof = {
            'type': 'real_device_user_report',
            'locked_screen_received': True, 'correct_activity_opened': True,
            'receipt': {
                'backend': 'ntfy', 'http_status': 200, 'state': 'accepted',
                'message_id': 'receipt123', 'sequence_id': 'phone-test-02',
            },
            'verified_at': '2026-10-23T10:30:00+08:00',
            'configuration_id': notification_configuration_id(conf),
        }
        return root, token, conf, proof

    def save_proof(self, root, proof):
        path = root / 'notification-verification.json'
        path.write_text(json.dumps(proof))
        path.chmod(0o600)
        return path

    def test_verified_user_report_is_read_only_and_does_not_read_token_contents(self):
        with tempfile.TemporaryDirectory() as temp:
            root, token, conf, proof = self.fixture(temp)
            proof['untrusted_detail'] = 'SECRET-FROM-REPORT'
            proof['receipt']['raw_body'] = 'SECRET-FROM-UPSTREAM'
            self.save_proof(root, proof)
            original_open = Path.open

            def guarded_open(path, *args, **kwargs):
                if path == token:
                    raise AssertionError('Credential contents must not be read')
                return original_open(path, *args, **kwargs)

            with patch.object(Path, 'open', guarded_open), patch('urllib.request.urlopen', side_effect=AssertionError('No network allowed')):
                result = notification_readiness(root, conf)
                overall = readiness(root)
            self.assertTrue(result['configured'])
            self.assertTrue(result['real_device_verified'])
            self.assertEqual(result['verified_at'], proof['verified_at'])
            self.assertEqual(overall['notification'], result)
            self.assertFalse((root / 'personal.sqlite').exists())
            serialized = json.dumps(overall)
            self.assertNotIn('SECRET', serialized)
            self.assertNotIn('tk_' + 'A' * 29, serialized)
            self.assertNotIn('192.0.2.10', serialized)

    def test_missing_malformed_and_oversized_proof_never_verify(self):
        with tempfile.TemporaryDirectory() as temp:
            root, _, conf, proof = self.fixture(temp)
            self.assertFalse(notification_readiness(root, conf)['real_device_verified'])
            path = root / 'notification-verification.json'
            for raw in (b'{', b'null', b'[]', b'42', b'"text"', b'\xff', b' ' * 65537,
                        b'[' * 2000 + b'0' + b']' * 2000):
                with self.subTest(raw_prefix=repr(raw[:15])):
                    path.write_bytes(raw)
                    result = notification_readiness(root, conf)
                    self.assertFalse(result['real_device_verified'])
                    self.assertIsNone(result['verified_at'])
            oversized = {**proof, 'padding': 'x' * 65536}
            self.save_proof(root, oversized)
            self.assertFalse(notification_readiness(root, conf)['real_device_verified'])

    def test_server_receipt_without_successful_user_report_does_not_verify(self):
        with tempfile.TemporaryDirectory() as temp:
            root, _, conf, proof = self.fixture(temp)
            invalid = [
                ('type', 'automated_integration'), ('type', None),
                ('locked_screen_received', False), ('locked_screen_received', 1),
                ('correct_activity_opened', False), ('correct_activity_opened', 1),
                ('verified_at', ''), ('verified_at', 'not-a-time'),
                ('verified_at', '2026-10-23T10:30:00'), ('verified_at', 0),
                ('configuration_id', None), ('configuration_id', 'different-configuration'),
            ]
            for key, value in invalid:
                with self.subTest(key=key, value=value):
                    candidate = {**proof, key: value}
                    self.save_proof(root, candidate)
                    self.assertFalse(notification_readiness(root, conf)['real_device_verified'])
            for key in ('type', 'locked_screen_received', 'correct_activity_opened', 'receipt', 'verified_at', 'configuration_id'):
                with self.subTest(missing=key):
                    candidate = copy.deepcopy(proof)
                    del candidate[key]
                    self.save_proof(root, candidate)
                    self.assertFalse(notification_readiness(root, conf)['real_device_verified'])

    def test_bounded_accepted_ntfy_receipt_is_required(self):
        with tempfile.TemporaryDirectory() as temp:
            root, _, conf, proof = self.fixture(temp)
            invalid = [
                ('backend', 'webhook'), ('http_status', 403), ('http_status', '200'),
                ('state', 'unknown'), ('message_id', ''), ('message_id', 'invalid/id'),
                ('message_id', 'x' * 65), ('sequence_id', ''),
                ('sequence_id', 'invalid\nsequence'), ('sequence_id', 'x' * 65),
            ]
            for key, value in invalid:
                with self.subTest(key=key, value=value):
                    candidate = copy.deepcopy(proof)
                    candidate['receipt'][key] = value
                    self.save_proof(root, candidate)
                    self.assertFalse(notification_readiness(root, conf)['real_device_verified'])
            for receipt in (None, [], 'accepted', 200):
                with self.subTest(receipt=receipt):
                    self.save_proof(root, {**proof, 'receipt': receipt})
                    self.assertFalse(notification_readiness(root, conf)['real_device_verified'])

    def test_verification_is_bound_to_routing_and_credential_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root, token, conf, proof = self.fixture(temp)
            self.save_proof(root, proof)
            replacement = root / 'another.token'
            replacement.write_bytes(token.read_bytes())
            replacement.chmod(0o600)
            variants = []
            for key, value in (
                ('backend', 'webhook'), ('url', 'http://192.0.2.10:18784'),
                ('topic', 'different-topic'), ('token_file', str(replacement)),
            ):
                candidate = copy.deepcopy(conf)
                candidate['notification'][key] = value
                variants.append((key, candidate))
            candidate = copy.deepcopy(conf)
            candidate['public_url'] = 'http://192.0.2.10:18790'
            variants.append(('public_url', candidate))
            for key, candidate in variants:
                with self.subTest(changed=key):
                    self.assertFalse(notification_readiness(root, candidate)['real_device_verified'])
            self.assertTrue(notification_readiness(root, conf)['real_device_verified'])

    def test_credential_rotation_and_removal_invalidate_existing_proof(self):
        with tempfile.TemporaryDirectory() as temp:
            root, token, conf, proof = self.fixture(temp)
            self.save_proof(root, proof)
            old = token.stat()
            token.write_text('tk_' + 'B' * 29)
            os.utime(token, ns=(old.st_atime_ns, old.st_mtime_ns + 1000000))
            self.assertFalse(notification_readiness(root, conf)['real_device_verified'])
            proof['configuration_id'] = notification_configuration_id(conf)
            self.save_proof(root, proof)
            self.assertTrue(notification_readiness(root, conf)['real_device_verified'])
            other = root / 'replacement.token'
            other.write_bytes(token.read_bytes())
            other.chmod(0o600)
            os.replace(other, token)
            self.assertFalse(notification_readiness(root, conf)['real_device_verified'])
            token.unlink()
            self.assertIsNone(notification_configuration_id(conf))
            self.assertFalse(notification_readiness(root, conf)['real_device_verified'])
