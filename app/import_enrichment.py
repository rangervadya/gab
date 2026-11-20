import json
import re
import os
from datetime import datetime
from typing import Literal
from psycopg.types.json import Jsonb
from app.integration_config import configuration
from app.import_confirmation import _valid_inn

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.registry_enrichment import (
    get_conn,
    clean_search_query,
    search_variants,
    dadata_request,
    checko_request,
    text_value,
    as_strings,
    extract_emails,
    find_latest_revenue,
)

router = APIRouter(
    prefix="/registry-enrichment/imports",
    tags=["import-enrichment"],
)


class ImportInnRequest(BaseModel):
    inn: str = Field(min_length=10, max_length=12)
    provider:Literal['dadata','checko']='checko'
    version:int|None=None
    use_revenue:bool=False
    checked_at:datetime|None=None
    include_dadata:bool=False
    dadata_checked_at:datetime|None=None


@router.get('/configuration')
def provider_configuration():
    return configuration()


def get_import_row(import_row_id):
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT id, batch_id, company_name, full_name, inn, edit_version, import_status
            FROM gab_crm_import_rows
            WHERE id = %s
            """,
            [import_row_id],
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Строка импорта не найдена")
    if row['import_status'] not in ('pending','needs_review'):raise HTTPException(409,'Обогащать можно только неподтверждённые строки импорта')

    return row


def assert_source_current(conn,source):
    row=conn.execute('SELECT import_status,edit_version FROM gab_crm_import_rows WHERE id=%s FOR UPDATE',[source['id']]).fetchone()
    if not row or row['import_status'] not in ('pending','needs_review') or row['edit_version']!=source['edit_version']:
        raise HTTPException(409,'Строка изменилась во время запроса; повторите поиск')


def save_import_dadata_candidate(conn, import_row_id, item):
    data = item.get("data") or {}
    inn = str(data.get("inn") or "").strip()

    if not _valid_inn(inn):
        return None

    raw_type = data.get("type")
    entity_type = (
        "company" if raw_type == "LEGAL"
        else "entrepreneur" if raw_type == "INDIVIDUAL"
        else "company" if len(inn) == 10
        else "entrepreneur"
    )

    name = data.get("name") or {}
    fio = data.get("fio") or {}
    state = data.get("state") or {}
    address = data.get("address") or {}
    address_data = address.get("data") or {}
    management = data.get("management") or {}
    def contacts(key):
        values=data.get(key) or []
        if not isinstance(values,list):return []
        return [str(v.get('value') or v.get('unrestricted_value') or '').strip() if isinstance(v,dict) else str(v).strip() for v in values]
    emails=extract_emails(contacts('emails'))
    phones=[v for v in contacts('phones') if 7<=len(re.sub(r'\D','',v))<=15]

    legal_name = (
        name.get("full_with_opf")
        or item.get("value")
        or " ".join(filter(None, [
            fio.get("surname"),
            fio.get("name"),
            fio.get("patronymic"),
        ]))
        or None
    )

    conn.execute(
        """
        INSERT INTO gab_crm_import_enrichment (
            import_row_id, provider, entity_type, inn, ogrn,
            legal_name, status_code, status_name, region,
            legal_address, okved_code, director_name,
            phones, emails, match_status
        )
        VALUES (
            %s, 'dadata', %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s::jsonb, %s::jsonb, 'candidate'
        )
        ON CONFLICT (import_row_id, provider, inn)
        DO UPDATE SET
            entity_type = EXCLUDED.entity_type,
            ogrn = EXCLUDED.ogrn,
            legal_name = EXCLUDED.legal_name,
            status_code = EXCLUDED.status_code,
            status_name = EXCLUDED.status_name,
            region = EXCLUDED.region,
            legal_address = EXCLUDED.legal_address,
            okved_code = EXCLUDED.okved_code,
            director_name = EXCLUDED.director_name,
            phones = EXCLUDED.phones,
            emails = EXCLUDED.emails,
            match_status = 'candidate',
            checked_at = now(),
            updated_at = now()
        """,
        [
            import_row_id,
            entity_type,
            inn,
            data.get("ogrn"),
            legal_name,
            state.get("status"),
            state.get("status"),
            address_data.get("region_with_type"),
            address.get("value"),
            data.get("okved"),
            management.get("name"),
            json.dumps(phones),json.dumps(emails),
        ],
    )

    return inn


def get_import_enrichment_row(conn, import_row_id, provider, inn):
    return conn.execute(
        """
        SELECT *
        FROM gab_crm_import_enrichment
        WHERE import_row_id = %s
          AND provider = %s
          AND inn = %s
        """,
        [import_row_id, provider, inn],
    ).fetchone()


@router.post("/{import_row_id}/search")
def search_import_company(import_row_id: int):
    source = get_import_row(import_row_id)

    source_inn = re.sub(r"\D", "", str(source["inn"] or ""))
    if len(source_inn) in (10, 12):
        query = source_inn
        variants = [query]
    else:
        query = clean_search_query(source["company_name"] or source.get('full_name') or "")
        variants = search_variants(query) if query else []

    if not variants:
        raise HTTPException(
            status_code=400,
            detail="В импортированной строке нет названия компании или корректного ИНН",
        )

    suggestions = []
    for variant in variants:
        response = dadata_request(variant)
        if not isinstance(response,dict):raise HTTPException(502,'Некорректный ответ DaData')
        suggestions = response.get("suggestions") or []
        if not isinstance(suggestions,list) or any(not isinstance(s,dict) or not isinstance(s.get('data'),dict) for s in suggestions):raise HTTPException(502,'Некорректные кандидаты DaData')
        if suggestions:
            break

    candidates = []
    seen_inns = set()

    with get_conn() as conn:
        assert_source_current(conn,source)
        for suggestion in sorted(suggestions,key=lambda s:s['data'].get('branch_type')!='MAIN'):
            candidate_inn=str(suggestion['data'].get('inn') or '').strip()
            if candidate_inn in seen_inns:continue
            inn = save_import_dadata_candidate(conn, import_row_id, suggestion)
            if not inn or inn in seen_inns:
                continue

            seen_inns.add(inn)
            candidates.append({
                "candidate": get_import_enrichment_row(
                    conn, import_row_id, "dadata", inn
                ),
                "checko": get_import_enrichment_row(
                    conn, import_row_id, "checko", inn
                ),
                "source_inn_match": not source_inn or source_inn == inn,
            })

    return {
        "import_row_id": import_row_id,
        "source_name": source["company_name"] or source.get('full_name'),
        "source_inn": source["inn"],
        "version":source['edit_version'],
        "search_query": query,
        "candidates": candidates,
        "no_matches": len(candidates) == 0,
    }


@router.post("/{import_row_id}/checko")
def enrich_import_company(import_row_id: int, body: ImportInnRequest):
    source=get_import_row(import_row_id)
    inn = body.inn.strip()

    if not _valid_inn(inn):
        raise HTTPException(status_code=400, detail="Некорректный ИНН")

    with get_conn() as conn:
        candidate = conn.execute(
            """
            SELECT entity_type
            FROM gab_crm_import_enrichment
            WHERE import_row_id = %s
              AND provider = 'dadata'
              AND inn = %s
            """,
            [import_row_id, inn],
        ).fetchone()

    if not candidate:
        raise HTTPException(
            status_code=404,
            detail="Сначала найдите этот ИНН через DaData",
        )

    entity_type = candidate["entity_type"]
    endpoint = "company" if entity_type == "company" else "entrepreneur"
    data = checko_request(endpoint, inn)
    if not isinstance(data,dict):raise HTTPException(502,'Некорректная карточка Checko')
    returned_inn=str(data.get('ИНН') or '').strip()
    if returned_inn and returned_inn!=inn:raise HTTPException(502,'Checko вернул карточку другого ИНН')

    contacts = data.get("Контакты") or {}
    if not isinstance(contacts, dict):
        contacts = {}

    status = data.get("Статус") or {}
    okved = data.get("ОКВЭД") or {}

    phones = as_strings(contacts.get("Тел"))
    emails = extract_emails(contacts.get("Емэйл"))
    website = text_value(contacts.get("ВебСайт"))

    directors = data.get("Руковод") or []
    if isinstance(directors, list):
        director_name = (
            text_value(directors[0].get("ФИО"))
            if directors and isinstance(directors[0], dict)
            else None
        )
    else:
        director_name = text_value(directors)

    revenue_year = None
    revenue_rub = None
    if entity_type == "company":
        revenue_year, revenue_rub = find_latest_revenue(inn)

    ogrn = data.get("ОГРН") or data.get("ОГРНИП")
    legal_name = text_value(
        data.get("НаимПолн")
        or data.get("НаимСокр")
        or data.get("ФИО")
        or data.get("ФИОИП")
    )

    status_code = status.get("Код") if isinstance(status, dict) else None
    status_name = text_value(status)
    okved_code = (
        okved.get("Код") if isinstance(okved, dict) else text_value(okved)
    )
    okved_name = okved.get("Наим") if isinstance(okved, dict) else None

    with get_conn() as conn:
        assert_source_current(conn,source)
        conn.execute(
            """
            INSERT INTO gab_crm_import_enrichment (
                import_row_id, provider, entity_type, inn, ogrn,
                legal_name, status_code, status_name, region,
                legal_address, okved_code, okved_name, director_name,
                phones, emails, website, revenue_year, revenue_rub,
                match_status
            )
            VALUES (
                %s, 'checko', %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s::jsonb, %s::jsonb, %s, %s, %s, 'candidate'
            )
            ON CONFLICT (import_row_id, provider, inn)
            DO UPDATE SET
                entity_type = EXCLUDED.entity_type,
                ogrn = EXCLUDED.ogrn,
                legal_name = EXCLUDED.legal_name,
                status_code = EXCLUDED.status_code,
                status_name = EXCLUDED.status_name,
                region = EXCLUDED.region,
                legal_address = EXCLUDED.legal_address,
                okved_code = EXCLUDED.okved_code,
                okved_name = EXCLUDED.okved_name,
                director_name = EXCLUDED.director_name,
                phones = EXCLUDED.phones,
                emails = EXCLUDED.emails,
                website = EXCLUDED.website,
                revenue_year = EXCLUDED.revenue_year,
                revenue_rub = EXCLUDED.revenue_rub,
                match_status = 'candidate',
                checked_at = now(),
                updated_at = now()
            """,
            [
                import_row_id,
                entity_type,
                inn,
                ogrn,
                legal_name,
                status_code,
                status_name,
                text_value(data.get("Регион")),
                text_value(data.get("ЮрАдрес")),
                okved_code,
                okved_name,
                director_name,
                json.dumps(phones, ensure_ascii=False),
                json.dumps(emails, ensure_ascii=False),
                website,
                revenue_year,
                revenue_rub,
            ],
        )

        saved = get_import_enrichment_row(
            conn, import_row_id, "checko", inn
        )

    return {"item": saved}


@router.post("/{import_row_id}/confirm")
def confirm_import_company(import_row_id: int, body: ImportInnRequest):
    inn = body.inn.strip()

    if not _valid_inn(inn):
        raise HTTPException(status_code=400, detail="Некорректный ИНН")

    with get_conn() as conn:
        details = conn.execute(
            """
            SELECT e.*, r.inn AS source_inn,r.edit_version AS source_version,
                r.manual_overrides,r.revenue_rub AS row_revenue,r.import_status,r.batch_id
            FROM gab_crm_import_enrichment e
            JOIN gab_crm_import_rows r ON r.id = e.import_row_id
            WHERE e.import_row_id = %s
              AND e.provider = %s
              AND e.inn = %s
            FOR UPDATE OF r
            """,
            [import_row_id, body.provider,inn],
        ).fetchone()

        if not details:
            raise HTTPException(
                status_code=409,
                detail="Сначала получите выбранную карточку реестра",
            )
        if details['import_status'] not in ('pending','needs_review'):raise HTTPException(409,'Строка уже перенесена или отклонена')
        if body.version is not None and details['source_version']!=body.version:raise HTTPException(409,'Строка изменилась; повторите поиск и проверку')
        if body.checked_at is not None and details['checked_at']!=body.checked_at:raise HTTPException(409,'Карточка реестра обновилась; проверьте её заново')
        if body.include_dadata:
            extra=get_import_enrichment_row(conn,import_row_id,'dadata',inn)
            if not extra or body.dadata_checked_at is None or extra['checked_at']!=body.dadata_checked_at:
                raise HTTPException(409,'Данные DaData обновились; проверьте контакты заново')

        source_inn = re.sub(r"\D", "", str(details["source_inn"] or ""))
        if len(source_inn) in (10, 12) and source_inn != inn:
            raise HTTPException(
                status_code=409,
                detail=(
                    "ИНН из файла отличается от выбранного ИНН. "
                    "Проверьте исходную строку перед подтверждением."
                ),
            )

        conn.execute(
            """
            UPDATE gab_crm_import_enrichment
            SET match_status = CASE
                    WHEN inn <> %s THEN 'rejected'
                    WHEN provider=%s OR (provider='dadata' AND %s) THEN 'confirmed'
                    ELSE match_status
                END,
                updated_at = now()
            WHERE import_row_id = %s
              AND provider IN ('dadata', 'checko')
            """,
            [inn,body.provider,body.include_dadata,import_row_id],
        )

        revenue = details["revenue_rub"]
        year = details["revenue_year"]
        manual=details.get('manual_overrides') or {}
        use_revenue=(details['row_revenue'] is None or body.use_revenue) and not any(k in manual for k in ('revenue_rub','revenue_year','revenue_source'))
        if not use_revenue:revenue=None;year=None

        updated = conn.execute(
            """
            UPDATE gab_crm_import_rows
            SET inn = %s,
                company_name=CASE WHEN NULLIF(BTRIM(company_name),'') IS NULL
                    AND NOT (manual_overrides ? 'company_name') THEN %s ELSE company_name END,
                field_sources=field_sources || jsonb_build_object('inn',%s::text)
                    || CASE WHEN NULLIF(BTRIM(company_name),'') IS NULL AND %s::text IS NOT NULL
                        AND NOT (manual_overrides ? 'company_name')
                        THEN jsonb_build_object('company_name',%s::text) ELSE '{}'::jsonb END,
                revenue_rub = CASE
                    WHEN %s::numeric IS NOT NULL
                    THEN %s ELSE revenue_rub
                END,
                revenue_year = CASE
                    WHEN %s::numeric IS NOT NULL
                    THEN %s ELSE revenue_year
                END,
                revenue_source = CASE
                    WHEN %s::numeric IS NOT NULL
                    THEN 'checko' ELSE revenue_source
                END,
                revenue_status = CASE
                    WHEN %s::numeric IS NOT NULL
                    THEN CASE
                        WHEN %s >= 100000000 THEN 'eligible'
                        ELSE 'below_threshold'
                    END
                    ELSE revenue_status
                END,
                edit_version=edit_version+1,updated_at = now()
            WHERE id = %s
            RETURNING id, inn, revenue_rub, revenue_year,
                      revenue_source, revenue_status,edit_version
            """,
            [
                inn,details['legal_name'],body.provider,details['legal_name'],body.provider,
                revenue, revenue,
                revenue, year,
                revenue,
                revenue, revenue,
                import_row_id,
            ],
        ).fetchone()
        conn.execute("INSERT INTO gab_crm_import_batch_events(batch_id,event_type,actor,detail_json) VALUES (%s,'registry_confirmed',%s,%s)",
                     [details['batch_id'],os.environ.get('GAB_APP_USER'),Jsonb({'row_id':import_row_id,'inn':inn,'provider':body.provider,'include_dadata':body.include_dadata,'use_revenue':use_revenue})])

    return {
        "confirmed": True,
        "import_row": updated,
    }
