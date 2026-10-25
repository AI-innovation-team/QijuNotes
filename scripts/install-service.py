#!/usr/bin/env python3
"""Install project-only user services; never modify unrelated services."""
import json, os, pathlib, subprocess, sys
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dsh_personal.server import Application
from dsh_personal.operations import create_key
app=Application(pathlib.Path.home()/'.local/share/dsh-personal')
config=app.config();config.setdefault('backup',{})['host_unit']='dsh-personal-host.service'
key=pathlib.Path.home()/'.config/dsh-personal/backup.key'
if not key.exists():create_key(key)
config['backup']['key_file']=str(key)
app.config_path.write_text(json.dumps(config,ensure_ascii=False,indent=2)+'\n')
private_env=app.data/'runtime.env';private_env.write_text('DSH_GATEWAY_KEY='+app.gateway_key+'\n');os.chmod(private_env,0o600)
def quote(p):return '"'+str(p).replace('\\','\\\\').replace('"','\\"')+'"'
try:
 ts=json.loads(subprocess.check_output(['tailscale','status','--json'],text=True));private_ip=ts.get('Self',{}).get('TailscaleIPs',[''])[0]
except (OSError,subprocess.SubprocessError,ValueError):private_ip=''
private_flag=(' --private-host '+private_ip) if private_ip else ''
python_binary=quote(sys.executable)
units=pathlib.Path.home()/'.config/systemd/user';units.mkdir(parents=True,exist_ok=True)
base=f'''[Unit]
Description=DSH Personal 1.1
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={ROOT}
UMask=0077
Environment=PYTHONUNBUFFERED=1
EnvironmentFile={quote(private_env)}
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

'''
for name,command in [('dsh-personal',f'{python_binary} -m dsh_personal.server --data {quote(app.data)}{private_flag}'),('dsh-personal-host',f'{python_binary} {quote(ROOT/"runtime/launch_dsh.py")} --home {quote(app.data/"dsh")}')]:
 (units/(name+'.service')).write_text(base+'ExecStart='+command+'\n\n[Install]\nWantedBy=default.target\n')
for name,command,calendar in [('dsh-personal-backup','backup','*-*-* 03:30:00 Asia/Shanghai'),('dsh-personal-ledger','ledger','*-*-* *:*:00')]:
 (units/(name+'.service')).write_text(f'[Unit]\nDescription=DSH Personal {command}\n\n[Service]\nType=oneshot\nWorkingDirectory={ROOT}\nUMask=0077\nExecStart={python_binary} -m dsh_personal.operations {command} --data {quote(app.data)} --key {quote(key)}\n')
 (units/(name+'.timer')).write_text(f'[Unit]\nDescription=DSH Personal {command} timer\n\n[Timer]\nOnCalendar={calendar}\nPersistent=true\nUnit={name}.service\n\n[Install]\nWantedBy=timers.target\n')
subprocess.run(['systemctl','--user','daemon-reload'],check=True)
subprocess.run(['systemctl','--user','enable','--now','dsh-personal.service','dsh-personal-host.service','dsh-personal-backup.timer','dsh-personal-ledger.timer'],check=True)
subprocess.run(['systemctl','--user','is-active','dsh-personal.service','dsh-personal-host.service'],check=True)
print('Installed project services. Data:',app.data)
print('首次在本机浏览器创建账号：http://127.0.0.1:18780/；已有账号继续使用账号密码。')
print('Keep a separate secure recovery copy of:',key)
