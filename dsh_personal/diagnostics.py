"""Local evidence and configuration readiness; never infer acceptance or expose secrets."""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import urllib.parse
from .backup_targets import validate_backup_targets

def boot_evidence(source_commit):
    result={'at':dt.datetime.now(dt.timezone.utc).isoformat(),'source_commit':source_commit,
            'uid':os.getuid(),'system_boot_id':None,'boot_started_at':None,
            'user_sessions':None,'linger':None,'user_state':None,
            'acceptance':'OBSERVATION_ONLY_NOT_PASS'}
    try:
        result['system_boot_id']=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        boot=next(int(line.split()[1]) for line in Path('/proc/stat').read_text().splitlines() if line.startswith('btime '))
        result['boot_started_at']=dt.datetime.fromtimestamp(boot,dt.timezone.utc).isoformat()
    except (OSError,ValueError,StopIteration):pass
    try:
        raw=subprocess.check_output(['loginctl','show-user',str(os.getuid()),'-p','Sessions','-p','Linger','-p','State'],text=True,stderr=subprocess.DEVNULL,timeout=3)
        fields=dict(line.split('=',1) for line in raw.splitlines() if '=' in line)
        result['user_sessions']=fields['Sessions'].split() if 'Sessions' in fields else None
        result['linger']=fields.get('Linger');result['user_state']=fields.get('State')
    except (OSError,subprocess.SubprocessError):pass
    return result

def _secret_file(path):
    # Paths can themselves contain credentials; only return presence/readability/perms.
    if not isinstance(path,str) or not path:return {'configured':False,'readable':False,'private_mode':None}
    try:
        p=Path(path).expanduser();stat=p.stat()
        return {'configured':True,'readable':p.is_file() and os.access(p,os.R_OK) and stat.st_size>0,
                'private_mode':stat.st_mode & 0o077==0}
    except (OSError,ValueError):return {'configured':True,'readable':False,'private_mode':None}

def model_configuration_id(model):
    """Bind a verification to endpoint/model and the private file's identity, never its contents."""
    try:
        stat=Path(model['api_key_file']).expanduser().stat()
        identity=[model.get('base_url'),model.get('name'),stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns]
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    except (OSError,KeyError,TypeError,ValueError):return None

def notification_configuration_id(conf):
    """Identify the tested channel without reading or returning credential contents."""
    try:
        notification=conf['notification']
        if not isinstance(notification,dict) or notification.get('backend')!='ntfy':return None
        endpoint=notification['url'];topic=notification['topic'];public_url=conf['public_url']
        if not all(isinstance(value,str) and value for value in (endpoint,topic,public_url)):return None
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',topic):return None
        for address in (endpoint,public_url):
            parsed=urllib.parse.urlsplit(address)
            if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:return None
            # Force validation of a supplied port, too.
            parsed.port
        parsed=urllib.parse.urlsplit(endpoint)
        if parsed.path not in ('','/'):return None
        path=Path(notification['token_file']).expanduser()
        if not path.is_absolute():return None
        info=path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid!=os.getuid()
                or info.st_size<=0 or not os.access(path,os.R_OK)):return None
        identity=[notification['backend'],endpoint,topic,str(path),public_url,
                  info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns,
                  info.st_mode,info.st_uid]
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    except (AttributeError,OSError,KeyError,TypeError,ValueError):return None

def notification_readiness(data,conf):
    """Read a bounded, explicit user-confirmed device proof; never perform network I/O."""
    notification=conf.get('notification',{}) if isinstance(conf,dict) else {}
    configured=bool(notification.get('url')) if isinstance(notification,dict) else False
    result={'configured':configured,'real_device_verified':False,'verified_at':None,
            'missing_information':[] if configured else ['接收通道尚未配置；设备信息以docs/USER_INPUTS.md为准'],
            'note':'真机通过状态仅表示当前配置的一次锁屏接收及正确活动深链接已核对；不代表长期后台稳定或全体验验收。配置或凭据变化后需重验。'}
    identity=notification_configuration_id(conf)
    if not identity:return result
    try:
        proof_path=Path(data).expanduser()/'notification-verification.json'
        fd=os.open(proof_path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as handle:
            info=os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size>65536:return result
            raw=handle.read(65537)
        if len(raw)>65536:return result
        proof=json.loads(raw)
        if not isinstance(proof,dict):return result
        receipt=proof.get('receipt');verified_at=proof.get('verified_at')
        if not isinstance(receipt,dict) or not isinstance(verified_at,str) or len(verified_at)>64:return result
        when=dt.datetime.fromisoformat(verified_at)
        if when.tzinfo is None or when.utcoffset() is None:return result
        if (proof.get('type')!='real_device_user_report'
                or proof.get('locked_screen_received') is not True
                or proof.get('correct_activity_opened') is not True
                or proof.get('configuration_id')!=identity
                or receipt.get('backend')!='ntfy'
                or type(receipt.get('http_status')) is not int or receipt['http_status']!=200
                or receipt.get('state')!='accepted'):return result
        for key in ('message_id','sequence_id'):
            value=receipt.get(key)
            if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',value):return result
        # A credential/config change while reading the proof cannot produce a stale success.
        if notification_configuration_id(conf)!=identity:return result
        result.update(real_device_verified=True,verified_at=verified_at)
    except (OSError,ValueError,TypeError,OverflowError,RecursionError):pass
    return result

def readiness(data):
    data=Path(data).expanduser().resolve();config=data/'config.json'
    try:conf=json.loads(config.read_text())
    except (OSError,ValueError):conf={}
    model=conf.get('model',{});notification=conf.get('notification',{});backup=conf.get('backup',{})
    model_key=_secret_file(model.get('api_key_file'));address=model.get('base_url','')
    valid_url=False
    try:
        parsed=urllib.parse.urlsplit(address)
        valid_url=parsed.scheme=='https' and bool(parsed.hostname) or parsed.scheme=='http' and parsed.hostname in {'127.0.0.1','localhost'}
        valid_url=bool(valid_url and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment)
    except (ValueError,TypeError):pass
    missing=[]
    for k,present in [('model.base_url',valid_url),('model.name',bool(model.get('name'))),('model.api_key_file',model_key['readable'])]:
        if not present:missing.append(k)
    offsite=backup.get('offsite_dir');offsite_exists=False
    if isinstance(offsite,str) and offsite:
        try:offsite_exists=Path(offsite).expanduser().is_dir()
        except (OSError,ValueError):pass
    target_check={'configured':bool(offsite),'mount_matches_expected':False,'error':None}
    if offsite:
        try:
            checked=validate_backup_targets(data,backup.get('key_file'),
                backup.get('directory') or data/'backups',
                backup.get('ledger_directory') or data/'recovery-ledger',
                offsite,backup.get('offsite_mount'))
            target_check['mount_matches_expected']=checked['mount_matches_expected']
        except (ValueError,TypeError,OSError):
            target_check['error']='异机目标、密钥分离或预期挂载身份未通过只读检查；停止复制，需核对配置。'
    boots=[];observation_started=None;verified=False;verified_at=None
    db=data/'personal.sqlite'
    if db.is_file():
        with sqlite3.connect('file:'+urllib.parse.quote(str(db))+'?mode=ro',uri=True) as c:
            try:
                boots=[json.loads(r[0]) for r in c.execute("SELECT details FROM observations WHERE kind='process_start' ORDER BY at")]
                row=c.execute("SELECT value FROM meta WHERE key='observation_started'").fetchone();observation_started=row[0] if row else None
            except sqlite3.DatabaseError:pass
            try:
                row=c.execute("SELECT value FROM meta WHERE key='provider_verification'").fetchone()
                proof=json.loads(row[0]) if row else {}
                receipt=c.execute('SELECT u.state,d.model,d.response_state,d.usage_state FROM usage u JOIN usage_details d ON d.reservation_id=u.id WHERE u.id=?',(proof.get('receipt_id'),)).fetchone()
                verified=bool(not missing and proof.get('configuration_id')==model_configuration_id(model) and receipt==('settled',model.get('name'),'completed','valid'))
                if verified:verified_at=proof.get('verified_at')
            except (sqlite3.DatabaseError,ValueError,TypeError):pass
    return {'kind':'read_only_readiness_no_external_requests','checked_at':dt.datetime.now(dt.timezone.utc).isoformat(),
        'model':{'configured':not missing,'missing_fields':missing,'credential_file':model_key,
                 'actual_provider_verified':verified,'verified_at':verified_at,'note':'实际调用回执不代替内容质量、推理口径或用户体验验收；更换模型/地址/凭据文件后须重验。'},
        'notification':notification_readiness(data,conf),
        'backup':{'offsite_configured':bool(offsite),'offsite_directory_exists':offsite_exists,
                  'offsite_target_check':target_check,
                  'recovery_key':_secret_file(backup.get('key_file')),'offsite_restore_verified':False,
                  'note':'另一目录或挂载存在不等于异机恢复及独立密钥可用。'},
        'boot':{'record_count':len(boots),'distinct_system_boots':len({b['system_boot_id'] for b in boots if b.get('system_boot_id')}),
                'latest':boots[-1] if boots else None,'cold_boot_acceptance':'PENDING_REVIEW'},
        'observation_started':observation_started,'gate_changes':False}
