#!/usr/bin/env python3
"""Validate evidence bookkeeping against the immutable baseline, never manufacture PASS."""
import collections,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
baseline=json.loads((ROOT/'plans/dsh_codex_taskpack_v1.1/acceptance/ACCEPTANCE_MATRIX.json').read_text())
ledger=json.loads((ROOT/'progress/ACCEPTANCE.json').read_text())
expected={s['id']:s for c in baseline['cases'] for s in c['subchecks']}
actual={s['id']:s for c in ledger['cases'] for s in c['subchecks']}
errors=[]
if expected.keys()!=actual.keys():errors.append('Required baseline subchecks were added/removed')
for ident,sub in actual.items():
 old=expected.get(ident,{})
 for k in ('criterion','gate','required','evidence_kind'):
  if sub.get(k)!=old.get(k):errors.append(f'{ident}: baseline {k} changed')
 if sub['status'] not in ledger['allowed_statuses']:errors.append(f'{ident}: invalid status')
 for evidence in sub['evidence']:
  if not (ROOT/evidence).is_file():errors.append(f'{ident}: missing evidence {evidence}')
 if sub['status']=='PASS' and (not sub['actual_result'] or not sub['evidence'] or sub['remaining_conditions']):errors.append(f'{ident}: unsupported PASS')
 if sub['status']!='PASS' and not sub['remaining_conditions']:errors.append(f'{ident}: missing remaining condition')
for case in ledger['cases']:
 passed=all(s['status']=='PASS' for s in case['subchecks'] if s['required'])
 if (case['status']=='PASS')!=passed:errors.append(f"{case['id']}: inconsistent parent status")
gates={g:all(s['status']=='PASS' for s in actual.values() if s['required'] and (g=='FULL' or s['gate']=='CORE')) for g in ['CORE','FULL']}
if ledger.get('gate_results')!=gates:errors.append('Gate results disagree with required subchecks')
coverage={r for c in ledger['cases'] for r in c['requirement_ids']}
if not set(ledger['required_requirement_ids']).issubset(coverage):errors.append('Requirement mapping incomplete')
print(json.dumps({'status':'FAIL' if errors else 'VALID_BOOKKEEPING_NOT_ACCEPTANCE','subchecks':len(actual),'counts':dict(collections.Counter(s['status'] for s in actual.values())),'gates':gates,'errors':errors},ensure_ascii=False,indent=2))
raise SystemExit(bool(errors))
