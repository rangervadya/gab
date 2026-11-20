import os
import re
import json
import urllib.request
import urllib.error
import urllib.parse

from decimal import Decimal, InvalidOperation

from app.integration_config import api_key

import psycopg
from psycopg.rows import dict_row

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field


router = APIRouter(prefix="/registry-enrichment", tags=["registry-enrichment"])

DATABASE_URL = os.environ.get("DATABASE_URL", "")

DADATA_URL = (
    "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party"
)

EMAIL_RE = re.compile(
    r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
    re.IGNORECASE
)


class SearchRequest(BaseModel):
    record_ids: list[str] = Field(default_factory=list, max_length=10)


class CheckoRequest(BaseModel):
    inn: str = Field(min_length=10, max_length=12)


class ConfirmRequest(BaseModel):
    inn: str = Field(min_length=10, max_length=12)


def get_conn():
    if not DATABASE_URL:
        raise HTTPException(
            status_code=503,
            detail="DATABASE_URL is not configured"
        )
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def text_value(value):
    if value is None:
        return None

    if isinstance(value, dict):
        for key in (
            "Наим",
            "Наименование",
            "АдресПолн",
            "Адрес",
            "value",
            "ФИО"
        ):
            if value.get(key):
                return str(value[key]).strip()
        return json.dumps(value, ensure_ascii=False)

    if isinstance(value, list):
        parts = [text_value(x) for x in value]
        return "; ".join(x for x in parts if x)

    result = str(value).strip()
    return result or None


def clean_search_query(raw):
    query = str(raw or "").strip()

    query = re.split(
        r"(?i)\b(?:тел\.?|телефон|факс)\b",
        query,
        maxsplit=1
    )[0]

    query = re.sub(
        r"(?<!\w)\+?\d[\d\s()\-]{5,}\s*$",
        "",
        query
    )

    query = re.sub(r"\s+", " ", query).strip(" ,;.-")
    return query


def search_variants(query):
    result = [query]

    without_form = re.sub(
        r"(?i)^\s*(?:ИП|ООО|ОАО|ЗАО|АО|ПАО|НАО)\s+",
        "",
        query
    ).strip()

    if without_form and without_form != query:
        result.append(without_form)

    without_initials = re.sub(
        r"\s+[А-ЯЁ]\.?\s*(?:[А-ЯЁ]\.?)?\s*$",
        "",
        without_form,
        flags=re.IGNORECASE
    ).strip()

    if (
        without_initials
        and without_initials not in result
        and len(without_initials) >= 4
    ):
        result.append(without_initials)

    return result


def dadata_request(query):
    token = api_key("DADATA_API_KEY")
    if not token:
        raise HTTPException(
            status_code=503,
            detail="Ключ DaData не подключён"
        )

    payload = json.dumps({
        "query": query,
        "count": 5
    }).encode("utf-8")

    req = urllib.request.Request(
        DADATA_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": "Token " + token
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise HTTPException(
            status_code=502,
            detail=f"Ошибка DaData: HTTP {error.code}"
        )
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="Не удалось связаться с DaData"
        )


def checko_request(endpoint, inn):
    token = api_key("CHECKO_API_KEY")
    if not token:
        raise HTTPException(
            status_code=503,
            detail="Ключ Checko не подключён"
        )

    params = urllib.parse.urlencode({
        "key": token,
        "inn": inn
    })

    url = f"https://api.checko.ru/v2/{endpoint}?{params}"
    req = urllib.request.Request(url, method="GET")

    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise HTTPException(
            status_code=502,
            detail=f"Ошибка Checko: HTTP {error.code}"
        )
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="Не удалось связаться с Checko"
        )

    meta = result.get("meta") or {}
    if meta.get("status") != "ok":
        raise HTTPException(
            status_code=502,
            detail="Checko не подтвердил успешное выполнение запроса"
        )

    return result.get("data") or {}


def as_strings(value):
    if value is None:
        return []

    values = value if isinstance(value, list) else [value]
    result = []

    for item in values:
        text = text_value(item)
        if text:
            result.append(text)

    return list(dict.fromkeys(result))


def extract_emails(value):
    found = []

    for item in as_strings(value):
        found.extend(EMAIL_RE.findall(item))

    return list(dict.fromkeys(x.lower() for x in found))


def find_latest_revenue(inn):
    try:
        data = checko_request("finances", inn)
    except HTTPException:
        return None, None

    years = sorted(
        [str(x) for x in data.keys() if re.fullmatch(r"\d{4}", str(x))],
        reverse=True
    )

    for year in years:
        year_data = data.get(year)
        if not isinstance(year_data, dict):
            continue

        revenue = year_data.get("2110")

        if isinstance(revenue, dict):
            revenue = revenue.get("СумОтч")

        if revenue is None:
            continue

        try:
            value = Decimal(str(revenue).replace(",", "."))
        except (InvalidOperation, ValueError):
            continue

        return int(year), value

    return None, None


def save_dadata_candidate(conn, record_id, item):
    data = item.get("data") or {}

    inn = str(data.get("inn") or "").strip()
    if not inn.isdigit() or len(inn) not in (10, 12):
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

    legal_name = (
        name.get("full_with_opf")
        or item.get("value")
        or " ".join(filter(None, [
            fio.get("surname"),
            fio.get("name"),
            fio.get("patronymic")
        ]))
        or None
    )

    status = state.get("status")
    okved = data.get("okved")

    conn.execute(
        """
        INSERT INTO gab_review_enrichment (
            review_record_id, provider, entity_type, inn, ogrn,
            legal_name, status_code, status_name,
            region, legal_address, okved_code, director_name,
            phones, emails, match_status
        )
        VALUES (
            %s, 'dadata', %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, '[]'::jsonb, '[]'::jsonb, 'candidate'
        )
        ON CONFLICT (review_record_id, provider, inn)
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
            match_status = CASE
                WHEN gab_review_enrichment.match_status = 'confirmed'
                THEN 'confirmed'
                ELSE 'candidate'
            END,
            checked_at = now(),
            updated_at = now()
        WHERE
            gab_review_enrichment.status_code IS DISTINCT FROM 'ACTIVE'
            OR EXCLUDED.status_code = 'ACTIVE'
        """,
        [
            record_id,
            entity_type,
            inn,
            data.get("ogrn"),
            legal_name,
            status,
            status,
            address_data.get("region_with_type"),
            address.get("value"),
            okved,
            management.get("name")
        ]
    )

    return inn


def get_enrichment_row(conn, record_id, provider, inn):
    return conn.execute(
        """
        SELECT
            id, review_record_id, provider, entity_type, inn, ogrn,
            legal_name, status_code, status_name, region, legal_address,
            okved_code, okved_name, director_name, phones, emails,
            website, revenue_year, revenue_rub, match_status, checked_at
        FROM gab_review_enrichment
        WHERE review_record_id = %s
          AND provider = %s
          AND inn = %s
        """,
        [record_id, provider, inn]
    ).fetchone()


@router.post("/search")
def search_registry_companies(body: SearchRequest):
    record_ids = list(dict.fromkeys(
        str(x).strip() for x in body.record_ids if str(x).strip()
    ))

    if not record_ids:
        raise HTTPException(
            status_code=400,
            detail="Сначала выберите контрагентов в папке реестра"
        )

    if len(record_ids) > 10:
        raise HTTPException(
            status_code=400,
            detail="За один запрос можно обработать не более 10 групп"
        )

    result_items = []

    for record_id in record_ids:
        with get_conn() as conn:
            source = conn.execute(
                """
                SELECT
                    record_id,
                    review_state,
                    COALESCE(
                        NULLIF(BTRIM(payload->>'corrected_counterparty_contacts'), ''),
                        NULLIF(BTRIM(counterparty_contacts), ''),
                        NULLIF(BTRIM(payload->>'counterparty_contacts'), '')
                    ) AS counterparty
                FROM gab_review_records
                WHERE record_id = %s
                  AND review_state IN ('corrected', 'not_required')
                """,
                [record_id]
            ).fetchone()

        if not source:
            result_items.append({
                "record_id": record_id,
                "source_name": "",
                "candidates": [],
                "error": "Запись не найдена или не прошла проверку"
            })
            continue

        query = clean_search_query(source["counterparty"])

        if not query:
            result_items.append({
                "record_id": record_id,
                "source_name": "",
                "candidates": [],
                "error": "В записи нет названия контрагента"
            })
            continue

        suggestions = []

        for variant in search_variants(query):
            response = dadata_request(variant)
            suggestions = response.get("suggestions") or []
            if suggestions:
                break

        candidates = []
        seen_inns = set()

        with get_conn() as conn:
            for suggestion in suggestions:
                inn = save_dadata_candidate(
                    conn, record_id, suggestion
                )
                if not inn or inn in seen_inns:
                    continue

                seen_inns.add(inn)

                candidate = get_enrichment_row(
                    conn, record_id, "dadata", inn
                )

                details = get_enrichment_row(
                    conn, record_id, "checko", inn
                )

                candidates.append({
                    "candidate": candidate,
                    "checko": details
                })

        result_items.append({
            "record_id": record_id,
            "source_name": source["counterparty"],
            "search_query": query,
            "candidates": candidates,
            "no_matches": len(candidates) == 0
        })

    return {"items": result_items, "processed": len(result_items)}


@router.post("/{record_id}/checko")
def enrich_registry_candidate(record_id: str, body: CheckoRequest):
    inn = body.inn.strip()

    if not inn.isdigit() or len(inn) not in (10, 12):
        raise HTTPException(status_code=400, detail="Некорректный ИНН")

    with get_conn() as conn:
        candidate = conn.execute(
            """
            SELECT entity_type
            FROM gab_review_enrichment
            WHERE review_record_id = %s
              AND provider = 'dadata'
              AND inn = %s
            """,
            [record_id, inn]
        ).fetchone()

    if not candidate:
        raise HTTPException(
            status_code=404,
            detail="Сначала найдите этот ИНН через DaData"
        )

    entity_type = candidate["entity_type"]
    endpoint = "company" if entity_type == "company" else "entrepreneur"

    data = checko_request(endpoint, inn)

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
    legal_name = (
        data.get("НаимПолн")
        or data.get("НаимСокр")
        or data.get("ФИО")
        or data.get("ФИОИП")
    )

    status_code = (
        status.get("Код") if isinstance(status, dict) else None
    )
    status_name = text_value(status)

    okved_code = (
        okved.get("Код") if isinstance(okved, dict) else text_value(okved)
    )
    okved_name = (
        okved.get("Наим") if isinstance(okved, dict) else None
    )

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO gab_review_enrichment (
                review_record_id, provider, entity_type, inn, ogrn,
                legal_name, status_code, status_name, region,
                legal_address, okved_code, okved_name, director_name,
                phones, emails, website, revenue_year, revenue_rub,
                match_status
            )
            VALUES (
                %s, 'checko', %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s::jsonb, %s::jsonb, %s, %s, %s,
                'candidate'
            )
            ON CONFLICT (review_record_id, provider, inn)
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
                match_status = CASE
                    WHEN gab_review_enrichment.match_status = 'confirmed'
                    THEN 'confirmed'
                    ELSE 'candidate'
                END,
                checked_at = now(),
                updated_at = now()
            """,
            [
                record_id,
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
                revenue_rub
            ]
        )

        saved = get_enrichment_row(
            conn, record_id, "checko", inn
        )

    return {"item": saved}


@router.post("/{record_id}/confirm")
def confirm_registry_candidate(record_id: str, body: ConfirmRequest):
    inn = body.inn.strip()

    if not inn.isdigit() or len(inn) not in (10, 12):
        raise HTTPException(status_code=400, detail="Некорректный ИНН")

    with get_conn() as conn:
        details = conn.execute(
            """
            SELECT id
            FROM gab_review_enrichment
            WHERE review_record_id = %s
              AND provider = 'checko'
              AND inn = %s
            """,
            [record_id, inn]
        ).fetchone()

        if not details:
            raise HTTPException(
                status_code=409,
                detail="Сначала получите карточку Checko для этого ИНН"
            )

        conn.execute(
            """
            UPDATE gab_review_enrichment
            SET match_status = 'confirmed',
                updated_at = now()
            WHERE review_record_id = %s
              AND inn = %s
              AND provider IN ('dadata', 'checko')
            """,
            [record_id, inn]
        )

    return {
        "confirmed": True,
        "record_id": record_id,
        "inn": inn
    }
