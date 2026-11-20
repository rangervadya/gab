import hashlib
import json
import mimetypes
import os
import re
import uuid
from decimal import Decimal, InvalidOperation

from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from app.registry_enrichment import get_conn

router = APIRouter(prefix="/imports", tags=["investor-imports"])

STORAGE_DIR = Path(
    os.environ.get(
        "GAB_IMPORT_STORAGE",
        "/srv/gab-review-data/investor-imports",
    )
)

from app.import_files import MIME_TYPES, validate_file

ALLOWED_EXTENSIONS = set(MIME_TYPES)
MAX_FILE_SIZE = 20 * 1024 * 1024


def safe_folder_name(value):
    value = re.sub(r"[\x00-\x1f\x7f/\\]+", " ", value or "")
    value = re.sub(r"\s+", " ", value).strip(" ._-")
    return value[:180] or "Новая база инвесторов"


def get_batch(batch_id):
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT b.*,
                   (SELECT COUNT(*)::int
                    FROM gab_crm_import_rows r
                    WHERE r.batch_id = b.id) AS actual_rows
            FROM gab_crm_import_batches b
            WHERE b.id = %s
            """,
            [batch_id],
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Папка импорта не найдена")

    return {k: v for k, v in row.items() if k != "worker_token"}


@router.post("")
async def upload_import_file(
    file: UploadFile = File(...),
    folder_name: str = Form(default=""),
):
    original = (
        (file.filename or "investors")
        .replace("\\", "/")
        .split("/")[-1]
    )

    extension = Path(original).suffix.lower().lstrip(".")

    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail="Неподдерживаемый формат документа",
        )

    content = await file.read(MAX_FILE_SIZE + 1)

    if not content:
        raise HTTPException(status_code=400, detail="Файл пуст")

    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail="Максимальный размер файла — 20 МБ",
        )

    default_folder = Path(original).stem
    folder = safe_folder_name(folder_name or default_folder)

    storage_name = uuid.uuid4().hex + "." + extension
    mime_type = (
        file.content_type
        or mimetypes.guess_type(original)[0]
        or "application/octet-stream"
    )

    try:
        validate_file(extension, content, mime_type)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    target = None

    try:
        with get_conn() as conn:
            batch = conn.execute(
                """
                INSERT INTO gab_crm_import_batches (
                    folder_name,
                    original_filename,
                    storage_filename,
                    file_extension,
                    mime_type,
                    file_size_bytes,
                    sha256,
                    status
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'uploaded')
                RETURNING *
                """,
                [
                    folder,
                    original,
                    storage_name,
                    extension,
                    mime_type,
                    len(content),
                    hashlib.sha256(content).hexdigest(),
                ],
            ).fetchone()

            target_dir = STORAGE_DIR / str(batch["id"])
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / storage_name
            target.write_bytes(content)

        return {
            "item": batch,
            "message": "Файл загружен, папка создана",
        }

    except Exception:
        if target:
            target.unlink(missing_ok=True)
        raise


@router.get("")
def list_import_batches(limit: int = 100, status: str = ""):
    limit = max(1, min(limit, 500))
    allowed = {"uploaded", "processing", "review", "ready", "completed", "failed"}
    if status and status not in allowed:
        raise HTTPException(status_code=400, detail="Unknown batch status")

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT b.*,
                   (SELECT COUNT(*)::int
                    FROM gab_crm_import_rows r
                    WHERE r.batch_id = b.id) AS actual_rows
            FROM gab_crm_import_batches b
            WHERE (%s = '' OR b.status = %s)
            ORDER BY b.created_at ASC, b.id ASC
            LIMIT %s
            """,
            [status, status, limit],
        ).fetchall()

    return {"items": [{k: v for k, v in row.items() if k != "worker_token"} for row in rows], "total": len(rows)}


@router.get("/{batch_id}")
def get_import_batch(batch_id: int):
    return get_batch(batch_id)


@router.post("/{batch_id}/claim")
def claim_import_batch(batch_id: int, body: dict | None = None):
    execution_id = str((body or {}).get("execution_id") or "")
    if execution_id and not re.fullmatch(r"[0-9]{1,30}",execution_id):
        raise HTTPException(422,"Некорректный execution ID")
    with get_conn() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('gab-import-capacity', 0))")
        busy = conn.execute("SELECT COUNT(*) AS count FROM gab_crm_import_batches WHERE status='processing'").fetchone()['count']
        if busy >= max(1, int(os.environ.get('GAB_IMPORT_CONCURRENCY', '1'))):
            return {'claimed': False, 'batch_id': batch_id, 'reason': 'capacity_limit'}
        row = conn.execute(
            """
            UPDATE gab_crm_import_batches
            SET status = 'processing',
                worker_token = %s,
                n8n_execution_id = %s, processing_started_at=now(), processing_finished_at=NULL,
                progress_stage='extract',progress_detail='{}'::jsonb,stage_started_at=now(),progress_total=NULL,progress_pages='[]'::jsonb,
                progress_updated_at=now(),cancelled_at=NULL,
                error_message = NULL,
                updated_at = now()
            WHERE id = %s AND status = 'uploaded'
            RETURNING *
            """,
            [uuid.uuid4().hex, execution_id or None, batch_id],
        ).fetchone()

        if row:
            return {"claimed": True, "item": row}

        current = conn.execute(
            "SELECT id, status FROM gab_crm_import_batches WHERE id = %s",
            [batch_id],
        ).fetchone()

    if not current:
        raise HTTPException(status_code=404, detail="Папка импорта не найдена")

    return {
        "claimed": False,
        "batch_id": batch_id,
        "status": current["status"],
    }


@router.post("/{batch_id}/fail")
def fail_import_batch(batch_id: int, body: dict):
    message = import_clean(body.get("error") or body.get("message"), 1500)
    if not message:
        message = "Ошибка обработки файла"

    with get_conn() as conn:
        row = conn.execute(
            """
            UPDATE gab_crm_import_batches
            SET status = 'failed',
                progress_stage='failed',processing_finished_at=now(),
                error_message = %s,
                updated_at = now()
            WHERE id = %s AND status = 'processing' AND worker_token = %s
            RETURNING id, status, error_message
            """,
            [message, batch_id, body.get("worker_token")],
        ).fetchone()

        if not row:
            exists = conn.execute(
                "SELECT id, status FROM gab_crm_import_batches WHERE id = %s",
                [batch_id],
            ).fetchone()

            if not exists:
                raise HTTPException(
                    status_code=404,
                    detail="Папка импорта не найдена",
                )
            return {
                "updated": False,
                "batch_id": batch_id,
                "status": exists["status"],
            }

    return {"updated": True, "item": row}


@router.get("/{batch_id}/file")
def download_import_file(batch_id: int):
    batch = get_batch(batch_id)

    target = (
        STORAGE_DIR
        / str(batch["id"])
        / batch["storage_filename"]
    )

    if not target.is_file():
        raise HTTPException(status_code=404, detail="Исходный файл не найден")

    return FileResponse(
        target,
        media_type=batch["mime_type"],
        filename=batch["original_filename"],
    )


@router.get("/{batch_id}/rows")
def list_import_rows(
    batch_id: int,
    q: str = "",
    segment: str = "all",
    limit: int = 200,
    offset: int = 0,
):
    allowed_segments = {
        "all",
        "eligible_email",
        "eligible_phone",
        "below_threshold",
        "unknown",
    }

    if segment not in allowed_segments:
        raise HTTPException(status_code=400, detail="Неизвестный фильтр")

    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    pattern = "%" + q.strip() + "%"

    with get_conn() as conn:
        rows = conn.execute(
            """
            WITH enriched AS (
                SELECT
                    r.*,
                    e.legal_name AS enriched_name,
                    e.emails AS enriched_emails,
                    e.phones AS enriched_phones,
                    COALESCE(
                        (
                            SELECT jsonb_agg(d.value ORDER BY d.first_ordinal)
                            FROM (
                                SELECT
                                    LOWER(BTRIM(contact.value)) AS value,
                                    MIN(contact.ordinality) AS first_ordinal
                                FROM jsonb_array_elements_text(
                                    COALESCE(r.emails, '[]'::jsonb)
                                    || COALESCE(e.emails, '[]'::jsonb)
                                    || CASE
                                        WHEN NULLIF(BTRIM(r.email), '') IS NOT NULL
                                        THEN jsonb_build_array(LOWER(BTRIM(r.email)))
                                        ELSE '[]'::jsonb
                                    END
                                ) WITH ORDINALITY AS contact(value, ordinality)
                                WHERE NULLIF(BTRIM(contact.value), '') IS NOT NULL
                                GROUP BY LOWER(BTRIM(contact.value))
                            ) AS d
                        ),
                        '[]'::jsonb
                    ) AS effective_emails,
                    COALESCE(
                        (
                            SELECT jsonb_agg(d.value ORDER BY d.first_ordinal)
                            FROM (
                                SELECT
                                    BTRIM(contact.value) AS value,
                                    MIN(contact.ordinality) AS first_ordinal
                                FROM jsonb_array_elements_text(
                                    COALESCE(r.phones, '[]'::jsonb)
                                    || COALESCE(e.phones, '[]'::jsonb)
                                    || CASE
                                        WHEN NULLIF(BTRIM(r.phone), '') IS NOT NULL
                                        THEN jsonb_build_array(BTRIM(r.phone))
                                        ELSE '[]'::jsonb
                                    END
                                ) WITH ORDINALITY AS contact(value, ordinality)
                                WHERE NULLIF(BTRIM(contact.value), '') IS NOT NULL
                                GROUP BY BTRIM(contact.value)
                            ) AS d
                        ),
                        '[]'::jsonb
                    ) AS effective_phones,
                    COALESCE(
                        NULLIF(BTRIM(r.email), ''),
                        NULLIF(BTRIM(r.emails->>0), ''),
                        NULLIF(BTRIM(e.emails->>0), '')
                    ) AS effective_email,
                    COALESCE(
                        NULLIF(BTRIM(r.phone), ''),
                        NULLIF(BTRIM(r.phones->>0), ''),
                        NULLIF(BTRIM(e.phones->>0), '')
                    ) AS effective_phone,
                    CASE
                        WHEN NULLIF(BTRIM(r.email), '') IS NOT NULL
                          OR jsonb_array_length(COALESCE(r.emails, '[]'::jsonb)) > 0
                            THEN 'file'
                        WHEN jsonb_array_length(COALESCE(e.emails, '[]'::jsonb)) > 0
                            THEN 'checko'
                        ELSE NULL
                    END AS email_source,
                    CASE
                        WHEN NULLIF(BTRIM(r.phone), '') IS NOT NULL
                          OR jsonb_array_length(COALESCE(r.phones, '[]'::jsonb)) > 0
                            THEN 'file'
                        WHEN jsonb_array_length(COALESCE(e.phones, '[]'::jsonb)) > 0
                            THEN 'checko'
                        ELSE NULL
                    END AS phone_source
                FROM gab_crm_import_rows r
                LEFT JOIN LATERAL (
                    SELECT x.legal_name, x.emails, x.phones
                    FROM gab_crm_import_enrichment x
                    WHERE x.import_row_id = r.id
                      AND x.provider = 'checko'
                      AND x.match_status = 'confirmed'
                    ORDER BY x.checked_at DESC
                    LIMIT 1
                ) e ON TRUE
                WHERE r.batch_id = %s
            ),
            filtered AS (
                SELECT *
                FROM enriched
                WHERE (
                    %s = ''
                    OR COALESCE(company_name, '') ILIKE %s
                    OR COALESCE(inn, '') ILIKE %s
                    OR COALESCE(full_name, '') ILIKE %s
                    OR COALESCE(effective_email, '') ILIKE %s
                    OR COALESCE(effective_phone, '') ILIKE %s
                    OR effective_emails::text ILIKE %s
                    OR effective_phones::text ILIKE %s
                )
                AND (
                    %s = 'all'
                    OR (
                        %s = 'eligible_email'
                        AND revenue_status = 'eligible'
                        AND effective_email IS NOT NULL
                    )
                    OR (
                        %s = 'eligible_phone'
                        AND revenue_status = 'eligible'
                        AND effective_email IS NULL
                        AND effective_phone IS NOT NULL
                    )
                    OR (
                        %s = 'below_threshold'
                        AND revenue_status = 'below_threshold'
                    )
                    OR (
                        %s = 'unknown'
                        AND revenue_status IN ('unknown', 'needs_review')
                    )
                )
            )
            SELECT filtered.*,
                   COUNT(*) OVER()::int AS total_count
            FROM filtered
            ORDER BY company_name NULLS LAST, id
            LIMIT %s OFFSET %s
            """,
            [
                batch_id,
                q.strip(),
                pattern, pattern, pattern, pattern, pattern, pattern, pattern,
                segment, segment, segment, segment, segment,
                limit, offset,
            ],
        ).fetchall()

    total = rows[0]["total_count"] if rows else 0

    return {
        "items": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
        "segment": segment,
    }


# ---------- Batch ingestion from n8n ----------

IMPORT_EMAIL_RE = re.compile(
    r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
    re.IGNORECASE,
)
IMPORT_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?\d[\d\s().-]{5,}\d)(?!\d)"
)

EMAIL_KEYS = {
    "email", "emails", "emailaddress", "emailaddresses",
    "contactemail", "contactemails", "mail",
    "почта", "электроннаяпочта", "адресэлектроннойпочты",
}
PHONE_KEYS = {
    "phone", "phones", "telephone", "tel", "mobile",
    "mobilephone", "contactphone", "contactphones",
    "phonenumber", "workphone", "generalphone", "whatsapp",
    "телефон", "телефоны", "мобильныйтелефон",
    "контактныйтелефон", "рабочийтелефон", "номер",
}
CONTACT_CONTAINER_KEYS = {
    "contacts", "contact", "contactdetails", "contactinfo",
    "contactinformation", "контакты", "контактныеданные",
    "контактнаяинформация",
}


def import_clean(value, limit=2000):
    if value is None:
        return None
    value = re.sub(r"\s+", " ", str(value)).strip()
    if not value or value.lower() in {"null", "none", "undefined", "nan"}:
        return None
    return value[:limit]



def import_alias(record, aliases):
    normalized_aliases = {
        re.sub(r"[^0-9a-zа-яё]", "", x.lower())
        for x in aliases
    }
    for key, value in record.items():
        normalized = re.sub(
            r"[^0-9a-zа-яё]", "", str(key).lower()
        )
        if normalized in normalized_aliases and value not in (None, ""):
            return value
    return None


def import_flatten(value):
    if isinstance(value, dict):
        result = []
        for item in value.values():
            result.extend(import_flatten(item))
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(import_flatten(item))
        return result
    if isinstance(value, str):
        return [value]
    return []


def collect_import_contacts(record):
    emails = []
    phones = []

    def walk(value, context=""):
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = re.sub(
                    r"[^0-9a-zа-яё]", "", str(key).lower()
                )
                next_context = (
                    normalized
                    if normalized in EMAIL_KEYS
                    | PHONE_KEYS
                    | CONTACT_CONTAINER_KEYS
                    else context
                )
                walk(child, next_context)

        elif isinstance(value, list):
            for child in value:
                walk(child, context)

        elif isinstance(value, str):
            # Email addresses are detected in every textual field.
            emails.extend(IMPORT_EMAIL_RE.findall(value))

            # Phone extraction is limited to phone/contact fields
            # to avoid treating arbitrary numbers as telephone contacts.
            if context in PHONE_KEYS:
                matches = IMPORT_PHONE_RE.findall(value)
                if matches:
                    phones.extend(matches)
                else:
                    phones.extend(
                        part.strip()
                        for part in re.split(r"[;\n|]+", value)
                        if part.strip()
                    )
            elif context in CONTACT_CONTAINER_KEYS:
                phones.extend(IMPORT_PHONE_RE.findall(value))

    walk(record)

    unique_emails = list(dict.fromkeys(x.lower() for x in emails))
    unique_phones = []
    seen_phone_digits = set()

    for phone in phones:
        digits = re.sub(r"\D", "", phone)
        if not 7 <= len(digits) <= 15:
            continue
        if digits not in seen_phone_digits:
            seen_phone_digits.add(digits)
            unique_phones.append(phone[:100])

    return unique_emails, unique_phones


def parse_import_revenue(record, warnings):
    raw = record.get("revenue_rub")
    if raw is None:
        raw = record.get("annual_revenue_rub")

    if raw is None or raw == "":
        return None

    if isinstance(raw, bool):
        warnings.append("invalid_revenue_format")
        return None

    text = str(raw).strip().replace("\u00a0", "").replace(" ", "").replace(",", ".")
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", text):
        warnings.append("revenue_requires_review")
        return None

    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        warnings.append("invalid_revenue_format")
        return None

    if value < 0 or value > Decimal("999999999999999999.99"):
        warnings.append("revenue_out_of_range")
        return None

    return value


@router.post("/{batch_id}/rows")
def ingest_import_rows(batch_id: int, body: dict):
    batch = get_batch(batch_id)
    records = body.get("records")

    if not isinstance(records, list) or not records:
        raise HTTPException(
            status_code=400,
            detail="В запросе необходим непустой массив records",
        )

    if len(records) > 10000:
        raise HTTPException(
            status_code=413,
            detail="За один запрос можно передать не более 10000 записей",
        )

    saved_ids = []

    with get_conn() as conn:
        batch = conn.execute('SELECT * FROM gab_crm_import_batches WHERE id=%s FOR UPDATE', [batch_id]).fetchone()
        if not batch or batch['status'] not in ('processing', 'review') or not body.get('worker_token') or body['worker_token'] != batch.get('worker_token'):
            raise HTTPException(409, 'Пакет не захвачен этим обработчиком')
        for index, original in enumerate(records, start=1):
            if not isinstance(original, dict):
                continue

            record = original
            warnings = list(record.get("extraction_warnings") or record.get("warnings") or [])
            warnings = [str(x)[:300] for x in warnings[:100]]

            company_name = (
                import_clean(record.get("company_name"))
                or import_clean(record.get("company"))
                or import_clean(record.get("organization"))
                or import_clean(record.get("counterparty_name"))
                or import_clean(record.get("legal_name"))
                or import_clean(import_alias(record, {
                    "наименование организации", "наименование компании",
                    "название организации", "название компании",
                    "организация", "компания", "контрагент",
                    "инвестор", "эмитент",
                }))
            )

            full_name = (
                import_clean(record.get("full_name"))
                or import_clean(record.get("contact_person"))
                or import_clean(record.get("person_name"))
                or import_clean(record.get("fio"))
                or import_clean(import_alias(record, {
                    "фио", "фио контакта", "контактное фио",
                    "контактное лицо", "имя контакта",
                    "фамилия имя",
                }))
            )

            position = (
                import_clean(record.get("position"))
                or import_clean(record.get("title"))
                or import_clean(record.get("job_title"))
                or import_clean(import_alias(record, {
                    "должность", "должность контакта", "позиция",
                }))
            )

            city = import_clean(
                record.get("city") or record.get("location")
                or import_alias(record, {"город", "регион", "местоположение"})
            )

            raw_inn = (
                import_clean(record.get("inn"), 50)
                or import_clean(import_alias(record, {"инн", "tax id"}), 50)
            )

            inn_digits = re.sub(r"\D", "", raw_inn or "")
            inn = inn_digits if len(inn_digits) in (10, 12) else None
            if raw_inn and not inn:
                warnings.append("inn_requires_review")

            emails, phones = collect_import_contacts(record)

            if not emails:
                warnings.append("email_not_found")
            if not phones:
                warnings.append("phone_not_found")
            if not company_name and not full_name:
                warnings.append("identity_requires_review")

            revenue = parse_import_revenue(record, warnings)
            revenue_year = record.get("revenue_year")
            try:
                revenue_year = int(revenue_year) if revenue_year is not None else None
                if revenue_year is not None and not 1900 <= revenue_year <= 2100:
                    revenue_year = None
            except (TypeError, ValueError):
                revenue_year = None

            currency = import_clean(record.get('revenue_currency'), 20)
            if currency and currency.upper() not in {'RUB', 'RUR', 'РУБ', 'РУБ.', '₽'}:
                warnings.append('foreign_currency_requires_financial_review')
                revenue = None
            if revenue is None:
                revenue_status = "unknown"
            elif revenue >= Decimal("100000000"):
                revenue_status = "eligible"
            else:
                revenue_status = "below_threshold"

            revenue_source = (
                import_clean(record.get("revenue_source"), 100)
                if revenue is not None else None
            )
            if revenue is not None and not revenue_source:
                revenue_source = "document"

            page = record.get("source_page", record.get("page"))
            row_number = record.get(
                "source_row",
                record.get("row_number", record.get("row")),
            )

            explicit_key = import_clean(record.get("source_key"), 250)
            if explicit_key:
                source_key = explicit_key
            else:
                source_key = f"p{page or 0}-r{row_number or index}"

            source_locator = import_clean(record.get("source_locator"), 500)
            if not source_locator and (page is not None or row_number is not None):
                source_locator = f"page={page or '-'};row={row_number or index}"

            field_sources = record.get("field_sources")
            if not isinstance(field_sources, dict):
                field_sources = {}
            field_sources = dict(field_sources)
            for field in (
                "company_name", "inn", "city", "full_name",
                "position", "emails", "phones",
            ):
                field_sources.setdefault(field, "document")

            primary_email = emails[0] if emails else None
            primary_phone = phones[0] if phones else None

            raw_json = json.dumps(
                record, ensure_ascii=False, default=str
            )
            emails_json = json.dumps(emails, ensure_ascii=False)
            phones_json = json.dumps(phones, ensure_ascii=False)
            sources_json = json.dumps(field_sources, ensure_ascii=False)
            warnings_json = json.dumps(
                list(dict.fromkeys(warnings)), ensure_ascii=False
            )

            row = conn.execute(
                """
                INSERT INTO gab_crm_import_rows (
                    batch_id, source_key, source_locator, raw_data,
                    company_name, inn, city, full_name, position,
                    phone, email, phones, emails, field_sources,
                    extraction_warnings,
                    revenue_rub, revenue_year, revenue_source,
                    revenue_status, import_status
                )
                VALUES (
                    %s, %s, %s, %s::jsonb,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                    %s::jsonb,
                    %s, %s, %s, %s, %s
                )
                ON CONFLICT (batch_id, source_key)
                DO UPDATE SET
                    source_locator = COALESCE(
                        EXCLUDED.source_locator,
                        gab_crm_import_rows.source_locator
                    ),
                    raw_data = EXCLUDED.raw_data,
                    company_name = COALESCE(
                        EXCLUDED.company_name,
                        gab_crm_import_rows.company_name
                    ),
                    inn = COALESCE(
                        EXCLUDED.inn,
                        gab_crm_import_rows.inn
                    ),
                    city = COALESCE(EXCLUDED.city, gab_crm_import_rows.city),
                    full_name = COALESCE(EXCLUDED.full_name, gab_crm_import_rows.full_name),
                    position = COALESCE(EXCLUDED.position, gab_crm_import_rows.position),
                    phone = COALESCE(EXCLUDED.phone, gab_crm_import_rows.phone),
                    email = COALESCE(EXCLUDED.email, gab_crm_import_rows.email),
                    phones = (
                        SELECT COALESCE(jsonb_agg(v ORDER BY v), '[]'::jsonb)
                        FROM (
                            SELECT DISTINCT value AS v
                            FROM jsonb_array_elements_text(
                                gab_crm_import_rows.phones || EXCLUDED.phones
                            )
                        ) q
                    ),
                    emails = (
                        SELECT COALESCE(jsonb_agg(v ORDER BY v), '[]'::jsonb)
                        FROM (
                            SELECT DISTINCT lower(value) AS v
                            FROM jsonb_array_elements_text(
                                gab_crm_import_rows.emails || EXCLUDED.emails
                            )
                        ) q
                    ),
                    field_sources =
                        gab_crm_import_rows.field_sources || EXCLUDED.field_sources,
                    extraction_warnings = EXCLUDED.extraction_warnings,
                    revenue_rub = COALESCE(
                        EXCLUDED.revenue_rub,
                        gab_crm_import_rows.revenue_rub
                    ),
                    revenue_year = COALESCE(
                        EXCLUDED.revenue_year,
                        gab_crm_import_rows.revenue_year
                    ),
                    revenue_source = COALESCE(
                        EXCLUDED.revenue_source,
                        gab_crm_import_rows.revenue_source
                    ),
                    revenue_status = CASE
                        WHEN EXCLUDED.revenue_rub IS NOT NULL
                        THEN EXCLUDED.revenue_status
                        ELSE gab_crm_import_rows.revenue_status
                    END,
                    updated_at = now()
                RETURNING id
                """,
                [
                    batch_id, source_key, source_locator, raw_json,
                    company_name, inn, city, full_name, position,
                    primary_phone, primary_email,
                    phones_json, emails_json, sources_json, warnings_json,
                    revenue, revenue_year, revenue_source, revenue_status,
                    "pending" if company_name or full_name else "needs_review",
                ],
            ).fetchone()

            saved_ids.append(row["id"])

        stats = conn.execute(
            """
            SELECT
                COUNT(*)::int AS total_rows,
                COUNT(*) FILTER (
                    WHERE revenue_status = 'eligible'
                )::int AS eligible_rows,
                COUNT(*) FILTER (
                    WHERE COALESCE(email, '') = ''
                      AND jsonb_array_length(emails) = 0
                )::int AS without_email_rows,
                COUNT(*) FILTER (
                    WHERE revenue_status = 'below_threshold'
                )::int AS below_threshold_rows,
                COUNT(*) FILTER (
                    WHERE revenue_status IN ('unknown', 'needs_review')
                )::int AS unknown_revenue_rows
            FROM gab_crm_import_rows
            WHERE batch_id = %s
            """,
            [batch_id],
        ).fetchone()

        conn.execute(
            """
            UPDATE gab_crm_import_batches
            SET status = 'review',
                progress_stage='review',processing_finished_at=now(),
                total_rows = %s,
                eligible_rows = %s,
                without_email_rows = %s,
                below_threshold_rows = %s,
                unknown_revenue_rows = %s,
                error_message = NULL,
                updated_at = now()
            WHERE id = %s
            """,
            [
                stats["total_rows"],
                stats["eligible_rows"],
                stats["without_email_rows"],
                stats["below_threshold_rows"],
                stats["unknown_revenue_rows"],
                batch_id,
            ],
        )

    return {
        "batch_id": batch_id,
        "saved": len(saved_ids),
        "row_ids": saved_ids,
        "stats": stats,
        "status": "review",
    }


@router.patch('/{batch_id}/rows/{row_id}')
def edit_import_row(batch_id: int, row_id: int, body: dict):
    from app.import_editing import validate_changes
    try:
        changes = validate_changes(body.get('changes'))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    version = body.get('version')
    if type(version) is not int or version < 0:
        raise HTTPException(422, 'Необходима версия строки')
    with get_conn() as conn:
        row = conn.execute(
            'SELECT * FROM gab_crm_import_rows WHERE batch_id = %s AND id = %s FOR UPDATE',
            [batch_id, row_id],
        ).fetchone()
        if not row:
            raise HTTPException(404, 'Строка не найдена')
        if row['import_status'] not in ('pending', 'needs_review'):
            raise HTTPException(409, 'Можно редактировать только неподтверждённую строку')
        if row['edit_version'] != version:
            raise HTTPException(409, 'Строка изменилась. Обновите данные перед сохранением')
        changes = {key: value for key, value in changes.items() if row.get(key) != value}
        if not changes:
            return {'item': row}
        # The actor is the configured Basic Auth identity, never client input.
        actor = os.environ.get('GAB_APP_USER')
        if not actor:
            raise HTTPException(503, 'Пользователь аудита не настроен')
        audit = {key: {'before': row.get(key), 'after': value} for key, value in changes.items()}
        updated = conn.execute(
            '''UPDATE gab_crm_import_rows
               SET manual_overrides = manual_overrides || %s::jsonb, updated_at = now()
               WHERE id = %s RETURNING *''',
            [json.dumps(changes, ensure_ascii=False, default=str), row_id],
        ).fetchone()
        conn.execute(
            '''INSERT INTO gab_crm_import_edits(import_row_id, actor, changes)
               VALUES (%s, %s, %s::jsonb)''',
            [row_id, actor, json.dumps(audit, ensure_ascii=False, default=str)],
        )
    return {'item': updated}


@router.get('/{batch_id}/rows/{row_id}/edits')
def import_edit_history(batch_id: int, row_id: int):
    with get_conn() as conn:
        row = conn.execute('SELECT id FROM gab_crm_import_rows WHERE batch_id=%s AND id=%s', [batch_id, row_id]).fetchone()
        if not row:
            raise HTTPException(404, 'Строка не найдена')
        items = conn.execute(
            'SELECT actor, changes, created_at FROM gab_crm_import_edits WHERE import_row_id=%s ORDER BY id DESC LIMIT 100',
            [row_id],
        ).fetchall()
    return {'items': items}


@router.post('/{batch_id}/retry')
def retry_import_batch(batch_id: int):
    with get_conn() as conn:
        row = conn.execute(
            """UPDATE gab_crm_import_batches SET status='uploaded', worker_token=NULL, error_message=NULL, cancelled_at=NULL,processing_started_at=NULL,processing_finished_at=NULL,progress_stage='queued',progress_detail='{}'::jsonb,progress_pages='[]'::jsonb,progress_total=NULL,progress_updated_at=NULL,updated_at=now()
               WHERE id=%s AND status='failed' RETURNING *""", [batch_id],
        ).fetchone()
        if not row:
            raise HTTPException(409, 'Повторить можно только пакет с ошибкой')
    return {'item': row}
