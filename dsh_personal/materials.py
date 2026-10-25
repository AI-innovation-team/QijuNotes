"""Source-bound, versioned material projections; original imports stay immutable.

All projections use the existing records ledger and deletion graph. No copied
user messages, acceptance records, scheduled reminders, or inferred timestamps.
"""
import json
import hashlib
import datetime as dt
from .core import dumps


DATE_BASES={
    'conversation_month_day_label':'会话标注月日，年份未核；不等于每条消息或事件时间',
    'conversation_date_label':'会话标注日期；不等于每条消息或事件时间',
    'explicit_source_record_heading':'来源记录明确业务日期；精确原消息时间未提供',
}


def date_evidence(evidence):
    item=dict(evidence or {})
    item['basis']=DATE_BASES.get(item.get('basis'),item.get('basis','来源日期线索'))
    value=item.get('value')
    item['label']=('月'.join(value[2:].split('-'))+'日（年份未核）') if value and value.startswith('--') else value or '日期未能确认'
    return item


def full_day(evidence):
    try:return dt.date.fromisoformat(evidence.get('value')).isoformat()
    except (TypeError,ValueError):return None


def apply_plan(app, plan, *, dry_run=True):
    """Apply an already audited private plan with exact source hash checks.

    Stable IDs make interrupted applications resumable. Existing user revisions
    are never overwritten. All derived records participate in deletion/restore.
    """
    if plan.get('version')!=2 or not isinstance(plan.get('modules'),dict):raise ValueError('不支持的材料整理计划')
    records=app.records;prepared=[]
    allowed={'date_annotations','profiles','growth','sleep','reflection','learning_delivery','personal_history'}
    with app.store.db() as db:
        for module,items in plan['modules'].items():
            if module not in allowed:raise ValueError('未知材料模块')
            for item in items:
                eligible=item.get('eligible_for_active_context',item.get('context_eligible',False))
                if eligible and (module!='profiles' or not any(loc.get('role')=='user' for loc in item.get('source_locations',[]))):
                    raise ValueError('生效画像必须有明确本人原话依据')
                refs=[]
                for loc in item['source_locations']:
                    rows=db.execute('SELECT * FROM records_import_messages WHERE channel=? AND conversation_id=? AND source_id=? AND deleted=0 AND quarantined=0',
                        (loc['channel'],loc['conversation_id'],loc['source_id'])).fetchall()
                    if len(rows)!=1:raise ValueError('材料来源缺失或不唯一：'+item['id'])
                    row=rows[0]
                    if row['role']!=loc['role'] or hashlib.sha256(row['body'].encode()).hexdigest()!=loc['content_sha256']:
                        raise ValueError('材料来源角色或内容不一致：'+item['id'])
                    refs.append('import:'+row['message_id'])
                if not refs:raise ValueError('材料必须有实际来源')
                targets=[]
                if module=='date_annotations':
                    first=item['source_locations'][0]
                    for source_id in item['message_source_ids']:
                        row=db.execute('SELECT message_id FROM records_import_messages WHERE channel=? AND conversation_id=? AND source_id=? AND deleted=0 AND quarantined=0',
                            (first['channel'],first['conversation_id'],source_id)).fetchone()
                        if row:targets.append(row[0]);refs.append('import:'+row[0])
                ident='material_v2_'+hashlib.sha256(item['id'].encode()).hexdigest()[:24]
                refs=list(dict.fromkeys(refs));records._assert_refs(db,refs+[ident])
                prepared.append((module,item,ident,refs,targets))
    report={'dry_run':dry_run,'source_validation':'exact_body_hash_and_role','modules':{},'created':0,'existing':0,'ids':[]}
    from .chronicle import Chronicle
    user_title=Chronicle(app.store,records).settings()['user_title']
    for module,item,ident,refs,targets in prepared:
        report['modules'][module]=report['modules'].get(module,0)+1
        if dry_run:continue
        provenance={'actor':'qiju_agent','method':'material_compilation','plan_item':item['id'],
            'origin':item.get('origin'),'evidence_directness':item.get('evidence_directness'),
            'context_eligible':bool(item.get('eligible_for_active_context',item.get('context_eligible',False))),
            'activation_basis':item.get('activation_basis'),
            'uncertainties':item.get('uncertainties',[])}
        evidence=date_evidence(item.get('date'));day=full_day(evidence)
        if module=='growth':
            try:existing=records.get_growth(ident)
            except KeyError:existing=None
            if existing:report['existing']+=1;continue
            kind=item['suggested_kind']
            if kind in ('current_state','evaluation'):raise ValueError('历史整理不能冒充当前核对或用户评价')
            content={k:item[k] for k in ('title','origin','status','uncertainties') if k in item}
            content.update(text=item['content'].replace('{{user_title}}',user_title),date_evidence=evidence,date_label=evidence['label'])
            records.put_growth(item['line'],kind,content,source_refs=refs,item_id=ident,
                business_date=day or 'unknown',user_input=dumps(provenance))
        else:
            try:existing=records.get_profile(ident)
            except KeyError:existing=None
            if existing:report['existing']+=1;continue
            scope=module;category='background';body=item.get('content','')
            if module!='personal_history':body=body.replace('{{user_title}}',user_title)
            if module=='date_annotations':
                scope='date_annotation';body=dumps({'message_ids':targets,'date':evidence})
            elif module=='profiles':
                category=item['category'];scope='global' if provenance['context_eligible'] else 'reference:profile'
                body='### '+item.get('title','个人画像')+'\n\n'+body+'\n\n来源时期：'+evidence['label']+'。'
            elif module in ('sleep','reflection'):
                if day!=item['business_date'] or item.get('mark_as_user_answer') is not False:raise ValueError('历史活动日期或归因不合法')
                activity=app.store.ensure_activity(item['routine'],day,origin='material_import')
                scope='activity:'+activity['id']
                details=''
                if module=='sleep':
                    details='\n\n'+ '\n'.join('**'+key+'**：'+str(value) for key,value in item.get('source_fields',{}).items() if key!='当日建议')
                else:
                    sections=item.get('sections',{})
                    details=''.join('\n\n#### '+key+'\n'+sections[key] for key in item.get('source_fact_sections',[]) if key in sections)
                    mixed=[key for key in item.get('mixed_reflection_sections',[]) if key in sections]
                    details+=''.join('\n\n#### 原复盘自评与旧助手评述（来源转录） · '+key+'\n'+sections[key] for key in mixed)
                body=dumps({'body':body+details,'fields':item.get('normalized_fields',{}),
                    'date_evidence':evidence,'provenance':'Kimi依据口述整理的历史记录，由起居编修；不是逐字用户原话',
                    'uncertainties':item.get('uncertainties',[]),'origin':item['origin']})
            elif module=='learning_delivery':
                body=dumps({'body':body,'title':item['title'],'bibliographic_source':item['bibliographic_source'],
                    'date_evidence':evidence,'status':'source_reports_delivered','user_read':None,'user_mastery':None})
            elif module=='personal_history':
                linked=None
                if item.get('merge_policy')=='link_existing_manual':
                    anchor=item['manual_anchor_source_location']
                    with app.store.db() as db:
                        row=db.execute('SELECT message_id FROM records_import_messages WHERE channel=? AND conversation_id=? AND source_id=? AND deleted=0 AND quarantined=0',
                            (anchor['channel'],anchor['conversation_id'],anchor['source_id'])).fetchone()
                    if row:
                        from .chronicle import Chronicle
                        candidate=Chronicle._import_profile_id(row[0])
                        try:manual=records.get_profile(candidate)
                        except KeyError:manual=None
                        if manual and manual['confirmed']:
                            linked=candidate;refs.append('profile:'+candidate)
                scope='chronicle';body=dumps({'format':'qiju-material-narrative-v1','title':item.get('title','历史记事'),
                    'body':body,'date':evidence,'provenance':'起居依据历史材料编修；'+('本人原话' if item.get('evidence_directness')=='direct_excerpt' else '来源整理记录，非逐字原话'),
                    'uncertainties':item.get('uncertainties',[]),'linked_manual_profile_id':linked})
            records.add_profile(body,category=category,scope=scope,source_refs=refs,confirmed=False,
                original_at=day,user_input=dumps(provenance),item_id=ident)
        report['created']+=1;report['ids'].append({'module':module,'id':ident})
    return report


def compiled_profiles(records, include_inactive=False):
    result=[]
    if records.is_quarantined():return result
    for profile in records.list_profile(scope='chronicle',include_inactive=include_inactive):
        try:payload=json.loads(profile['content'])
        except (ValueError,TypeError):continue
        if isinstance(payload,dict) and payload.get('format')=='qiju-material-narrative-v1':
            result.append((profile,payload))
    return result


def covered_imports(records):
    # Explicitly hidden compiled entries still suppress their raw projections.
    # Context dependencies invalidate prose on deletion, but are not events
    # narrated by this entry. Keep anchors in the document: a human correction
    # replaces user_input provenance while retaining document metadata.
    covered=set()
    for profile,payload in compiled_profiles(records,True):
        refs=payload.get('entry_source_refs')
        if (not isinstance(refs,list) or not refs or
                any(not isinstance(ref,str) for ref in refs) or
                not set(refs)<=set(profile['source_refs'])):
            refs=profile['source_refs']
        covered.update(ref.removeprefix('import:') for ref in refs if ref.startswith('import:'))
    return covered


def compiled_entries(records,user_title):
    result=[]
    for profile,payload in compiled_profiles(records):
        # Manual edits/hides remain authoritative; absence is not permission to
        # resurrect a substitute. The dependency graph owns source removal.
        if payload.get('linked_manual_profile_id'):continue
        evidence=payload.get('date',{})
        value=evidence.get('value')
        import datetime as dt
        try:day=dt.date.fromisoformat(value).isoformat()
        except (ValueError,TypeError):day=None
        result.append({'id':'chronicle:compiled:'+profile['item_id'],'kind':'compiled_material',
            'title':payload.get('title','个人史记事'),'body':payload['body'].replace('{{user_title}}',user_title),
            'business_date':day,'date_label':evidence.get('label') or value,
            'date_basis':evidence.get('basis'),'recorded_at':profile['imported_at'],
            'status':'recorded','source_refs':profile['source_refs'],'source_ids':[],
            'activity_id':None,'import_id':None,'compiled_profile_id':profile['item_id'],
            'correction_version':profile['version'],'links':[],
            'formation':'manual_revision' if profile['confirmed'] else 'agent_synthesis',
            'basis':payload.get('provenance','起居依据历史材料编修，保留来源及不确定性；可随时更正')})
    return result


def revise_compiled(records,profile_id,body,expected_version,excluded=False):
    prior=records.get_profile(profile_id)
    payload=json.loads(prior['content'])
    if prior['scope']!='chronicle' or payload.get('format')!='qiju-material-narrative-v1':
        raise ValueError('不是可编辑的材料编修记载')
    if not isinstance(body,str) or len(body)>12000 or (not excluded and not body.strip()):
        raise ValueError('记载正文为空或过长')
    payload['body']=body.strip() or payload['body']
    return records.revise_profile(profile_id,expected_version,'用户在个人史控件更正或隐藏编修记载',
        content=dumps(payload),active=not excluded,confirmed=True)
