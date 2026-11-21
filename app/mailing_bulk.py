"""Explicit manual campaign dispatch through the existing single-mail n8n sender.

No scheduler and no startup resume. A recipient is attempted once. Any uncertain
SMTP/webhook outcome pauses the campaign and is never automatically retried.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import time
import urllib.request
import uuid
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field
from app.registry_enrichment import get_conn
from app.mailing_payload import MAX_ATTACHMENTS_BYTES, encode_payload, validate_planned_payload
from app.mailing_audience import SCOPES, in_scope, revenue_details

router=APIRouter(prefix='/api/mailings',tags=['bulk-mailings'])
LOCK="hashtextextended('gab-manual-mailing-dispatch', 0)"
EMAIL=re.compile(r'[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+')
MAX_ATTACHMENTS=MAX_ATTACHMENTS_BYTES


class Launch(BaseModel):
    confirm: Literal[True]
    preview_hash: str = Field(min_length=64,max_length=64)


class Resume(BaseModel):
    confirm: Literal[True]
    run_id: uuid.UUID


def event(conn,mailing_id,kind,detail,recipient_id=None):
    conn.execute('INSERT INTO gab_crm_mailing_events(mailing_id,recipient_id,event_type,detail_json) VALUES (%s,%s,%s,%s)',
                 [mailing_id,recipient_id,kind,Jsonb(detail)])


def transport_settings():
    from app.mailings_api import WEBHOOK_URL,WEBHOOK_TOKEN
    parts=urlsplit(WEBHOOK_URL)
    if parts.scheme!='https' or parts.hostname!='n8n.techsmysl.ru' or parts.port not in (None,443) or not parts.path.startswith('/webhook/') or parts.username or not WEBHOOK_TOKEN:
        raise HTTPException(503,'Почтовый webhook не настроен')
    return WEBHOOK_URL,WEBHOOK_TOKEN


def attachments_payload(items,encode=False):
    from app.mailings_api import STORAGE_DIR
    total=sum(int(item['size_bytes']) for item in items)
    if total>MAX_ATTACHMENTS:
        raise HTTPException(422,'Общий размер вложений превышает лимит почтового workflow — 12 МиБ')
    result=[]
    for item in items:
        path=(STORAGE_DIR/str(item['mailing_id'])/item['storage_filename']).resolve()
        if STORAGE_DIR.resolve() not in path.parents or not path.is_file():
            raise HTTPException(422,'Вложение отсутствует')
        if path.stat().st_size!=int(item['size_bytes']):
            raise HTTPException(422,'Размер вложения изменился')
        data=path.read_bytes()
        if len(data)!=int(item['size_bytes']) or hashlib.sha256(data).hexdigest()!=item['sha256']:
            raise HTTPException(422,'Вложение изменилось: сохраните его заново')
        result.append({'filename':item['original_filename'],'mime_type':item['mime_type'],
                       'data_base64':base64.b64encode(data).decode()} if encode else {key:item[key] for key in ('id','mailing_id','original_filename','storage_filename','mime_type','size_bytes','sha256')})
    return result


def plan(conn,mailing_id):
    transport_settings()  # validation only: no request
    mailing=conn.execute('SELECT * FROM gab_crm_mailings WHERE id=%s FOR UPDATE',[mailing_id]).fetchone()
    if not mailing:raise HTTPException(404,'Рассылка не найдена')
    if mailing.get('bulk_run_id') or mailing['status'] in ('sending','sent','archived'):
        raise HTTPException(409,'Рассылка уже запускалась. Откройте её статус')
    if not mailing['subject'].strip() or not (mailing['body_html'].strip() or mailing['body_text'].strip()):
        raise HTTPException(422,'Сохраните тему и текст письма')
    if re.search(r'{{\s*[^}]+\s*}}',mailing['subject']+mailing['body_html']+mailing['body_text']):
        raise HTTPException(422,'Замените переменные {{...}}: персонализация пока не подключена')
    attachments=attachments_payload(conn.execute('SELECT * FROM gab_crm_mailing_attachments WHERE mailing_id=%s ORDER BY id',[mailing_id]).fetchall())
    rows=conn.execute('SELECT * FROM gab_crm_mailing_recipients WHERE mailing_id=%s ORDER BY id FOR UPDATE',[mailing_id]).fetchall()
    opted_out={r['email'] for r in conn.execute("SELECT DISTINCT lower(btrim(email)) AS email FROM gab_crm_mailing_recipients WHERE status='opted_out' AND email IS NOT NULL").fetchall()}
    eligible=[];excluded=[];seen=set()
    scope=mailing.get('recipient_scope','all')
    if scope not in SCOPES:raise HTTPException(422,'Неизвестная группа получателей')
    for row in rows:
        email=(row['email'] or '').strip().lower()
        reason=None
        if not email:reason='Нет email'
        elif not EMAIL.fullmatch(email):reason='Некорректный email'
        elif email in opted_out:reason='Отказ от рассылок'
        elif row['status']!='pending' or row['delivery_state']!='pending':reason='Адрес уже обрабатывался'
        elif not in_scope(row,scope):reason='Нет подтверждённой выручки компании от 100 млн ₽'
        elif email in seen:reason='Повторяющийся email'
        if reason:excluded.append({'id':row['id'],'email':email,'reason':reason})
        else:
            seen.add(email)
            selected={'id':row['id'],'email':email,'name':row['recipient_name']}
            if scope=='revenue_100m':selected['company_revenue']=revenue_details(row)
            eligible.append(selected)
    if len(eligible)>5000:raise HTTPException(422,'Не более 5000 адресов за один запуск')
    try:
        validate_planned_payload(mailing_id,mailing['subject'],mailing['body_html'],mailing['body_text'],attachments,[r['email'] for r in eligible])
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc
    frozen={'recipient_scope':scope,'subject':mailing['subject'],'body_html':mailing['body_html'],'body_text':mailing['body_text'],
            'attachments':attachments,'recipients':eligible}
    digest=hashlib.sha256(json.dumps(frozen,sort_keys=True,default=str,ensure_ascii=False).encode()).hexdigest()
    return mailing,frozen,excluded,digest


@router.post('/{mailing_id}/bulk-preview')
def preview(mailing_id:int):
    with get_conn() as conn:
        mailing,frozen,excluded,digest=plan(conn,mailing_id)
    return {'mailing_id':mailing_id,'preview_hash':digest,'recipient_scope':frozen['recipient_scope'],'subject':frozen['subject'],
            'body_html':frozen['body_html'],'body_text':frozen['body_text'],
            'recipients':frozen['recipients'],'count':len(frozen['recipients']),
            'excluded':excluded,'attachments':[{'filename':a['original_filename'],'size_bytes':a['size_bytes']} for a in frozen['attachments']],
            'can_send':bool(frozen['recipients'])}


def capacity(conn):
    if not conn.execute('SELECT pg_try_advisory_xact_lock('+LOCK+') AS acquired').fetchone()['acquired']:
        raise HTTPException(409,'Другая отправка ещё выполняется')


@router.post('/{mailing_id}/bulk-send')
def launch(mailing_id:int,body:Launch,background:BackgroundTasks):
    with get_conn() as conn:
        capacity(conn)
        existing=conn.execute('SELECT * FROM gab_crm_mailing_runs WHERE mailing_id=%s',[mailing_id]).fetchone()
        if existing:return {'started':False,'already_started':True,'run_id':existing['id']}
        if conn.execute("SELECT id FROM gab_crm_mailing_runs WHERE status='running' LIMIT 1").fetchone():
            raise HTTPException(409,'Есть незавершённая рассылка. Проверьте её статус')
        mailing,frozen,excluded,digest=plan(conn,mailing_id)
        if digest!=body.preview_hash:raise HTTPException(409,'Письмо, вложения или получатели изменились. Повторите предпросмотр')
        if not frozen['recipients']:raise HTTPException(422,'Нет получателей для отправки')
        actor=os.environ.get('GAB_APP_USER')
        if not actor:raise HTTPException(503,'Пользователь аудита не настроен')
        run_id=uuid.uuid4();token=uuid.uuid4()
        conn.execute("INSERT INTO gab_crm_mailing_runs(id,mailing_id,status,approved_hash,payload,worker_token,started_by) VALUES (%s,%s,'running',%s,%s,%s,%s)",
                     [run_id,mailing_id,digest,Jsonb(frozen),token,actor])
        for recipient in frozen['recipients']:
            conn.execute('UPDATE gab_crm_mailing_recipients SET bulk_run_id=%s,delivery_request_id=%s WHERE id=%s',
                         [run_id,uuid.uuid4(),recipient['id']])
        conn.execute("UPDATE gab_crm_mailings SET bulk_run_id=%s,status='sending',updated_at=now() WHERE id=%s",[run_id,mailing_id])
        event(conn,mailing_id,'bulk_manually_confirmed',{'actor':actor,'run_id':str(run_id),'count':len(frozen['recipients']),'preview_hash':digest,'excluded':len(excluded)})
    background.add_task(dispatch,run_id,token)
    return {'started':True,'run_id':run_id,'count':len(frozen['recipients'])}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def send_one(payload):
    url,token=transport_settings()
    request=urllib.request.Request(url,data=encode_payload(payload),method='POST',
        headers={'Content-Type':'application/json','X-GAB-Webhook-Token':token})
    with urllib.request.build_opener(NoRedirect).open(request,timeout=90) as response:
        if not 200<=response.status<300:raise ValueError('Unconfirmed HTTP status')
        result=json.loads(response.read(100000))
    email=payload['to_email'].lower()
    def address(value):return (value if isinstance(value,str) else value.get('address','')).strip().lower()
    if result.get('sent') is not True or str(result.get('mailing_id'))!=str(payload['mailing_id']) or str(result.get('to_email','')).lower()!=email:
        raise ValueError('Unconfirmed recipient')
    if email not in [address(v) for v in result.get('accepted',[])] or email in [address(v) for v in result.get('rejected',[])]:
        raise ValueError('SMTP acceptance missing')
    return str(result.get('message_id') or '')[:500]


def pause(conn,run,reason):
    changed=conn.execute("UPDATE gab_crm_mailing_runs SET status='paused',error_message=%s,heartbeat_at=now() WHERE id=%s AND worker_token=%s RETURNING id",[reason,run['id'],run['worker_token']]).fetchone()
    if not changed:return
    conn.execute("UPDATE gab_crm_mailings SET status='failed',updated_at=now() WHERE id=%s",[run['mailing_id']])
    event(conn,run['mailing_id'],'bulk_paused',{'run_id':str(run['id']),'reason':reason})


def dispatch(run_id,token):
    # One session lock serializes campaigns across all API processes. A crash
    # releases it, but pending recipients still require an explicit resume.
    with get_conn() as guard:
        guard.autocommit=True
        if not guard.execute('SELECT pg_try_advisory_lock('+LOCK+') AS acquired').fetchone()['acquired']:
            with get_conn() as conn:
                run=conn.execute('SELECT * FROM gab_crm_mailing_runs WHERE id=%s',[run_id]).fetchone()
                if run and run['worker_token']==token:pause(conn,run,'Отправщик занят. Продолжите вручную')
            return
        try:
            while True:
                guard.execute('SELECT 1')
                with get_conn() as conn:
                    run=conn.execute('SELECT * FROM gab_crm_mailing_runs WHERE id=%s FOR UPDATE',[run_id]).fetchone()
                    if not run or run['worker_token']!=token or run['status']!='running':return
                    recipient=conn.execute("SELECT * FROM gab_crm_mailing_recipients WHERE bulk_run_id=%s AND delivery_state='pending' ORDER BY id LIMIT 1 FOR UPDATE",[run_id]).fetchone()
                    if not recipient:
                        conn.execute("UPDATE gab_crm_mailing_runs SET status='completed',finished_at=now(),heartbeat_at=now() WHERE id=%s",[run_id])
                        conn.execute("UPDATE gab_crm_mailings SET status=CASE WHEN EXISTS (SELECT 1 FROM gab_crm_mailing_recipients WHERE bulk_run_id=%s AND delivery_state='unknown') THEN 'failed' ELSE 'sent' END,updated_at=now() WHERE id=%s",[run_id,run['mailing_id']])
                        event(conn,run['mailing_id'],'bulk_finished',{'run_id':str(run_id)})
                        return
                    approved=next((r for r in run['payload']['recipients'] if r['id']==recipient['id']),None)
                    if not approved or approved['email']!=(recipient['email'] or '').strip().lower():
                        pause(conn,run,'Список адресов изменился после подтверждения');return
                    # Re-check suppression immediately before each new attempt.
                    suppressed=conn.execute("SELECT 1 FROM gab_crm_mailing_recipients WHERE lower(btrim(email))=lower(btrim(%s)) AND status='opted_out' LIMIT 1",[recipient['email']]).fetchone()
                    if suppressed:
                        conn.execute("UPDATE gab_crm_mailing_recipients SET delivery_state='excluded' WHERE id=%s",[recipient['id']]);continue
                    try:encoded=attachments_payload(run['payload']['attachments'],True)
                    except Exception:
                        pause(conn,run,'Вложения недоступны или изменены');return
                    conn.execute("UPDATE gab_crm_mailing_recipients SET delivery_state='sending',updated_at=now() WHERE id=%s",[recipient['id']])
                    conn.execute('UPDATE gab_crm_mailing_runs SET heartbeat_at=now() WHERE id=%s',[run_id])
                # Sending marker is committed BEFORE network I/O. Never replay it.
                try:
                    message_id=send_one({'request_id':str(recipient['delivery_request_id']),
                        'mailing_id':run['mailing_id'],'to_email':recipient['email'].strip().lower(),
                        'subject':run['payload']['subject'],'body_html':run['payload']['body_html'],
                        'body_text':run['payload']['body_text'],'attachments':encoded})
                except Exception as exc:
                    with get_conn() as conn:
                        conn.execute("UPDATE gab_crm_mailing_recipients SET delivery_state='unknown',status='failed',error_message=%s,updated_at=now() WHERE id=%s",['Не подтверждено: '+type(exc).__name__,recipient['id']])
                        pause(conn,run,'Результат письма не подтверждён. Автоматический повтор запрещён')
                    return
                with get_conn() as conn:
                    conn.execute("UPDATE gab_crm_mailing_recipients SET delivery_state='sent',status='sent',sent_at=now(),message_id=%s,error_message=NULL,updated_at=now() WHERE id=%s",[message_id or None,recipient['id']])
                    event(conn,run['mailing_id'],'bulk_smtp_accepted',{'request_id':str(recipient['delivery_request_id']),'message_id':message_id},recipient['id'])
                time.sleep(max(1,float(os.environ.get('GAB_MAILING_INTERVAL_SECONDS','3'))))
        finally:
            guard.execute('SELECT pg_advisory_unlock('+LOCK+')')


@router.get('/{mailing_id}/bulk-status')
def status(mailing_id:int):
    with get_conn() as conn:
        run=conn.execute('SELECT * FROM gab_crm_mailing_runs WHERE mailing_id=%s',[mailing_id]).fetchone()
        if not run:return {'started':False}
        rows=conn.execute('SELECT id,email,recipient_name,delivery_state,error_message,sent_at FROM gab_crm_mailing_recipients WHERE bulk_run_id=%s ORDER BY id',[run['id']]).fetchall()
    counts={state:sum(r['delivery_state']==state for r in rows) for state in ('pending','sending','sent','unknown','excluded')}
    return {'started':True,'run_id':run['id'],'status':run['status'],'counts':counts,
            'error':run['error_message'],'recipients':rows,'heartbeat_at':run['heartbeat_at']}


@router.post('/{mailing_id}/bulk-resume')
def resume(mailing_id:int,body:Resume,background:BackgroundTasks):
    with get_conn() as conn:
        capacity(conn)
        run=conn.execute('SELECT * FROM gab_crm_mailing_runs WHERE id=%s AND mailing_id=%s FOR UPDATE',[body.run_id,mailing_id]).fetchone()
        if not run:raise HTTPException(404,'Запуск не найден')
        if run['status']=='completed':return {'resumed':False,'completed':True}
        other=conn.execute("SELECT id FROM gab_crm_mailing_runs WHERE status='running' AND id<>%s LIMIT 1",[run['id']]).fetchone()
        if other:raise HTTPException(409,'Другая рассылка выполняется')
        # If an old process died, uncertain attempts are never changed to pending.
        conn.execute("UPDATE gab_crm_mailing_recipients SET delivery_state='unknown',status='failed',error_message='Процесс прервался; повтор запрещён' WHERE bulk_run_id=%s AND delivery_state='sending'",[run['id']])
        token=uuid.uuid4()
        conn.execute("UPDATE gab_crm_mailing_runs SET status='running',worker_token=%s,error_message=NULL,heartbeat_at=now() WHERE id=%s",[token,run['id']])
        conn.execute("UPDATE gab_crm_mailings SET status='sending',updated_at=now() WHERE id=%s",[mailing_id])
        event(conn,mailing_id,'bulk_manually_resumed',{'actor':os.environ.get('GAB_APP_USER'),'run_id':str(run['id'])})
    background.add_task(dispatch,run['id'],token)
    return {'resumed':True,'run_id':run['id']}


@router.post('/{mailing_id}/bulk-pause')
def pause_manually(mailing_id:int,body:Resume):
    with get_conn() as conn:
        run=conn.execute('SELECT * FROM gab_crm_mailing_runs WHERE id=%s AND mailing_id=%s FOR UPDATE',[body.run_id,mailing_id]).fetchone()
        if not run:raise HTTPException(404,'Запуск не найден')
        if run['status']=='completed':return {'paused':False,'completed':True}
        pause(conn,run,'Остановлено пользователем после текущего письма')
        event(conn,mailing_id,'bulk_manually_paused',{'actor':os.environ.get('GAB_APP_USER'),'run_id':str(run['id'])})
    return {'paused':True}
