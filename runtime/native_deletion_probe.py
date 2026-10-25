#!/usr/bin/env python3
"""Reproduce native deletion boundaries using real DSH and disposable fixture data.

This probe does not erase or rewrite native logs. Its expected result is that
archive and business tombstones do not physically erase native originals.
"""
from pathlib import Path
import http.server, json, os, sys, tempfile, threading, time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.probe import Fixture, wait_for
from runtime.combined_migration_probe import Host
from runtime.dsh_bridge import DSHError
from dsh_personal.server import Application
from dsh_personal.worker import Worker
from dsh_personal.operations import export_bundle, recovery_ledger, restore_bundle

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / 'evidence/P4/native-deletion-boundary.json'
MARKER = 'NATIVE_DELETE_FIXTURE_ONLY_d7d3fc8e'


def main():
    root = Path(tempfile.mkdtemp(prefix='dsh-native-delete-boundary-'))
    os.chmod(root, 0o700)
    source = root / 'source'
    app = Application(source)
    Fixture.requests = []
    Fixture.unavailable = False
    fixture = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    patch = json.loads((ROOT / 'runtime/personal.patch.json').read_text())
    next(p for p in patch if p.get('id') == 'llm-pi-ai')['config']['providers']['dsh-personal']['baseURL'] = f'http://127.0.0.1:{fixture.server_port}/v1'
    patch_path = root / 'fixture.patch.json'
    patch_path.write_text(json.dumps(patch))
    result = {'scope': 'real_native_DSH_isolated_fixture_deletion_boundary',
              'version': '0.2.1-rc.2', 'commit': '687f282e8625c6b8d11ff58211a90524be2e4317',
              'isolated_root': str(root), 'checks': [], 'native_erasure_implemented': False}
    hosts = []

    def record(name, details):
        result['checks'].append({'name': name, 'status': 'PASS', 'actual': details})
        print(name + ': PASS', flush=True)

    def has_original(bridge, sid):
        return MARKER in json.dumps(bridge.events(sid), ensure_ascii=False)

    try:
        host = Host(source / 'dsh', patch_path, root / 'source.log')
        hosts.append(host)
        bridge = host.start()
        activity = app.store.ensure_activity('A2', '2026-10-20')
        aid = activity['id']
        receipt = app.action({'op': 'message', 'activity_id': aid,
                              'submission_id': 'delete-boundary-original', 'body': MARKER})
        worker = Worker(app.store, bridge, app.records, app.config)
        assert worker.work_one()
        sid = app.store.activity(aid)['activity']['session_id']
        wait_for(lambda: any(e['type'] == 'turn/end' for e in bridge.events(sid)))
        worker.sync_all()
        assert has_original(bridge, sid)
        missing = {}
        for method in ('session/delete', 'session/destroy', 'session/removeMessage'):
            try:
                bridge.rpc(method, {'request': {'sessionId': sid, 'messageId': receipt['receipt']}})
            except DSHError as exc:
                missing[method] = {'code': exc.code, 'message': str(exc)}
            else:
                raise AssertionError('Unexpected supported destructive RPC: ' + method)
        record('no_exposed_native_erase_RPC', missing)
        archived = bridge.rpc('workspace/archiveSession', {'request': {'sessionId': sid}})
        assert sid in archived['archivedSessionIds'] and has_original(bridge, sid)
        host.stop()
        bridge = host.start()
        assert has_original(bridge, sid)
        record('archive_and_restart_preserve_native_original', {'session_id': sid, 'history_readable_after_archive': True, 'history_readable_after_restart': True})
        host.stop()
        before = export_bundle(app)
        deleted = app.store.delete_message(receipt['receipt'], 'fixture explicit single-original deletion')
        after = export_bundle(app)
        ledger = recovery_ledger(app)
        record('local_tombstone_does_not_claim_native_erasure', {'receipt': deleted, 'activity_state': app.store.activity(aid)['activity']['session_state']})
        for label, bundle in [('old_backup_latest_ledger', before), ('new_backup_after_delete', after)]:
            destination = root / label
            report = restore_bundle(bundle, destination, latest_ledger=ledger)
            restored = Application(destination)
            assert report['isolated'] and restored.store.meta('isolated') == 'true'
            with restored.store.db() as db:
                row = db.execute('SELECT body,deleted FROM messages WHERE id=?', (receipt['receipt'],)).fetchone()
            assert row['body'] == '' and row['deleted'] == 1
            restored_host = Host(destination / 'dsh', patch_path, root / (label + '.log'))
            hosts.append(restored_host)
            restored_bridge = restored_host.start()
            assert has_original(restored_bridge, sid)
            record(label + '_native_original_still_readable', {'local_body_cleared': True,
                    'restored_isolated': True, 'session_state': restored.store.activity(aid)['activity']['session_state'],
                    'native_history_contains_deleted_original': True})
            restored_host.stop()
        result['outcome'] = 'SUPPORTED_API_ERASURE_UNAVAILABLE; current containment works but native originals persist in both recovery paths'
    except Exception as exc:
        result['error'] = repr(exc)
        raise
    finally:
        for host in hosts:
            host.stop()
        fixture.shutdown()
        result['fixture_model_calls'] = len(Fixture.requests)
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
