#!/usr/bin/env python3
"""Real DSH fixture probe for the finite offline session cleanup adapter."""
from pathlib import Path
import hashlib, http.server, json, os, sqlite3, sys, tempfile, threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.probe import Fixture, wait_for
from runtime.combined_migration_probe import Host
from runtime.native_cleanup import NativeCleanupError, purge_sessions
from dsh_personal.server import Application
from dsh_personal.operations import export_bundle, recovery_ledger, restore_bundle

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / 'evidence/P4/native-cleanup.json'
ERASE = 'nativeerasefixture3fd917ae'
KEEP = 'nativekeepfixture7a3fed9b'
OTHER = 'nativeotherfixture951b55de'


def hashes_for_sid(home, sid):
    return {p.relative_to(home).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in home.rglob('*') if p.is_file() and sid in p.parts + (p.stem,)}


def main():
    root = Path(tempfile.mkdtemp(prefix='dsh-native-cleanup-'))
    os.chmod(root, 0o700)
    fixture = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
    Fixture.requests = []
    Fixture.unavailable = False
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    patch = json.loads((ROOT / 'runtime/personal.patch.json').read_text())
    next(p for p in patch if p.get('id') == 'llm-pi-ai')['config']['providers']['dsh-personal']['baseURL'] = f'http://127.0.0.1:{fixture.server_port}/v1'
    patch_path = root / 'fixture.patch.json'
    patch_path.write_text(json.dumps(patch))
    app = Application(root / 'source')
    hosts = []
    result = {'scope': 'real_DSH_offline_cleanup_isolated_fixture_no_production_writes',
              'dsh_version': '0.2.1-rc.2', 'isolated_root': str(root), 'checks': []}

    def record(name, actual):
        result['checks'].append({'name': name, 'status': 'PASS', 'actual': actual})
        print(name + ': PASS', flush=True)

    def finish(bridge, sid, request_id):
        wait_for(lambda: bridge.reconcile(sid, request_id)['accepted'])
        wait_for(lambda: any(e['type'] == 'turn/end' and e['data'].get('reason', {}).get('kind') == 'completed' for e in bridge.events(sid)))

    try:
        host = Host(app.data / 'dsh', patch_path, root / 'source.log')
        hosts.append(host)
        bridge = host.start()
        activity = app.store.ensure_activity('A2', '2026-10-20')
        aid = activity['id']
        sid = bridge.ensure_session(aid)
        unrelated = app.store.ensure_activity('A3', '2026-10-20')
        other_sid = bridge.ensure_session(unrelated['id'])
        with app.store.db(True) as db:
            for a, s in [(aid, sid), (unrelated['id'], other_sid)]:
                db.execute("UPDATE activities SET session_id=?,session_state='ready' WHERE id=?", (s, a))
        erased = app.store.submit(aid, 'native-cleanup-erased', ERASE)
        retained = app.store.submit(aid, 'native-cleanup-retained', KEEP)
        app.store.submit(unrelated['id'], 'native-cleanup-unrelated', OTHER)
        bridge.submit(sid, 'native-cleanup-erased', ERASE)
        finish(bridge, sid, 'native-cleanup-erased')
        bridge.submit(sid, 'native-cleanup-retained', KEEP)
        wait_for(lambda: len([e for e in bridge.events(sid) if e['type'] == 'turn/end']) >= 2)
        bridge.submit(other_sid, 'native-cleanup-unrelated', OTHER)
        finish(bridge, other_sid, 'native-cleanup-unrelated')
        try:
            purge_sessions(app.data / 'dsh', [sid])
        except NativeCleanupError as exc:
            assert '仍在写入' in str(exc)
        else:
            raise AssertionError('active writer was not rejected')
        assert ERASE in json.dumps(bridge.events(sid))
        record('actual_live_host_refuses_cleanup', {'native_original_still_intact': True})
        host.stop()
        before = hashes_for_sid(app.data / 'dsh', other_sid)
        assert len(before) >= 3
        old_bundle = export_bundle(app)
        app.store.delete_message(erased['receipt'], 'fixture explicit original deletion')
        ledger = recovery_ledger(app)
        cleanup = purge_sessions(app.data / 'dsh', [sid])
        assert cleanup['sessions'][sid]['generations_removed'] == 1
        assert hashes_for_sid(app.data / 'dsh', other_sid) == before
        with app.store.db() as db:
            kept = db.execute('SELECT body,deleted FROM messages WHERE id=?', (retained['receipt'],)).fetchone()
        assert kept['body'] == KEEP and kept['deleted'] == 0
        record('whole_target_namespace_removed_other_originals_preserved', {'cleanup': cleanup, 'other_session_files_byte_identical': len(before), 'retained_business_original_unchanged': True})
        bridge = host.start()
        assert bridge.projections(sid) is None and not bridge.events(sid)
        assert OTHER in json.dumps(bridge.events(other_sid))
        new_sid = bridge.ensure_session(aid + ':cleanup-generation:1')
        assert new_sid != sid
        bridge.submit(new_sid, 'native-cleanup-retained-replay', kept['body'])
        finish(bridge, new_sid, 'native-cleanup-retained-replay')
        clean = json.dumps(bridge.events(new_sid))
        assert KEEP in clean and ERASE not in clean
        record('restart_absent_and_new_generation_uses_surviving_original_only', {'retired_session_id': sid, 'new_session_id': new_sid, 'same_business_activity': aid, 'retired_absent': True})
        host.stop()

        # The recovery layer may itself apply the new ledger; the final native
        # adapter invocation remains idempotent for a never-started fixture copy.
        restored_dir = root / 'restored-old'
        restored_report = restore_bundle(old_bundle, restored_dir, latest_ledger=ledger)
        restored = Application(restored_dir)
        assert restored_report['isolated'] and restored.store.meta('isolated') == 'true'
        restored_cleanup = purge_sessions(restored_dir / 'dsh', [sid])
        restored_host = Host(restored_dir / 'dsh', patch_path, root / 'restored.log')
        hosts.append(restored_host)
        restored_bridge = restored_host.start()
        assert restored_bridge.projections(sid) is None
        assert OTHER in json.dumps(restored_bridge.events(other_sid))
        with restored.store.db() as db:
            row = db.execute('SELECT body,deleted FROM messages WHERE id=?', (erased['receipt'],)).fetchone()
        assert row['body'] == '' and row['deleted'] == 1
        record('old_backup_latest_ledger_then_offline_cleanup_no_resurrection', {'isolated': True, 'cleanup': restored_cleanup, 'retired_native_absent_after_start': True, 'unrelated_native_history_readable': True, 'old_encrypted_backup_bytes_not_erased': True})
        restored_host.stop()

        # Separate actual persistent FTS fixture, kept inside its own DSH home.
        indexed_home = root / 'indexed-home'
        durable_patch = [*patch, {'id': 'session-query-sqlite', 'config': {'path': str(indexed_home / 'cache/search.sqlite'), 'openAt': 'first-search'}}]
        durable_path = root / 'indexed.patch.json'
        durable_path.write_text(json.dumps(durable_patch))
        indexed_host = Host(indexed_home, durable_path, root / 'indexed.log')
        hosts.append(indexed_host)
        indexed_bridge = indexed_host.start()
        indexed_sid = indexed_bridge.ensure_session('fixture-index-deletion')
        index_other = indexed_bridge.ensure_session('fixture-index-keep')
        indexed_bridge.submit(indexed_sid, 'index-erase', ERASE)
        finish(indexed_bridge, indexed_sid, 'index-erase')
        indexed_bridge.submit(index_other, 'index-keep', OTHER)
        finish(indexed_bridge, index_other, 'index-keep')
        indexed_host.stop()
        indexed_bridge = indexed_host.start()
        found = indexed_bridge.rpc('session/search', {'request': {'query': ERASE}})
        assert any(r['sessionId'] == indexed_sid for r in found['items'])
        indexed_host.stop()
        index_cleanup = purge_sessions(indexed_home, [indexed_sid])
        with sqlite3.connect(indexed_home / 'cache/search.sqlite') as db:
            assert db.execute('SELECT count(*) FROM persisted_docs WHERE session_id=?', (indexed_sid,)).fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM persisted_docs WHERE session_id=?', (index_other,)).fetchone()[0] > 0
        assert ERASE.encode() not in (indexed_home / 'cache/search.sqlite').read_bytes()
        indexed_bridge = indexed_host.start()
        assert not indexed_bridge.rpc('session/search', {'request': {'query': ERASE}})['items']
        assert any(r['sessionId'] == index_other for r in indexed_bridge.rpc('session/search', {'request': {'query': OTHER}})['items'])
        record('actual_durable_FTS_purge_and_restart_preserve_other_search', {'cleanup': index_cleanup, 'removed_text_not_in_active_sqlite_bytes': True, 'retired_search_empty_after_restart': True, 'other_search_remains': True})
        result['outcome'] = 'PASS finite logical cleanup; explicit namespace ownership and recovery tombstone coverage remain caller requirements'
    except Exception as exc:
        result['error'] = repr(exc)
        raise
    finally:
        for host in hosts:
            host.stop()
        fixture.shutdown()
        result['fixture_model_calls'] = len(Fixture.requests)
        REPORT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
