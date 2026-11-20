"""User cancellation and capability-scoped OCR/LLM page progress."""
from datetime import datetime,timezone
import hmac
import json
import math
import os
from typing import Literal
from fastapi import APIRouter,Header,HTTPException
from pydantic import BaseModel,Field
from psycopg.types.json import Jsonb
from app.registry_enrichment import get_conn

router=APIRouter(prefix='/api/mailings/imports',tags=['import-progress'])


class FragmentProgress(BaseModel):
    page:int=Field(ge=1,le=200)
    completed:int=Field(ge=0,le=32)
    total:int=Field(ge=1,le=32)


class PageProgress(BaseModel):
    stage:Literal['ocr','extract','llm']
    total:int=Field(ge=1,le=200)
    page_completed:int|None=Field(default=None,ge=1,le=200)
    fragment:FragmentProgress|None=None


class PageResult(BaseModel):
    source_page:int=Field(ge=1,le=200)
    request_hash:str=Field(pattern=r'^[a-f0-9]{64}$')
    result:dict|None=None
    scope:Literal['page','fragment']='page'


@router.post('/{batch_id}/page-result')
def page_result(batch_id:int,body:PageResult,x_gab_worker_token:str=Header(min_length=32,max_length=128)):
    # The batch row lock fences cancellation/retry against both cache reads and
    # writes. Results survive retry, but are reusable only for identical input.
    with get_conn() as conn:
        row=conn.execute('SELECT status,worker_token FROM gab_crm_import_batches WHERE id=%s FOR UPDATE',[batch_id]).fetchone()
        if not row or row['status']!='processing' or not hmac.compare_digest(str(row.get('worker_token') or ''),x_gab_worker_token):
            raise HTTPException(409,'Обработка остановлена или token устарел')
        if body.result is not None:
            if len(json.dumps(body.result).encode())>5*1024*1024:
                raise HTTPException(413,'Ответ страницы слишком большой')
            try:
                content=body.result['message']['content']
                parsed=json.loads(content)
                if body.result.get('done') is not True or body.result.get('done_reason')=='length' or not isinstance(parsed.get('records'),list) or any(not isinstance(record,dict) for record in parsed['records']):
                    raise ValueError()
            except (KeyError,TypeError,ValueError,AttributeError):
                raise HTTPException(422,'Страница не содержит завершённый JSON records') from None
            table='gab_crm_import_page_fragments' if body.scope=='fragment' else 'gab_crm_import_page_results'
            conflict='batch_id,source_page,request_hash' if body.scope=='fragment' else 'batch_id,source_page'
            conn.execute(f'''INSERT INTO {table}(batch_id,source_page,request_hash,result_json)
                VALUES (%s,%s,%s,%s) ON CONFLICT({conflict}) DO UPDATE SET
                request_hash=excluded.request_hash,result_json=excluded.result_json,created_at=now()''',
                [batch_id,body.source_page,body.request_hash,Jsonb(body.result)])
        table='gab_crm_import_page_fragments' if body.scope=='fragment' else 'gab_crm_import_page_results'
        cached=conn.execute(f'SELECT result_json FROM {table} WHERE batch_id=%s AND source_page=%s AND request_hash=%s',
                           [batch_id,body.source_page,body.request_hash]).fetchone()
    return {'result':cached['result_json'] if cached else None}


def progress_view(row):
    now=datetime.now(timezone.utc)
    start=row.get('processing_started_at')
    finish=row.get('processing_finished_at') or now
    elapsed=max(0,int((finish-start).total_seconds())) if start else None
    stage_start=row.get('stage_started_at')
    total=row.get('progress_total')
    completed=len(set(row.get('progress_pages') or []))
    eta=None
    # Only an estimate for the CURRENT measured stage, never an invented whole
    # document ETA. Two completed pages are the minimum useful sample.
    if row['status']=='processing' and stage_start and total and 2<=completed<total and not (row.get('progress_stage')=='llm' and row.get('progress_detail')):
        stage_elapsed=max(0,(now-stage_start).total_seconds())
        eta=max(1,math.ceil(stage_elapsed/completed*(total-completed)))
    cancelled=bool(row.get('cancelled_at')) or str(row.get('error_message') or '').startswith('Остановлено пользователем')
    fragment=row.get('progress_detail') or None
    if fragment and fragment.get('page') in (row.get('progress_pages') or []):fragment=None
    return {'batch_id':row['id'],'status':row['status'],'stage':row.get('progress_stage'),
            'completed':completed,'total':total,'elapsed_seconds':elapsed,
            'stage_eta_seconds':eta,'percent':round(completed/total*100) if total else None,
            'last_update':row.get('progress_updated_at'),'cancelled':cancelled,
            'can_cancel':row['status'] in ('uploaded','processing'),
            'error':row.get('error_message'),'execution_id':row.get('n8n_execution_id'),'fragment':fragment}


@router.get('/{batch_id}/progress')
def get_progress(batch_id:int):
    with get_conn() as conn:
        row=conn.execute('SELECT * FROM gab_crm_import_batches WHERE id=%s',[batch_id]).fetchone()
    if not row:raise HTTPException(404,'Документ не найден')
    return progress_view(row)


@router.post('/{batch_id}/cancel')
def cancel(batch_id:int):
    with get_conn() as conn:
        row=conn.execute('SELECT * FROM gab_crm_import_batches WHERE id=%s FOR UPDATE',[batch_id]).fetchone()
        if not row:raise HTTPException(404,'Документ не найден')
        if row.get('cancelled_at'):return {'cancelled':True,'already_cancelled':True,'item':progress_view(row)}
        if row['status'] not in ('uploaded','processing'):
            raise HTTPException(409,'Документ уже завершён или остановлен; результат не изменён')
        actor=os.environ.get('GAB_APP_USER')
        if not actor:raise HTTPException(503,'Пользователь аудита не настроен')
        row=conn.execute("UPDATE gab_crm_import_batches SET status='failed',worker_token=NULL,cancelled_at=now(),processing_finished_at=now(),progress_stage='cancelled',error_message='Остановлено пользователем',updated_at=now() WHERE id=%s RETURNING *",[batch_id]).fetchone()
        conn.execute("INSERT INTO gab_crm_import_batch_events(batch_id,event_type,actor,detail_json) VALUES (%s,'cancelled',%s,%s)",[batch_id,actor,Jsonb({'execution_id':row.get('n8n_execution_id')})])
    return {'cancelled':True,'already_cancelled':False,'item':progress_view(row),
            'message':'Приём результатов остановлен. Текущий внешний запрос может ещё завершаться.'}


@router.post('/{batch_id}/progress')
def report_progress(batch_id:int,body:PageProgress,x_gab_worker_token:str=Header(min_length=32,max_length=128)):
    with get_conn() as conn:
        row=conn.execute('SELECT * FROM gab_crm_import_batches WHERE id=%s FOR UPDATE',[batch_id]).fetchone()
        if not row or row['status']!='processing' or not hmac.compare_digest(str(row.get('worker_token') or ''),x_gab_worker_token):
            raise HTTPException(409,'Обработка остановлена или token устарел')
        if body.page_completed and body.page_completed>body.total:raise HTTPException(422,'Номер страницы превышает число страниц')
        if body.fragment and (body.stage!='llm' or body.fragment.page>body.total or body.fragment.completed>body.fragment.total):
            raise HTTPException(422,'Некорректный прогресс фрагментов')
        changed=row.get('progress_stage')!=body.stage
        if row.get('progress_stage')=='llm' and body.stage!='llm':raise HTTPException(409,'Этап уже завершён')
        pages=set() if changed else set(row.get('progress_pages') or [])
        if not changed and row.get('progress_total') not in (None,body.total):raise HTTPException(409,'Число страниц изменилось')
        if body.page_completed:pages.add(body.page_completed)
        detail=body.fragment.model_dump() if body.fragment else ({} if changed else row.get('progress_detail') or {})
        updated=conn.execute('''UPDATE gab_crm_import_batches SET progress_stage=%s,progress_total=%s,progress_pages=%s,
            stage_started_at=CASE WHEN %s OR stage_started_at IS NULL THEN now() ELSE stage_started_at END,
            progress_detail=%s,progress_updated_at=now(),updated_at=now() WHERE id=%s RETURNING *''',
            [body.stage,body.total,Jsonb(sorted(pages)),changed,Jsonb(detail),batch_id]).fetchone()
    return progress_view(updated)
