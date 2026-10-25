#!/usr/bin/env python3
"""One bounded real call through the running service; no credentials or user text in evidence."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dsh_personal.diagnostics import model_configuration_id

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,default=Path.home()/'.local/share/dsh-personal')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    root=args.data.expanduser().resolve()
    conf=json.loads((root/'config.json').read_text())
    model=conf.get('model',{})
    configuration_before=model_configuration_id(model)
    if not configuration_before:
        raise SystemExit('Configured credential file is unavailable; no verification performed.')
    if model.get('base_url','').rstrip('/') not in {'https://api.deepseek.com','https://api.deepseek.com/v1'}:
        raise SystemExit('This probe is scoped to the configured official DeepSeek endpoint.')
    pid=int(subprocess.check_output(['systemctl','--user','show','dsh-personal.service','-p','MainPID','--value'],text=True).strip())
    if not pid or (Path('/proc')/str(pid)).stat().st_uid!=os.getuid():
        raise SystemExit('The expected service account is not running.')
    before=dt.datetime.now(dt.timezone.utc).isoformat()
    with sqlite3.connect(root/'personal.sqlite') as db:
        prior={r[0] for r in db.execute('SELECT id FROM usage')}
    report={'started_at':before,'synthetic_test':True,'via_running_service_gateway':True,
            'service':{'unit':'dsh-personal.service','pid':pid,'uid':os.getuid()},
            'model':model.get('name'),'requested_max_tokens':32,'credential_in_output':False}
    payload={'model':'foreground-model','messages':[{'role':'user','content':'This is an integration test. Reply only: DSH_API_OK'}],
             'max_tokens':32,'stream':False}
    req=urllib.request.Request('http://127.0.0.1:18782/v1/chat/completions',json.dumps(payload).encode(),
        {'Content-Type':'application/json','Authorization':'Bearer '+(root/'gateway-key').read_text().strip()})
    try:
        with urllib.request.urlopen(req,timeout=60) as response:
            result=json.load(response)
            report.update({'http_status':response.status,'output_matches':result['choices'][0]['message']['content'].strip()=='DSH_API_OK',
                           'response_model':result.get('model'),'finish_reason':result['choices'][0].get('finish_reason')})
    except urllib.error.HTTPError as error:
        report.update({'http_status':error.code,'result':'gateway_rejected'})
    except Exception as error:
        report.update({'result':'request_failed','error_class':type(error).__name__})
    with sqlite3.connect(root/'personal.sqlite') as db:
        db.row_factory=sqlite3.Row
        receipts=[dict(r) for r in db.execute('SELECT u.*,d.model,d.output_limit_field,d.output_limit,d.reported_total,d.usage_json,d.response_state,d.usage_state FROM usage u JOIN usage_details d ON d.reservation_id=u.id')
                  if r['id'] not in prior and r['kind']=='foreground' and r['model']==model.get('name') and r['output_limit']==32]
        report['usage_receipts']=receipts
        try:
            current_model=json.loads((root/'config.json').read_text()).get('model',{})
            configuration_unchanged=model_configuration_id(current_model)==configuration_before
        except (OSError,ValueError,TypeError):
            configuration_unchanged=False
        report['configuration_unchanged']=configuration_unchanged
        passed=bool(report.get('output_matches') and len(receipts)==1 and receipts[0]['state']=='settled'
                    and receipts[0]['response_state']=='completed' and receipts[0]['usage_state']=='valid' and configuration_unchanged)
        if passed:
            proof={'configuration_id':configuration_before,'receipt_id':receipts[0]['id'],
                   'verified_at':dt.datetime.now(dt.timezone.utc).isoformat()}
            db.execute("INSERT OR REPLACE INTO meta VALUES('provider_verification',?)",(json.dumps(proof),))
    report.update({'finished_at':dt.datetime.now(dt.timezone.utc).isoformat(),'status':'PASS' if passed else 'FAIL'})
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if passed else 1

if __name__=='__main__':
    sys.exit(main())
