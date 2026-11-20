"""Explicit bulk confirmation reuses single-row guards and idempotent writes."""
import base64
import hashlib
import hmac
import json
import os
import time
from fastapi import APIRouter,HTTPException
from pydantic import BaseModel,Field
from app.registry_enrichment import get_conn
from app.import_confirmation import _load_row,_analyze,_row_contacts,_row_company_contacts,confirm_import_row

router=APIRouter(prefix='/imports',tags=['import-bulk'])


class Preview(BaseModel):
    row_ids:list[int]=Field(min_length=1,max_length=25)


class Confirm(BaseModel):
    token:str=Field(max_length=30000)
    reviewed:bool=False
    acknowledge_warnings:bool=False


def digest(row):
    value=dict(row)
    for key in ('enriched_emails','enriched_phones'):
        value[key]=sorted(value.get(key) or [])
    return hashlib.sha256(json.dumps(value,default=str,sort_keys=True).encode()).hexdigest()


def sign(payload):
    key=os.environ.get('GAB_APP_PASSWORD','')
    if not key:raise HTTPException(503,'Авторизация не настроена')
    body=base64.urlsafe_b64encode(json.dumps(payload,separators=(',',':')).encode()).decode()
    mac=hmac.new(key.encode(),('gab-import-bulk:'+body).encode(),hashlib.sha256).hexdigest()
    return body+'.'+mac


def verify(token,batch_id):
    try:
        body,mac=token.rsplit('.',1)
        payload=json.loads(base64.urlsafe_b64decode(body))
        expected=sign(payload)
        if not hmac.compare_digest(expected,token) or payload['batch']!=batch_id or not 0<=time.time()-payload['issued']<=3600 or len(payload['items'])>25:raise ValueError()
        return payload
    except (ValueError,KeyError,TypeError):raise HTTPException(409,'Предпросмотр устарел или изменён; выполните проверку заново') from None


@router.post('/{batch_id}/confirm-preview')
def preview(batch_id:int,body:Preview):
    items=[];allowed=[]
    with get_conn() as conn:
        batch=conn.execute('SELECT status FROM gab_crm_import_batches WHERE id=%s',[batch_id]).fetchone()
        if not batch or batch['status'] not in ('review','ready','completed'):raise HTTPException(409,'Пакет ещё не готов к переносу')
        for row_id in dict.fromkeys(body.row_ids):
            row=_load_row(conn,batch_id,row_id)
            analysis=_analyze(conn,row)
            reasons=analysis['reasons']
            hard=[r for r in reasons if r!='extraction_warnings_require_manual_review']
            ready=row['import_status'] in ('pending','needs_review') and not hard
            plan={k:analysis['suggested_'+k] for k in ('company_id','contact_id','investor_id')}
            items.append({'row_id':row_id,'company_name':row['company_name'],'full_name':row['full_name'],
                'inn':row['inn'],'ready':ready,'already_confirmed':row['import_status'] in ('created','matched'),
                'reasons':reasons,'warnings':row.get('extraction_warnings') or [],
                'requires_warning_review':bool(reasons),'plan':plan,
                'emails':_row_contacts(row)[0],'company_emails':_row_company_contacts(row)[0]})
            if ready:allowed.append({'row_id':row_id,'digest':digest(row),'version':row['edit_version'],
                                      'expected_plan':plan,'warnings':reasons})
    return {'items':items,'token':sign({'batch':batch_id,'issued':int(time.time()),'items':allowed})}


@router.post('/{batch_id}/confirm-all')
def confirm_all(batch_id:int,body:Confirm):
    if body.reviewed is not True:raise HTTPException(422,'Сначала подтвердите проверку списка')
    payload=verify(body.token,batch_id)
    results=[]
    with get_conn() as conn:
        lock='gab-bulk-confirm:'+str(batch_id)
        if not conn.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS ok',[lock]).fetchone()['ok']:
            raise HTTPException(409,'Этот пакет уже подтверждается; дождитесь результата')
        try:
            for item in payload['items']:
                row=_load_row(conn,batch_id,item['row_id'])
                if row['import_status'] in ('created','matched') and row['investor_id']:
                    results.append({'row_id':row['id'],'status':'already_confirmed'});continue
                if item['warnings'] and not body.acknowledge_warnings:
                    results.append({'row_id':row['id'],'status':'skipped','error':'Требуется подтверждение проверки предупреждений'});continue
                if digest(row)!=item['digest']:
                    results.append({'row_id':row['id'],'status':'skipped','error':'Данные изменились; повторите предпросмотр'});continue
                try:
                    expected_plan=item['expected_plan']
                    analysis=_analyze(conn,row)
                    actual={key:analysis['suggested_'+key] for key in ('company_id','contact_id','investor_id')}
                    # Earlier rows in this reviewed batch can create shared
                    # company/contact cards. Reuse only those newly confirmed
                    # links; unrelated concurrent CRM changes still invalidate
                    # the preview. Single-row identity/financial guards remain.
                    reusable=True
                    for key,value in actual.items():
                        if value==expected_plan[key]:continue
                        if expected_plan[key] is not None or value is None:
                            reusable=False;break
                        found=conn.execute(f'''SELECT 1 FROM gab_crm_import_rows WHERE batch_id=%s
                            AND {key}=%s AND confirmed_at>=to_timestamp(%s)
                            AND import_status IN ('created','matched') LIMIT 1''',[batch_id,value,payload['issued']]).fetchone()
                        if not found:reusable=False;break
                    if reusable:expected_plan=actual
                    result=confirm_import_row(batch_id,row['id'],{'version':item['version'],
                        'expected_plan':expected_plan,'acknowledged_warnings':item['warnings']})
                    results.append(result)
                except HTTPException as exc:
                    results.append({'row_id':row['id'],'status':'skipped','error':exc.detail})
        finally:conn.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))',[lock])
    return {'items':results,'transferred':sum(r['status'] in ('created','matched') and not r.get('already_confirmed') for r in results)}
