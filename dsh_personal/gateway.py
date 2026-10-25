"""Single model-call budget boundary, including native DSH tool continuation calls."""
import http.client, json, socket, ssl, threading, time, urllib.request, urllib.error, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .core import dumps, BudgetRequestTooLarge

# Bulk personal-history refinement shares the background budget but may not
# consume its last 30k tokens. Ordinary routines retain the existing limits.
HISTORIAN_DAILY_HEADROOM = 30000

# Provider fields are deliberately enumerated: usage can contain arbitrary nested
# content. Retain only token counters, never reasoning text or request content.
TOKEN_FIELDS = frozenset({'prompt_tokens','completion_tokens','total_tokens',
    'input_tokens','output_tokens','cached_tokens','reasoning_tokens',
    'prompt_cache_hit_tokens','prompt_cache_miss_tokens'})
DETAIL_FIELDS = {
    'prompt_tokens_details': frozenset({'cached_tokens','audio_tokens'}),
    'completion_tokens_details': frozenset({'reasoning_tokens','audio_tokens',
        'accepted_prediction_tokens','rejected_prediction_tokens'}),
    'input_tokens_details': frozenset({'cached_tokens','audio_tokens'}),
    'output_tokens_details': frozenset({'reasoning_tokens','audio_tokens'}),
}

def token_count(value):
    # SQLite INTEGER is signed 64-bit. bool is not a token count.
    return type(value) is int and 0 <= value <= 2**63-1

def is_deepseek_official(base):
    url=urllib.parse.urlsplit(base)
    return (url.scheme=='https' and url.hostname=='api.deepseek.com'
        and url.port in (None,443) and not url.username and not url.password
        and not url.query and not url.fragment and url.path.rstrip('/') in ('','/v1','/beta'))

class RequestDeadline:
    """One deadline for connected HTTP headers and body, including slow drips.

    Register the connection socket before HTTP writes, instead of depending on
    urllib response.fp internals. OS DNS resolution cannot be interrupted here;
    if it returns after expiry, connect() refuses to send the expired request.
    """
    def __init__(self,seconds):
        self.until=time.monotonic()+seconds;self.sockets=[]
        self.lock=threading.Lock();self.closed=False
        self.timer=threading.Timer(seconds,self.expire);self.timer.daemon=True
    def remaining(self):
        left=self.until-time.monotonic()
        if left<=0:raise TimeoutError('模型请求超过整体期限')
        return left
    @staticmethod
    def shutdown(sock):
        try:sock.shutdown(socket.SHUT_RDWR)
        except OSError:pass
    def register(self,sock):
        with self.lock:
            try:sock.settimeout(self.remaining())
            except (TimeoutError,OSError):
                self.shutdown(sock);sock.close();raise
            self.sockets.append(sock)
    def connect(self,address,timeout=None,source_address=None):
        # HTTPConnection's replaceable connection factory. Each address attempt
        # shares the deadline; getaddrinfo itself is the documented OS boundary.
        last_error=None
        for family,kind,proto,_,remote in socket.getaddrinfo(*address,0,socket.SOCK_STREAM):
            self.remaining();sock=socket.socket(family,kind,proto)
            try:
                sock.settimeout(self.remaining())
                if source_address:sock.bind(source_address)
                sock.connect(remote);self.register(sock);return sock
            except OSError as error:
                last_error=error;sock.close()
        self.remaining()
        if last_error:raise last_error
        raise OSError('模型地址未返回网络端点')
    def before_read(self):
        with self.lock:
            left=self.remaining()
            for sock in self.sockets:
                # Completed redirects may leave a closed socket in the set.
                if sock.fileno()!=-1:sock.settimeout(left)
    def expire(self):
        with self.lock:
            if not self.closed:
                for sock in self.sockets:self.shutdown(sock)
    def __enter__(self):self.timer.start();return self
    def __exit__(self,*args):
        with self.lock:
            self.closed=True;self.timer.cancel()
            for sock in self.sockets:self.shutdown(sock)
    def open(self,request):
        deadline=self
        class HTTPConnection(http.client.HTTPConnection):
            def __init__(self,*args,**kwargs):
                super().__init__(*args,**kwargs);self._create_connection=deadline.connect
        class HTTPSConnection(http.client.HTTPSConnection):
            def __init__(self,*args,**kwargs):
                super().__init__(*args,**kwargs);self._create_connection=deadline.connect
            def connect(self):
                http.client.HTTPConnection.connect(self)
                # Register SSL before its handshake so the same timer also
                # interrupts a slow TLS peer. Mirrors CPython 3.12's SNI choice.
                self.sock=self._context.wrap_socket(self.sock,
                    server_hostname=self._tunnel_host or self.host,do_handshake_on_connect=False)
                deadline.register(self.sock);self.sock.do_handshake();deadline.remaining()
        class HTTPHandler(urllib.request.HTTPHandler):
            def http_open(self,req):return self.do_open(HTTPConnection,req)
        class HTTPSHandler(urllib.request.HTTPSHandler):
            def https_open(self,req):
                return self.do_open(HTTPSConnection,req,context=ssl.create_default_context())
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,req,fp,code,msg,headers,newurl):
                # Model requests carry credentials and private input. Correct the
                # configured API endpoint instead of forwarding either elsewhere.
                raise urllib.error.HTTPError(req.full_url,code,'模型接口不允许重定向',headers,fp)
        return urllib.request.build_opener(HTTPHandler(),HTTPSHandler(),NoRedirect()).open(request,timeout=self.remaining())

class UsageReceipt:
    def __init__(self):
        self.values={};self.total=None;self.state='not_received'
    def accept(self,parsed):
        if not isinstance(parsed,dict) or parsed.get('usage') is None:return
        raw=parsed['usage']
        if not isinstance(raw,dict):
            self.total=None;self.state='invalid';return
        for key in TOKEN_FIELDS:
            if token_count(raw.get(key)):self.values[key]=raw[key]
        for key,allowed in DETAIL_FIELDS.items():
            if isinstance(raw.get(key),dict):
                values={k:v for k,v in raw[key].items() if k in allowed and token_count(v)}
                if values:self.values.setdefault(key,{}).update(values)
        if 'total_tokens' in raw:
            self.total=raw['total_tokens'] if token_count(raw['total_tokens']) else None
            self.state='valid' if self.total is not None else 'invalid'
            if self.total is None:self.values.pop('total_tokens',None)
        elif self.state=='not_received':self.state='missing'
    def line(self,line):
        try:self.accept(json.loads(line[5:].lstrip() if line.startswith(b'data:') else line))
        except (ValueError,TypeError):pass
    def complete(self):
        if self.state=='not_received':self.state='missing'

class Gateway:
    def __init__(self,store,config_path,key,*,request_timeout=300):
        self.store=store;self.config_path=Path(config_path);self.key=key
        self.request_timeout=request_timeout
        self.slots=threading.BoundedSemaphore(2)
    def config(self):
        return json.loads(self.config_path.read_text()) if self.config_path.exists() else {}
    def begin_receipt(self,rid,model,field,limit,stream):
        with self.store.db(True) as c:
            c.execute('INSERT INTO usage_details VALUES(?,?,?,?,?,?,NULL,NULL,?,?,?)',
                (rid,model,field,limit,int(stream),self.store.stamp(),'{}','started','not_received'))
    def finish_receipt(self,rid,usage,completed):
        # Persist accounting and its evidence atomically. A partial or disconnected
        # upstream response keeps the reservation even if it emitted interim usage.
        actual=usage.total if completed else None
        with self.store.db(True) as c:
            c.execute('UPDATE usage SET actual=?,state=?,receipt=? WHERE id=?',
                (actual,'settled' if actual is not None else 'unknown',
                 'upstream_response' if completed else 'unconfirmed_upstream',rid))
            c.execute('UPDATE usage_details SET finished_at=?,reported_total=?,usage_json=?,response_state=?,usage_state=? WHERE reservation_id=?',
                (self.store.stamp(),usage.total,dumps(usage.values),
                 'completed' if completed else 'unconfirmed',usage.state,rid))
    def serve(self,port=18782):
        outer=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                from .operations import maintenance_lock
                with maintenance_lock(outer.store.path.parent):return self.process_post()
            def process_post(self):
                if self.headers.get('Authorization')!='Bearer '+outer.key:return self.error(401,'内部认证失败')
                if self.path!='/v1/chat/completions':return self.error(404,'未知模型接口')
                rid=None;usage=UsageReceipt();response_started=False
                try:
                    n=int(self.headers.get('Content-Length','0'))
                    if not 0<n<=2_000_000:return self.error(413,'模型请求超限')
                    body=json.loads(self.rfile.read(n));conf=outer.config()
                    if not isinstance(body,dict):return self.error(400,'模型请求须为对象')
                    provider=conf.get('model',{})
                    if not provider.get('base_url') or not provider.get('name') or not provider.get('api_key_file'):
                        return self.error(503,'模型尚未配置；原文已保存，处理待恢复')
                    base=provider['base_url'].rstrip('/')
                    if not base.startswith('https://') and not base.startswith(('http://127.0.0.1:','http://localhost:')):
                        return self.error(400,'模型地址须使用 HTTPS 或本机回环')
                    key=Path(provider['api_key_file']).read_text().strip()
                    accounting_alias=body.get('model')
                    kind={'foreground-model':'foreground','delegated-model':'delegated'}.get(accounting_alias,'background')
                    field='max_completion_tokens' if 'max_completion_tokens' in body else 'max_tokens'
                    configured_max=provider.get('max_tokens',8192)
                    requested_max=body.get(field,8192)
                    if type(configured_max)!=int or configured_max<1:
                        return self.error(400,'配置 model.max_tokens 必须为正整数')
                    if type(requested_max)!=int or requested_max<1:
                        return self.error(400,'请求输出上限必须为正整数')
                    body['model']=provider['name'];maxout=min(requested_max,configured_max,8192)
                    if is_deepseek_official(base):
                        # The local accounting aliases hide provider detection from
                        # pi-ai. Use the documented DeepSeek field explicitly.
                        field='max_tokens';body.pop('max_completion_tokens',None);body['max_tokens']=maxout
                        thinking=provider.get('thinking','disabled')
                        if thinking not in ('enabled','disabled'):
                            return self.error(400,'配置 model.thinking 须为 enabled 或 disabled')
                        body['thinking']={'type':thinking};body.pop('store',None)
                        if thinking=='disabled':body.pop('reasoning_effort',None)
                        elif body.get('reasoning_effort')=='none':body.pop('reasoning_effort')
                        if thinking=='enabled' and body.get('tools') is not None and any(
                            m.get('role')=='assistant' and not isinstance(m.get('reasoning_content'),str)
                            for m in body.get('messages',[])):
                            return self.error(400,'思考工具上下文缺少完整 reasoning 回传；请使用非思考模式或新的完整会话')
                    elif 'max_completion_tokens' in body:
                        body.pop('max_tokens',None);body['max_completion_tokens']=maxout
                    else:body['max_tokens']=maxout
                    # Byte upper bound on input tokenization, including format overhead.
                    estimate=len(dumps(body.get('messages',[])).encode())+len(dumps(body.get('tools',[])).encode())+2048+maxout
                    if body.get('stream'):body['stream_options']={'include_usage':True}
                    if not outer.slots.acquire(timeout=1):return self.error(429,'计算槽已满，请排队重试')
                    try:
                        rid=outer.store.reserve(estimate,kind,minimum_daily_remaining=(
                            HISTORIAN_DAILY_HEADROOM if accounting_alias=='historian-model' else 0))
                        outer.begin_receipt(rid,body['model'],field,maxout,bool(body.get('stream')))
                        req=urllib.request.Request(base+'/chat/completions',dumps(body).encode(),{'Content-Type':'application/json','Authorization':'Bearer '+key})
                        with RequestDeadline(outer.request_timeout) as deadline, deadline.open(req) as response:
                            deadline.remaining()
                            self.send_response(200);self.send_header('Content-Type',response.headers.get('Content-Type','application/json'));self.send_header('Cache-Control','no-store');self.end_headers()
                            response_started=True;data=[]
                            while True:
                                deadline.before_read();line=response.readline();deadline.remaining()
                                if not line:break
                                usage.line(line)
                                if not body.get('stream'):data.append(line)
                                try:self.wfile.write(line);self.wfile.flush()
                                except (BrokenPipeError,ConnectionResetError):pass # keep accounting upstream completion
                            if not body.get('stream'):
                                try:usage.accept(json.loads(b''.join(data)))
                                except ValueError:pass
                            usage.complete()
                            outer.finish_receipt(rid,usage,True)
                            outer.store.set_meta('model_last_success',outer.store.stamp())
                            if usage.state=='valid':
                                for name in ('model_error','model_error_status','model_error_category'):outer.store.set_meta(name,'')
                    finally:outer.slots.release()
                except BudgetRequestTooLarge as exc:self.error(413,str(exc))
                except RuntimeError as exc:self.error(429,str(exc))
                except urllib.error.HTTPError as exc:
                    if rid:outer.finish_receipt(rid,usage,False)
                    category,label={401:('auth','鉴权失败'),403:('auth','鉴权或访问权限不足'),
                        402:('quota','余额或额度不足'),400:('parameters','请求参数不兼容'),
                        404:('model_or_endpoint','模型或接口不存在'),429:('rate','请求受限')}.get(exc.code,('upstream','上游请求失败'))
                    message=f'模型服务{label}（HTTP {exc.code}）；预留用量保留待核对'
                    outer.store.set_meta('model_error',message)
                    outer.store.set_meta('model_error_status',exc.code)
                    outer.store.set_meta('model_error_category',category)
                    if response_started:self.close_connection=True
                    else:self.error(502,message,upstream_status=exc.code,category=category)
                    exc.close()
                except Exception:
                    if rid:outer.finish_receipt(rid,usage,False)
                    outer.store.set_meta('model_error','模型服务请求未完成；预留用量保留待核对')
                    if response_started:self.close_connection=True
                    else:self.error(502,'模型服务请求失败，原文仍已保存')
            def error(self,status,message,**details):
                try:
                    data=dumps({'error':{'message':message,'type':'dsh_personal_gateway',**details}}).encode()
                    self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
                except (BrokenPipeError,ConnectionResetError):pass
        server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        return server
