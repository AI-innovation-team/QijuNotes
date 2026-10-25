import json
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from dsh_personal.core import Store, dumps
from dsh_personal.gateway import Gateway, RequestDeadline, is_deepseek_official


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.store=Store(self.root/'personal.sqlite')
        self.calls=[];self.reply={'usage':{'total_tokens':10}};self.sse=False
        self.header_delay=0;self.drip_headers=False;self.body_parts=None
        self.redirect_to=None
        self.error_status=None
        outer=self
        class Provider(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                outer.calls.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                try:self.respond()
                except OSError:pass
            def respond(self):
                if outer.error_status:
                    content=b'{"error":{"message":"PRIVATE_PROVIDER_ERROR_BODY"}}'
                    self.send_response(outer.error_status);self.send_header('Content-Length',str(len(content)))
                    self.end_headers();self.wfile.write(content);return
                if outer.redirect_to:
                    self.send_response(302);self.send_header('Location',outer.redirect_to)
                    self.send_header('Content-Length','0');self.end_headers();return
                time.sleep(outer.header_delay)
                if outer.drip_headers:
                    self.wfile.write(b'HTTP/1.0 200 OK\r\nX-Slow: ');self.wfile.flush()
                    for _ in range(40):
                        time.sleep(.03);self.wfile.write(b'x');self.wfile.flush()
                    self.wfile.write(b'\r\n\r\n');return
                if outer.sse:
                    content=b''.join(b'data: '+dumps(part).encode()+b'\n\n' for part in outer.reply)+b'data: [DONE]\n\n'
                else:content=json.dumps(outer.reply,indent=2).encode()
                self.send_response(200)
                self.send_header('Content-Type','text/event-stream' if outer.sse else 'application/json')
                if outer.body_parts is None:self.send_header('Content-Length',str(len(content)))
                self.end_headers()
                if outer.body_parts is not None:
                    for delay,chunk in outer.body_parts:
                        time.sleep(delay);self.wfile.write(chunk);self.wfile.flush()
                else:self.wfile.write(content)
        self.provider=ThreadingHTTPServer(('127.0.0.1',0),Provider)
        threading.Thread(target=self.provider.serve_forever,daemon=True).start()
        (self.root/'provider.key').write_text('fixture-provider-key-never-log')
        self.conf={'model':{'base_url':f'http://127.0.0.1:{self.provider.server_port}',
            'name':'fixture-model','api_key_file':str(self.root/'provider.key')}}
        self.config_path=self.root/'config.json';self.save_config()
        self.gateway_instance=Gateway(self.store,self.config_path,'fixture-local-auth')
        self.gateway=self.gateway_instance.serve(0)
    def tearDown(self):
        for server in (self.gateway,self.provider):
            server.shutdown();server.server_close()
        self.tmp.cleanup()
    def save_config(self):self.config_path.write_text(dumps(self.conf))
    def request(self,**extra):
        body={'model':'configured-model','messages':[{'role':'user','content':'PRIVATE_PROMPT_MARKER'}],**extra}
        request=urllib.request.Request(f'http://127.0.0.1:{self.gateway.server_port}/v1/chat/completions',
            dumps(body).encode(),{'Content-Type':'application/json','Authorization':'Bearer fixture-local-auth'})
        try:
            with urllib.request.urlopen(request,timeout=5) as response:return response.status,response.read()
        except urllib.error.HTTPError as error:return error.code,error.read()
    def rows(self):
        with self.store.db() as db:
            usage=[dict(r) for r in db.execute('SELECT * FROM usage ORDER BY rowid')]
            details=[dict(r) for r in db.execute('SELECT * FROM usage_details ORDER BY rowid')]
        return usage,details

    def test_oversize_historian_request_returns_413_without_upstream_call(self):
        status, body = self.request(model='historian-model', max_tokens=32,
                                    messages=[{'role':'user','content':'x'*31000}])
        self.assertEqual(status, 413)
        self.assertIn('完整史料', json.loads(body)['error']['message'])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.rows(), ([], []))

    def test_historian_alias_keeps_background_kind_and_preserves_routine_budget(self):
        used=self.store.reserve(29000);self.store.settle(used,29000)
        status,body=self.request(model='historian-model',max_tokens=32)
        self.assertEqual(status,429)
        self.assertIn('每日活动',body.decode())
        self.assertEqual(self.calls,[])
        usage,details=self.rows()
        self.assertEqual(len(usage),1)
        self.assertEqual(details,[])
        # Ordinary background work may consume the protected balance.
        self.assertEqual(self.request(model='configured-model',max_tokens=32)[0],200)
        self.store.settle(used,1000)
        self.assertEqual(self.request(model='historian-model',max_tokens=32)[0],200)
        usage,details=self.rows()
        self.assertEqual({row['kind'] for row in usage},{'background'})
        self.assertEqual(self.calls[-1]['model'],'fixture-model')
        self.assertNotIn('minimum_daily_remaining',self.calls[-1])
        self.assertEqual(len(details),2)

    def test_explicit_unlimited_background_still_uses_gateway_and_receipts(self):
        used = self.store.reserve(60000)
        self.store.settle(used, 60000)
        self.store.set_meta('monthly_tokens', 60000)
        self.assertEqual(self.request(max_tokens=32)[0], 429)
        self.store.set_meta('background_limit_enforced', 'false')
        for alias in ('configured-model', 'historian-model'):
            self.assertEqual(self.request(model=alias, max_tokens=32)[0], 200)
        usage, details = self.rows()
        self.assertEqual(len(usage), 3)
        self.assertEqual(len(details), 2)
        self.assertEqual({r['kind'] for r in usage}, {'background'})
        self.assertEqual({r['reported_total'] for r in details}, {10})
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.store.budget()['usage'][0]['settled'], 60020)

    def official_request(self,**body):
        # The gateway exercises official-host normalization but every byte goes
        # only to this test's local fixture; no real credential or API is used.
        self.conf['model']['base_url']='https://api.deepseek.com';self.save_config()
        original=RequestDeadline.open
        def local_open(deadline,request):
            redirected=urllib.request.Request(f'http://127.0.0.1:{self.provider.server_port}/chat/completions',
                request.data,dict(request.header_items()))
            return original(deadline,redirected)
        with mock.patch.object(RequestDeadline,'open',local_open):return self.request(**body)

    def test_deepseek_official_limit_and_explicit_non_thinking_default(self):
        self.conf['model']['max_tokens']=400
        self.assertEqual(self.official_request(max_completion_tokens=900,reasoning_effort='high',store=False)[0],200)
        call=self.calls[0];self.assertEqual(call['max_tokens'],400)
        self.assertEqual(call['thinking'],{'type':'disabled'})
        for field in ('max_completion_tokens','reasoning_effort','store'):self.assertNotIn(field,call)
        usage,details=self.rows()
        self.assertEqual((details[0]['output_limit_field'],details[0]['output_limit']),('max_tokens',400))
        self.assertGreaterEqual(usage[0]['reserved'],400)

    def test_deepseek_thinking_tool_history_must_preserve_reasoning(self):
        self.conf['model']['thinking']='enabled'
        messages=[{'role':'user','content':'fixture'},
            {'role':'assistant','content':'fixture reply','reasoning_content':'fixture reasoning'},
            {'role':'user','content':'fixture followup'}]
        tools=[{'type':'function','function':{'name':'fixture','parameters':{'type':'object','properties':{}}}}]
        self.assertEqual(self.official_request(messages=messages,tools=tools,max_tokens=300)[0],200)
        self.assertEqual(self.calls[-1]['messages'][1]['reasoning_content'],'fixture reasoning')
        self.assertEqual(self.calls[-1]['thinking'],{'type':'enabled'})
        del messages[1]['reasoning_content']
        self.assertEqual(self.official_request(messages=messages,tools=tools)[0],400)
        self.assertEqual(self.official_request(messages=messages,tools=[])[0],400)
        self.assertEqual(len(self.calls),1)
        self.conf['model']['thinking']='disabled'
        self.assertEqual(self.official_request(messages=messages,tools=tools,reasoning_effort='max')[0],200)
        self.assertNotIn('reasoning_effort',self.calls[-1])

    def test_deepseek_detection_is_exact_and_generic_route_unchanged(self):
        for base in ('https://api.deepseek.com','https://api.deepseek.com/v1','https://api.deepseek.com:443'):
            self.assertTrue(is_deepseek_official(base))
        for base in ('https://api.deepseek.com.evil.test','https://example.test/deepseek.com',
                     'http://api.deepseek.com','https://api.deepseek.com/anthropic',
                     'https://api.deepseek.com?x=1','https://api.deepseek.com:444'):
            self.assertFalse(is_deepseek_official(base))
        self.assertEqual(self.request(max_completion_tokens=512,reasoning_effort='high')[0],200)
        self.assertEqual(self.calls[0]['max_completion_tokens'],512)
        self.assertEqual(self.calls[0]['reasoning_effort'],'high');self.assertNotIn('thinking',self.calls[0])

    def test_invalid_deepseek_thinking_configuration_refuses_before_call(self):
        for invalid in ('maybe',True,{},None):
            self.conf['model']['thinking']=invalid
            self.assertEqual(self.official_request()[0],400)
        self.assertEqual(self.calls,[]);self.assertEqual(self.rows(),([],[]))

    def test_provider_http_failures_have_safe_categories_and_keep_reservation(self):
        cases={401:'auth',403:'auth',402:'quota',400:'parameters',404:'model_or_endpoint',429:'rate',500:'upstream'}
        for code,category in cases.items():
            self.error_status=code;status,raw=self.request(max_tokens=32)
            self.assertEqual(status,502);error=json.loads(raw)['error']
            self.assertEqual((error['upstream_status'],error['category']),(code,category))
            self.assertNotIn(b'PRIVATE_PROVIDER_ERROR_BODY',raw)
            self.assertNotIn('PRIVATE_PROVIDER_ERROR_BODY',self.store.meta('model_error'))
            self.assertEqual(self.store.meta('model_error_status'),str(code))
            self.assertEqual(self.store.meta('model_error_category'),category)
        usage,details=self.rows()
        self.assertTrue(all(r['actual'] is None and r['state']=='unknown' for r in usage))
        self.assertTrue(all(r['response_state']=='unconfirmed' for r in details))

    def test_valid_success_clears_stale_provider_error(self):
        self.error_status=401;self.assertEqual(self.request(max_tokens=32)[0],502)
        self.error_status=None;self.reply={'choices':[]}
        self.assertEqual(self.request(max_tokens=32)[0],200)
        self.assertEqual(self.store.meta('model_error_category'),'auth')
        self.reply={'usage':{'prompt_tokens':5,'completion_tokens':5,'total_tokens':10}}
        self.assertEqual(self.request(max_tokens=32)[0],200)
        for key in ('model_error','model_error_status','model_error_category'):
            self.assertEqual(self.store.meta(key),'')

    def test_nonstream_exact_total_and_private_fields_excluded(self):
        self.reply={'choices':[{'message':{'content':'PRIVATE_RESPONSE_MARKER'}}],
            'usage':{'prompt_tokens':20,'completion_tokens':15,'total_tokens':35,
                'completion_tokens_details':{'reasoning_tokens':7,'audio_tokens':2,
                    'text':'PRIVATE_REASONING_MARKER','unknown_tokens':999},
                'prompt_tokens_details':{'cached_tokens':8},'unknown_tokens':100,
                'reasoning':'PRIVATE_REASONING_MARKER'}}
        self.assertEqual(self.request(model='foreground-model',max_tokens=512)[0],200)
        usage,details=self.rows();u=usage[0];d=details[0]
        self.assertEqual((u['kind'],u['actual'],u['state']),('foreground',35,'settled'))
        self.assertEqual(d['reservation_id'],u['id'])
        self.assertEqual((d['model'],d['output_limit_field'],d['output_limit'],d['stream']),('fixture-model','max_tokens',512,0))
        self.assertEqual((d['reported_total'],d['response_state'],d['usage_state']),(35,'completed','valid'))
        self.assertGreaterEqual(d['finished_at'],d['started_at'])
        self.assertEqual(json.loads(d['usage_json']),{'prompt_tokens':20,'completion_tokens':15,'total_tokens':35,
            'completion_tokens_details':{'reasoning_tokens':7,'audio_tokens':2},'prompt_tokens_details':{'cached_tokens':8}})
        self.assertNotIn('PRIVATE_',dumps(details));self.assertNotIn('fixture-provider-key',dumps(details))
        reloaded=Store(self.store.path)
        with reloaded.db() as db:self.assertEqual(db.execute('SELECT reported_total FROM usage_details').fetchone()[0],35)

    def test_stream_usage_and_reasoning_are_not_double_counted(self):
        self.sse=True
        self.reply=[{'choices':[{'delta':{'content':'PRIVATE_STREAM_CONTENT'}}],'usage':None},
            {'usage':{'prompt_tokens':22,'completion_tokens':19,'total_tokens':41,
                'completion_tokens_details':{'reasoning_tokens':11},'prompt_cache_hit_tokens':4}}]
        status,response=self.request(model='delegated-model',stream=True,max_completion_tokens=400)
        self.assertEqual(status,200);self.assertIn(b'[DONE]',response)
        usage,details=self.rows()
        self.assertEqual((usage[0]['kind'],usage[0]['actual']),('delegated',41))
        self.assertEqual(details[0]['stream'],1)
        self.assertEqual(json.loads(details[0]['usage_json'])['completion_tokens_details']['reasoning_tokens'],11)
        self.assertEqual(self.calls[0]['stream_options'],{'include_usage':True})
        self.assertNotIn('max_tokens',self.calls[0])
        self.assertNotIn('PRIVATE_',dumps(details))

    def test_missing_usage_keeps_reservation_for_stream_and_nonstream(self):
        for stream in (False,True):
            self.sse=stream;reply={'choices':[{'message':{'content':'fixture'}}]}
            self.reply=[reply] if stream else reply
            self.assertEqual(self.request(stream=stream)[0],200)
        usage,details=self.rows()
        self.assertTrue(all(r['actual'] is None and r['state']=='unknown' and r['reserved']>8192 for r in usage))
        self.assertTrue(all(r['reported_total'] is None and r['usage_state']=='missing' and r['usage_json']=='{}' for r in details))

    def test_invalid_reported_total_never_reduces_reservation(self):
        for invalid in (-1,True,'35',1.5,2**70):
            self.reply={'usage':{'total_tokens':invalid,'completion_tokens':True,
                'prompt_tokens':5,'completion_tokens_details':{'reasoning_tokens':-1}}}
            self.assertEqual(self.request(max_tokens=64)[0],200)
        usage,details=self.rows()
        self.assertTrue(all(r['state']=='unknown' and r['actual'] is None for r in usage))
        self.assertTrue(all(r['reported_total'] is None and r['usage_state']=='invalid' for r in details))
        self.assertTrue(all(json.loads(r['usage_json'])=={'prompt_tokens':5} for r in details))

    def test_configured_and_request_output_limits_apply_to_forwarding_and_reserve(self):
        cases=[(300,{'max_completion_tokens':350,'max_tokens':999},'max_completion_tokens',300),
               (100000,{'max_tokens':100000},'max_tokens',8192),
               (500,{'max_tokens':100},'max_tokens',100)]
        for configured,request,field,expected in cases:
            self.conf['model']['max_tokens']=configured;self.save_config()
            self.assertEqual(self.request(**request)[0],200)
            call=self.calls[-1]
            self.assertEqual(call[field],expected)
            self.assertNotIn('max_tokens' if field=='max_completion_tokens' else 'max_completion_tokens',call)
            usage,details=self.rows()
            self.assertEqual(details[-1]['output_limit'],expected)
            self.assertEqual(usage[-1]['reserved'],len(dumps(call['messages']).encode())+len(dumps([]).encode())+2048+expected)

    def test_invalid_output_limits_refuse_before_reserve_or_provider(self):
        for invalid in (0,-2,True,'300',1.5,None):
            self.conf['model']['max_tokens']=invalid;self.save_config()
            self.assertEqual(self.request()[0],400)
        self.conf['model']['max_tokens']=100;self.save_config()
        for field in ('max_tokens','max_completion_tokens'):
            for invalid in (0,-2,True,'300',1.5,None):
                self.assertEqual(self.request(**{field:invalid})[0],400)
        self.assertEqual(self.calls,[]);self.assertEqual(self.rows(),([],[]))

    def test_headers_and_stream_share_one_deadline(self):
        self.gateway_instance.request_timeout=.3;self.header_delay=.12;self.sse=True
        self.body_parts=[(.08,b'data: {"usage":{"total_tokens":7}}\n\n')]+[(.08,b'data: {}\n\n')]*10
        started=time.monotonic();status,_=self.request(stream=True);elapsed=time.monotonic()-started
        self.assertEqual(status,200);self.assertLess(elapsed,.4);self.assertGreater(elapsed,.25)
        usage,details=self.rows()
        self.assertEqual((usage[0]['state'],usage[0]['actual']),('unknown',None))
        # Interim usage survives for diagnosis but is not final settlement.
        self.assertEqual((details[0]['response_state'],details[0]['reported_total']),('unconfirmed',7))

    def test_slow_headers_and_no_newline_body_are_interrupted(self):
        self.gateway_instance.request_timeout=.2
        for phase in ('headers','body'):
            self.drip_headers=phase=='headers'
            self.body_parts=[(.03,b'x')]*40 if phase=='body' else None
            started=time.monotonic();status,_=self.request(stream=True);elapsed=time.monotonic()-started
            self.assertEqual(status,502 if phase=='headers' else 200)
            self.assertLess(elapsed,.4)
            usage,details=self.rows()
            self.assertEqual(usage[-1]['state'],'unknown')
            self.assertEqual(details[-1]['response_state'],'unconfirmed')

    def test_expired_dns_resolution_does_not_send_provider_request(self):
        self.gateway_instance.request_timeout=.1
        original=socket.getaddrinfo
        def delayed(host,port,*args,**kwargs):
            if port==self.provider.server_port:time.sleep(.25)
            return original(host,port,*args,**kwargs)
        with mock.patch('socket.getaddrinfo',side_effect=delayed):
            self.assertEqual(self.request()[0],502)
        self.assertEqual(self.calls,[])
        usage,details=self.rows();self.assertEqual(usage[0]['state'],'unknown')
        self.assertEqual(details[0]['response_state'],'unconfirmed')

    def test_authenticated_model_redirect_does_not_reach_target(self):
        received=[]
        class Target(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                received.append(dict(self.headers));self.send_response(200);self.end_headers()
            do_POST=do_GET
        target=ThreadingHTTPServer(('127.0.0.1',0),Target)
        threading.Thread(target=target.serve_forever,daemon=True).start()
        try:
            self.redirect_to=f'http://127.0.0.1:{target.server_port}/different-origin'
            self.assertEqual(self.request()[0],502)
            self.assertEqual(len(self.calls),1);self.assertEqual(received,[])
            usage,details=self.rows();self.assertEqual(usage[0]['state'],'unknown')
            self.assertEqual(details[0]['response_state'],'unconfirmed')
        finally:target.shutdown();target.server_close()

    @unittest.skipUnless(shutil.which('openssl'),'local TLS fixture requires openssl')
    def test_https_verified_certificate_and_deadline(self):
        cert=self.root/'fixture.crt';key=self.root/'fixture.key'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
            '-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1',
            '-keyout',str(key),'-out',str(cert)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        handler=self.provider.RequestHandlerClass
        self.provider.shutdown();self.provider.server_close()
        self.provider=ThreadingHTTPServer(('127.0.0.1',0),handler)
        tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(cert,key)
        self.provider.socket=tls.wrap_socket(self.provider.socket,server_side=True)
        threading.Thread(target=self.provider.serve_forever,daemon=True).start()
        self.conf['model']['base_url']=f'https://127.0.0.1:{self.provider.server_port}';self.save_config()
        trust=ssl.create_default_context(cafile=str(cert))
        with mock.patch('dsh_personal.gateway.ssl.create_default_context',return_value=trust):
            self.assertEqual(self.request()[0],200)
            self.gateway_instance.request_timeout=.2;self.drip_headers=True
            started=time.monotonic();self.assertEqual(self.request()[0],502)
            self.assertLess(time.monotonic()-started,.4)
            self.drip_headers=False;self.body_parts=[(.03,b'x')]*40
            started=time.monotonic();self.assertEqual(self.request(stream=True)[0],200)
            self.assertLess(time.monotonic()-started,.4)
            self.body_parts=None;self.gateway_instance.request_timeout=.3
            original_connect=socket.socket.connect;original_handshake=ssl.SSLSocket.do_handshake
            def slow_connect(sock,address):
                result=original_connect(sock,address)
                if address[1]==self.provider.server_port:time.sleep(.15)
                return result
            def slow_handshake(sock,*args,**kwargs):
                if sock.server_side:time.sleep(.4)
                return original_handshake(sock,*args,**kwargs)
            # TCP establishment and TLS handshake must not receive separate full
            # timeout windows. The expired handshake sends no provider POST.
            called=len(self.calls)
            with mock.patch.object(socket.socket,'connect',slow_connect),mock.patch.object(ssl.SSLSocket,'do_handshake',slow_handshake):
                started=time.monotonic();self.assertEqual(self.request()[0],502)
                self.assertLess(time.monotonic()-started,.38)
            self.assertEqual(len(self.calls),called)
        usage,details=self.rows()
        self.assertEqual([r['state'] for r in usage],['settled','unknown','unknown','unknown'])
        self.assertEqual([r['response_state'] for r in details],['completed','unconfirmed','unconfirmed','unconfirmed'])


if __name__=='__main__':unittest.main()
