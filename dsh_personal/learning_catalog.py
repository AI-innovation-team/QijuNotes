"""Bounded public-history discovery, independent of daily lesson dispatch.

Only this module performs network I/O. Index queues, successful text snapshots,
and failed attempts live in existing SQLite meta rows and travel with backups.
Fetched text is source material, never executable instructions or a fact check.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import ipaddress
import json
import re
import time
import urllib.error
import urllib.request
import uuid
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit

from .core import dumps

PREFIX = 'learning_catalog:'
STATE_KEY = PREFIX + 'refresh'
DOCUMENT_PREFIX = PREFIX + 'document:'
INDEX_PREFIX = PREFIX + 'index:'
REFRESH_HOURS = 6
MAX_BYTES = 1024 * 1024
MAX_TEXT_CHARS = 80000
LESSON_TEXT_CHARS = 4800
REQUEST_TIMEOUT = 6
MAX_DOCUMENTS = 4
MAX_DISCOVERIES = 300
LEASE_SECONDS = 180
INDEXES = (
    {'id': 'shiji', 'url': 'https://zh.wikisource.org/wiki/%E5%8F%B2%E8%A8%98',
     'scope': '二十四史', 'title': '维基文库《史记》原文目录'},
    {'id': 'cpc-events', 'url': 'https://cpc.people.com.cn/GB/64162/64167/',
     'scope': '党史', 'title': '人民网中国共产党新闻网·重大事件'},
)


def _sha(value):
    return hashlib.sha256(value.encode('utf-8') if isinstance(value, str) else value).hexdigest()


def canonical_url(url):
    """A closed set of public indices/article paths, not a generic URL fetcher."""
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.query or parsed.fragment):
        raise ValueError('来源地址不在允许范围')
    path = unquote(parsed.path)
    host = (parsed.hostname or '').lower()
    if host == 'zh.wikisource.org' and re.fullmatch(r'/wiki/史記(?:/卷\d{3})?', path):
        if '/卷' in path and not 1 <= int(path[-3:]) <= 130:
            raise ValueError('史记卷号超出目录范围')
        return 'https://' + host + quote(path, safe='/')
    if host == 'cpc.people.com.cn' and (
            path == '/GB/64162/64167/'
            or re.fullmatch(r'/GB/(?:33837|64162/64167|64156/64157)/\d+\.html', path)):
        return 'https://' + host + path
    raise ValueError('来源地址不在允许范围')


def _wiki_request(url):
    page = unquote(urlsplit(url).path).removeprefix('/wiki/')
    return 'https://zh.wikisource.org/w/api.php?' + urlencode({
        'action': 'parse', 'page': page,
        'prop': 'text|revid|displaytitle|links' if page == '史記' else 'text|revid|displaytitle',
        'format': 'json', 'formatversion': '2'})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Even an allowed host cannot redirect this fetch to a private endpoint.
        raise ValueError('来源重定向已拒绝')


def public_proxy_url(value):
    """Only an explicitly configured, credential-free proxy on this host."""
    if value is None:
        return None
    if not isinstance(value, str) or not value or any(ch.isspace() for ch in value):
        raise ValueError('公开来源代理须为本机 HTTP(S) 地址，不含凭据')
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or '').lower()
        loopback = host == 'localhost' or ipaddress.ip_address(host).is_loopback
        if (not loopback or parsed.scheme not in ('http', 'https')
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in ('', '/')
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('公开来源代理仅允许无凭据的本机 HTTP(S) 地址') from None
    return value.rstrip('/')


def fetch_public(url, *, proxy_url=None):
    """Fetch one allowlisted public resource; no cookies, credentials or scripts."""
    url = canonical_url(url)
    proxy_url = public_proxy_url(proxy_url)
    request_url = _wiki_request(url) if urlsplit(url).hostname == 'zh.wikisource.org' else url
    request = urllib.request.Request(request_url, headers={
        'User-Agent': 'DSH-Personal-Learning/1.1 (bounded public-history reading)',
        'Accept': 'application/json,text/html', 'Accept-Encoding': 'identity'})
    started = time.monotonic()
    # This opener has no effect on model, notification, or other network calls.
    # Empty ProxyHandler also makes default/direct behavior independent of the
    # shell's proxy environment, which systemd may not inherit.
    proxy = urllib.request.ProxyHandler({'http': proxy_url, 'https': proxy_url} if proxy_url else {})
    with urllib.request.build_opener(proxy, _NoRedirect()).open(request, timeout=REQUEST_TIMEOUT) as response:
        content_type = response.headers.get_content_type()
        if content_type not in ('text/html', 'application/json', 'text/json'):
            raise ValueError('来源不是支持的文本页面')
        length = response.headers.get('Content-Length')
        if length and int(length) > MAX_BYTES:
            raise ValueError('来源页面超出大小上限')
        data = bytearray()
        while True:
            if time.monotonic() - started > REQUEST_TIMEOUT:
                raise TimeoutError('来源读取超时')
            block = response.read1(min(65536, MAX_BYTES + 1 - len(data)))
            if not block:
                break
            data.extend(block)
            if len(data) > MAX_BYTES:
                raise ValueError('来源页面超出大小上限')
        raw = bytes(data)
        charset = response.headers.get_content_charset()
    if not charset:
        found = re.search(br'charset\s*=\s*["\']?([a-zA-Z0-9_-]+)', raw[:4096], re.I)
        charset = found.group(1).decode('ascii') if found else 'utf-8'
    charset = 'gb18030' if charset.lower() in ('gb2312', 'gbk') else charset
    if charset.lower() not in ('utf-8', 'utf8', 'gb18030', 'big5'):
        raise ValueError('来源字符编码不受支持')
    return {'body': raw.decode(charset, errors='strict'), 'response_sha256': _sha(raw),
            'retrieval_url': request_url, 'bytes': len(raw)}


class _Page(HTMLParser):
    """Extract only an observed content root; never fall back to whole-page text."""
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}
    BLOCK = {'p', 'br', 'div', 'li', 'h1', 'h2', 'h3', 'tr'}
    SKIP_CLASSES = {'ws-header', 'header', 'navbox', 'noprint', 'reference', 'references',
                    'mw-editsection', 'licenseContainer', 'catlinks', 'wst-footer', 'sisicon'}

    def __init__(self, wiki=False, index=False):
        super().__init__(convert_charrefs=True)
        self.wiki, self.index = wiki, index
        self.stack, self.parts, self.links = [], [], []
        self.title_parts, self.anchor_parts = [], []
        self.anchor = None
        self.root_seen = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set(attrs.get('class', '').split())
        parent = self.stack[-1] if self.stack else {'active': False, 'skip': False}
        root = ('mw-parser-output' in classes) if self.wiki else attrs.get('id') == 'zoom'
        active = parent['active'] or root
        skip = parent['skip'] or tag in ('script', 'style', 'noscript', 'iframe', 'form', 'button')
        skip = skip or bool(classes & self.SKIP_CLASSES) or attrs.get('aria-hidden') == 'true'
        self.root_seen = self.root_seen or root
        if tag in self.BLOCK and active and not skip:
            self.parts.append('\n')
        if tag == 'a' and self.index and attrs.get('href'):
            self.anchor, self.anchor_parts = attrs['href'], []
        if tag not in self.VOID:
            self.stack.append({'tag': tag, 'active': active, 'skip': skip})

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == 'a' and self.anchor is not None:
            self.links.append((self.anchor, ''.join(self.anchor_parts).strip()))
            self.anchor = None
        positions = [i for i, frame in enumerate(self.stack) if frame['tag'] == tag]
        if positions:
            del self.stack[positions[-1]:]
        if tag in self.BLOCK:
            self.parts.append('\n')

    def handle_data(self, data):
        if self.anchor is not None:
            self.anchor_parts.append(data)
        if any(frame['tag'] == 'title' for frame in self.stack):
            self.title_parts.append(data)
        if self.stack and self.stack[-1]['active'] and not self.stack[-1]['skip']:
            self.parts.append(data)

    def text(self):
        return '\n'.join(line for line in
                         (re.sub(r'[\s\u3000]+', ' ', s).strip() for s in ''.join(self.parts).split('\n'))
                         if line and line not in ('[编辑]', '编辑'))


def _wiki_payload(body):
    value = json.loads(body)
    parsed = value.get('parse', {}) if isinstance(value, dict) else None
    if (not isinstance(parsed, dict) or not isinstance(parsed.get('text'), str) or not isinstance(parsed.get('revid'), int)
            or not isinstance(parsed.get('pageid'), int)):
        raise ValueError('原文 API 没有返回正文或版本标识')
    return parsed


class _ChapterLabels(HTMLParser):
    """Join a directory's chapter link with its adjacent actual chapter title."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items, self.parts, self.href, self.active = {}, [], None, False

    def handle_starttag(self, tag, attrs):
        if tag == 'li':
            self.parts, self.href, self.active = [], None, True
        elif tag == 'a' and self.active:
            href = dict(attrs).get('href', '')
            if re.fullmatch(r'/wiki/史記/卷\d{3}', unquote(href)):
                self.href = unquote(href).removeprefix('/wiki/')

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'li':
            if self.href:
                self.items[self.href] = '《史记》' + re.sub(r'\s+', ' ', ''.join(self.parts)).strip()
            self.active = False


def discover(index, fetched):
    """Discover chapters/events from the live index, not a fixed topic list."""
    if index['id'] == 'shiji':
        payload = _wiki_payload(fetched['body'])
        labels = _ChapterLabels()
        labels.feed(payload['text'])
        links = [('https://zh.wikisource.org/wiki/' + quote(item.get('title', ''), safe='/'),
                  labels.items.get(item.get('title'), item.get('title', ''))) for item in payload.get('links', [])
                 if isinstance(item, dict) and item.get('ns') == 0 and item.get('exists') is True]
    else:
        parser = _Page(index=True)
        parser.feed(fetched['body'])
        links = [(urljoin(index['url'], href), title) for href, title in parser.links]
    found = {}
    for url, title in links:
        # Historical indices contain old HTTP hrefs: upgrade only these known
        # hosts before validation; the network never follows insecure URLs.
        url = re.sub(r'^http://(?=(?:cpc\.people\.com\.cn|zh\.wikisource\.org)/)', 'https://', url)
        try:
            url = canonical_url(url)
        except (ValueError, TypeError):
            continue
        if url == index['url'] or (index['id'] == 'shiji' and '/%E5%8D%B7' not in url):
            continue
        if urlsplit(url).hostname != urlsplit(index['url']).hostname:
            continue
        if title and url not in found:
            found[url] = {'url': url, 'title': title[:200], 'scope': index['scope'], 'index_id': index['id']}
        if len(found) >= MAX_DISCOVERIES:
            break
    if not found:
        raise ValueError('索引没有发现允许的专题链接')
    return list(found.values())


def extract_document(candidate, fetched):
    wiki = candidate['index_id'] == 'shiji'
    payload = _wiki_payload(fetched['body']) if wiki else {}
    parser = _Page(wiki=wiki)
    parser.feed(payload['text'] if wiki else fetched['body'])
    text = parser.text()
    if not parser.root_seen or len(re.findall(r'[\u3400-\u9fff]', text)) < 180:
        raise ValueError('正文缺失或过短，不用于教学')
    if len(text) > MAX_TEXT_CHARS or '\ufffd' in text:
        raise ValueError('正文超出上限或字符解码异常')
    title = candidate['title'] if wiki else ''.join(parser.title_parts).split('--')[0].strip()
    if not title:
        raise ValueError('来源缺少标题')
    return {'title': title[:200], 'text': text, 'text_sha256': _sha(text),
            'response_sha256': fetched['response_sha256'], 'retrieval_url': fetched['retrieval_url'],
            'page_id': payload.get('pageid'), 'revision_id': payload.get('revid'),
            'bytes': fetched['bytes']}


def _load(db, key, default=None):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    if row:
        try:
            value = json.loads(row[0])
            return value if isinstance(value, dict) else default
        except (ValueError, TypeError):
            pass
    return default


def _save(db, key, value):
    db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, dumps(value)))


def _records(db):
    for row in db.execute('SELECT key,value FROM meta WHERE substr(key,1,?)=? ORDER BY key',
                          (len(DOCUMENT_PREFIX), DOCUMENT_PREFIX)):
        try:
            record = json.loads(row['value'])
            if (record['version'] == 1 and canonical_url(record['url']) == record['url']
                    and row['key'] == DOCUMENT_PREFIX + _sha(record['url'])
                    and record.get('id') == 'public-history-' + _sha(record['url'])[:24]
                    and isinstance(record.get('title'), str) and record['title']
                    and type(record.get('attempts')) is int and record['attempts'] >= 0
                    and record.get('status') in ('pending', 'ready', 'error', 'duplicate')
                    and record.get('index_id') in {i['id'] for i in INDEXES}):
                if record['status'] == 'ready' and (not isinstance(record.get('text'), str)
                        or _sha(record['text']) != record.get('text_sha256')
                        or not _time(record.get('fetched_at')) or not record.get('response_sha256')):
                    continue
                yield record
        except (ValueError, TypeError, KeyError, AttributeError):
            continue  # A damaged public cache must not take daily routines down.


def _time(value):
    try:
        parsed = dt.datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def _owns_lease(db, token):
    return _load(db, STATE_KEY, {}).get('lease_token') == token


def refresh_schedule_key(store):
    """Stable six-hour UTC bucket, for the existing unique background job queue."""
    return 'history-catalog-' + str(int(store.now().timestamp()) // (REFRESH_HOURS * 3600))


def refresh_due(store):
    now = store.now()
    with store.db() as db:
        state = _load(db, STATE_KEY, {})
    lease, due = _time(state.get('lease_until')), _time(state.get('next_due_at'))
    return not (lease and lease > now) and not (due and due > now)


def refresh_catalog(store, max_documents=2, *, fetcher=None, proxy_url=None, force=False):
    """One index + bounded articles, with all network calls outside transactions.

    Default at most three requests. Call from a low-priority existing worker job,
    never from the scheduler or prepare_learning. A fetch failure is persisted,
    returned as data, and retried after a delay; it never disables daily routines.
    """
    if type(max_documents) is not int or not 1 <= max_documents <= MAX_DOCUMENTS:
        raise ValueError('每轮正文抓取数须为 1–4')
    if type(force) is not bool:
        raise ValueError('手动来源刷新标记须为布尔值')
    proxy_url = public_proxy_url(proxy_url)
    fetcher = fetcher or (lambda url: fetch_public(url, proxy_url=proxy_url))
    token = uuid.uuid4().hex
    with store.db(True) as db:
        now = store.now()
        state = _load(db, STATE_KEY, {})
        lease, due = _time(state.get('lease_until')), _time(state.get('next_due_at'))
        if (lease and lease > now) or (not force and due and due > now):
            return {'status': 'skipped', 'reason': 'leased' if lease and lease > now else 'not_due'}
        cursor = state.get('index_cursor', 0)
        cursor = cursor if type(cursor) is int else 0
        index = INDEXES[cursor % len(INDEXES)]
        state.update(version=1, lease_token=token, lease_until=(now + dt.timedelta(seconds=LEASE_SECONDS)).isoformat(),
                     last_attempt_at=now.isoformat())
        _save(db, STATE_KEY, state)
    result = {'status': 'ok', 'index_id': index['id'], 'discovered': 0, 'fetched': 0, 'failed': 0, 'duplicates': 0}
    try:
        fetched = fetcher(index['url'])
        discoveries = discover(index, fetched)
        attempted = store.stamp()
        with store.db(True) as db:
            if not _owns_lease(db, token):
                return {'status': 'skipped', 'reason': 'lease_lost'}
            for candidate in discoveries:
                key = DOCUMENT_PREFIX + _sha(candidate['url'])
                if not _load(db, key):
                    candidate.update(version=1, id='public-history-' + _sha(candidate['url'])[:24],
                                     discovered_at=attempted, status='pending', attempts=0)
                    _save(db, key, candidate)
                    result['discovered'] += 1
            _save(db, INDEX_PREFIX + index['id'], dict(version=1, url=index['url'], status='ok',
                  attempted_at=attempted, fetched_at=attempted,
                  response_sha256=fetched['response_sha256'], discovered_count=len(discoveries)))
    except (ValueError, OSError, urllib.error.URLError, KeyError, TypeError) as exc:
        result['status'], result['index_error'] = 'partial', _error(exc)
        with store.db(True) as db:
            if not _owns_lease(db, token):
                return {'status': 'skipped', 'reason': 'lease_lost'}
            previous = _load(db, INDEX_PREFIX + index['id'], {})
            previous.update(version=1, url=index['url'], status='error', attempted_at=store.stamp(), error=_error(exc))
            _save(db, INDEX_PREFIX + index['id'], previous)
    # Claiming the entire refresh lease makes this queue safe across concurrent
    # workers. Documents themselves are never deleted by a failed index refresh.
    with store.db() as db:
        if not _owns_lease(db, token):
            return {'status': 'skipped', 'reason': 'lease_lost'}
        now = store.now()
        pending = [d for d in _records(db) if d.get('status') in ('pending', 'error')
                   and (not _time(d.get('retry_at')) or _time(d['retry_at']) <= now)]
    pending.sort(key=lambda d: (d.get('attempts', 0), d['index_id'] != index['id'], d['url']))
    for candidate in pending[:max_documents]:
        attempted = store.stamp()
        try:
            document = extract_document(candidate, fetcher(candidate['url']))
            with store.db(True) as db:
                if not _owns_lease(db, token):
                    return {'status': 'skipped', 'reason': 'lease_lost'}
                duplicate = next((d for d in _records(db) if d.get('status') == 'ready'
                                  and d.get('text_sha256') == document['text_sha256']
                                  and d['url'] != candidate['url']), None)
                candidate.update(document, status='duplicate' if duplicate else 'ready',
                                 fetched_at=attempted, attempted_at=attempted, error=None,
                                 attempts=candidate.get('attempts', 0) + 1,
                                 duplicate_of=duplicate['id'] if duplicate else None)
                _save(db, DOCUMENT_PREFIX + _sha(candidate['url']), candidate)
            result['duplicates' if duplicate else 'fetched'] += 1
        except (ValueError, OSError, urllib.error.URLError, KeyError, TypeError) as exc:
            candidate.update(status='error', attempted_at=attempted, error=_error(exc),
                             attempts=candidate.get('attempts', 0) + 1,
                             retry_at=(store.now() + dt.timedelta(days=1)).isoformat())
            with store.db(True) as db:
                if not _owns_lease(db, token):
                    return {'status': 'skipped', 'reason': 'lease_lost'}
                _save(db, DOCUMENT_PREFIX + _sha(candidate['url']), candidate)
            result['failed'] += 1
            result['status'] = 'partial'
    with store.db(True) as db:
        current = _load(db, STATE_KEY, {})
        if current.get('lease_token') == token:
            current.update(index_cursor=(cursor + 1) % len(INDEXES), lease_token=None, lease_until=None,
                           last_completed_at=store.stamp(), result=result,
                           next_due_at=(store.now() + dt.timedelta(hours=REFRESH_HOURS)).isoformat())
            _save(db, STATE_KEY, current)
    return result


def _error(exc):
    # Do not persist arbitrary server response bodies or request headers.
    if isinstance(exc, urllib.error.HTTPError):
        return 'HTTP ' + str(exc.code)
    if isinstance(exc, ValueError):
        return str(exc)[:160]
    return type(exc).__name__


def catalog_status(db):
    state = _load(db, STATE_KEY, {})
    records = list(_records(db))
    return {'last_completed_at': state.get('last_completed_at'),
            'last_result': state.get('result'),
            'ready': sum(d.get('status') == 'ready' for d in records),
            'pending': sum(d.get('status') == 'pending' for d in records),
            'failed': sum(d.get('status') == 'error' for d in records)}


def catalog_documents(store, *, include_text=False):
    """Inspect public provenance/failures; no private conversations are collected."""
    with store.db() as db:
        records = list(_records(db))
    if not include_text:
        for record in records:
            record.pop('text', None)
    return records


def _lesson_excerpt(text):
    paragraphs, count = [], 0
    for paragraph in text.splitlines():
        if count + len(paragraph) + 1 > LESSON_TEXT_CHARS:
            break
        paragraphs.append(paragraph)
        count += len(paragraph) + 1
    if not paragraphs:
        # An unbroken long paragraph is shortened at a sentence boundary.
        cut = text[:LESSON_TEXT_CHARS]
        boundary = max(cut.rfind('。'), cut.rfind('；'), cut.rfind('！'), cut.rfind('？'))
        return (cut[:boundary + 1] if boundary >= 180 else ''), '开篇连续节选（未覆盖全文）'
    return '\n'.join(paragraphs), '提取正文第1–' + str(len(paragraphs)) + '段（未必覆盖全文）'


def ready_history_cards(db):
    """Build independent lesson packets from intact cached text, never network."""
    cards = []
    for record in _records(db):
        text = record.get('text', '')
        if (record.get('status') != 'ready' or not isinstance(text, str)
                or _sha(text) != record.get('text_sha256') or not record.get('fetched_at')):
            continue
        excerpt, locator = _lesson_excerpt(text)
        if len(re.findall(r'[\u3400-\u9fff]', excerpt)) < 180:
            continue
        wiki = record['index_id'] == 'shiji'
        plan = dict(objective='能复述本节选叙述的3个具体事实，并解释1个人物选择、事件或制度之间的联系。',
                    explanation_steps=['先用来源中的人、时、地和主要问题建立背景；来源没给出的信息不补造。',
                                       '按原文次序解释关键事件、人物选择或制度安排，并将文言词句译为通顺的现代语。' if wiki
                                       else '按资料叙述的事件经过，讲清具体人物选择、会议决定或制度安排。',
                                       '用原文中一个实际片段示范怎样连接前后事件，直接讲清其中的行动、变化或制度联系。'],
                    worked_example='从给定节选选一个包含行动和结果的具体片段，先解释句意再说明联系；只能依据该片段，不编造场景或心理。',
                    common_misconception='叙述中的先后关系不自动证明唯一因果；文件宣布的决定也不等于已经完成的结果。',
                    optional_check='任选本课一个人物选择或制度安排，用自己的话说明发生了什么、与哪个后续变化相关。')
        cards.append(dict(id=record['id'], category='history', scope='二十四史' if wiki else '党史',
                          title=record['title'], source_url=record['url'], source_title=record['title'],
                          locator=locator, reviewed_on=None, verified_facts=[], excerpt=excerpt,
                          background='以《史记》本卷原文节选为学习对象，区分史书的叙事、人物话语和后人的解释。' if wiki
                          else '以人民网中国共产党新闻网历史专题资料的节选为依据，归因转述其事件叙述及评价。',
                          interpretation_boundary=('这是自动抓取并提取的公开来源文本，不是逐条人工核验后的事实清单；归因讲述，节选之外的背景需另查，不能声称本次实时核验。'
                                                   + ('遇上古传说及作者判断时保留“《史记》叙述”的归属，不能当作考古已证实的史实。' if wiki else '')),
                          source_kind='古代史书原文的公开转录；非校勘定本' if wiki else '人民网中国共产党新闻网党史专题资料',
                          connection_question=plan['optional_check'], teaching_plan=plan,
                          retrieval=dict(mode='automatic_source_text', fetched_at=record['fetched_at'],
                                         text_sha256=record['text_sha256'], excerpt_sha256=_sha(excerpt),
                                         response_sha256=record['response_sha256'],
                                         revision_id=record.get('revision_id'), page_id=record.get('page_id'),
                                         index_id=record['index_id'], source_verified=False)))
    return copy.deepcopy(cards)
