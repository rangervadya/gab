import os
import urllib.request
import urllib.error
import json
import base64
import re
import uuid
import hashlib
import mimetypes

from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from app.registry_enrichment import router as registry_enrichment_router
from app.import_enrichment import router as import_enrichment_router
from app.import_bulk import router as import_bulk_router
from app.investor_imports_api import router as investor_imports_router
from app.import_confirmation import router as import_confirmation_router


router = APIRouter(prefix="/api/mailings", tags=["mailings"])
router.include_router(registry_enrichment_router)
router.include_router(import_enrichment_router)
router.include_router(import_bulk_router)
router.include_router(investor_imports_router)
router.include_router(import_confirmation_router)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
STORAGE_DIR = Path(
    os.environ.get("GAB_MAILING_STORAGE", "/srv/gab-review-data/mailings")
)

MAX_FILE_SIZE = 20 * 1024 * 1024
MAX_TEST_ATTACHMENTS_TOTAL = 8 * 1024 * 1024

WEBHOOK_URL = os.environ.get("GAB_MAILING_WEBHOOK_URL", "")
WEBHOOK_TOKEN = os.environ.get("GAB_MAILING_WEBHOOK_TOKEN", "")

ALLOWED_EXTENSIONS = {
    ".pdf", ".doc", ".docx",
    ".xls", ".xlsx", ".csv",
    ".jpg", ".jpeg", ".png", ".webp"
}


class MailingCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    subject: str = Field(default="", max_length=300)
    body_html: str = Field(default="", max_length=300000)
    body_text: str = Field(default="", max_length=100000)


class MailingUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    subject: str = Field(default="", max_length=300)
    body_html: str = Field(default="", max_length=300000)
    body_text: str = Field(default="", max_length=100000)


class RecipientSelection(BaseModel):
    investor_id: int
    email: str = Field(min_length=3, max_length=254)


class RecipientsUpdate(BaseModel):
    # New format: one selected email per investor.
    recipients: list[RecipientSelection] = Field(
        default_factory=list, max_length=5000
    )
    # Legacy format retained for older clients.
    investor_ids: list[int] = Field(default_factory=list, max_length=1000)
    review_record_ids: list[str] = Field(default_factory=list, max_length=1000)


class TestSendRequest(BaseModel):
    to_email: str = Field(min_length=3, max_length=254)


def get_conn():
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    try:
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Database connection failed: {exc.__class__.__name__}"
        )


def check_mailing(conn, mailing_id):
    row = conn.execute(
        "SELECT * FROM gab_crm_mailings WHERE id = %s FOR UPDATE",
        [mailing_id]
    ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Mailing not found")

    if row.get("bulk_run_id") or row["status"] in ("sending", "sent", "archived"):
        raise HTTPException(
            status_code=409,
            detail="This mailing is not editable"
        )

    return row


@router.get("/recipients")
def list_recipients(q: str = "", limit: int = 5000):
    limit = max(1, min(limit, 5000))
    pattern = f"%{q.strip()}%"

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                i.id AS investor_id,
                i.name AS investor_name,
                i.company_id,
                c.company_name,
                c.inn,
                ct.id AS contact_id,
                ct.full_name AS contact_name,
                ct.position,
                addr.email,
                COALESCE(addr.email_source, 'missing') AS email_source,
                ct.phone,
                c.general_phone
            FROM gab_crm_investors i
            LEFT JOIN gab_crm_companies c ON c.id = i.company_id
            LEFT JOIN gab_crm_contacts ct ON ct.id = i.primary_contact_id
            LEFT JOIN LATERAL (
                SELECT DISTINCT ON (lower(btrim(a.email)))
                    btrim(a.email) AS email,
                    a.email_source
                FROM (
                    SELECT ct.email::text AS email, 'contact'::text AS email_source
                    UNION ALL
                    SELECT e.value, 'contact'::text
                    FROM jsonb_array_elements_text(
                        COALESCE(ct.emails, '[]'::jsonb)
                    ) AS e(value)
                    UNION ALL
                    SELECT c.general_email::text, 'company'::text
                    UNION ALL
                    SELECT e.value, 'company'::text
                    FROM jsonb_array_elements_text(
                        COALESCE(c.emails, '[]'::jsonb)
                    ) AS e(value)
                ) a
                WHERE NULLIF(btrim(a.email), '') IS NOT NULL
                ORDER BY
                    lower(btrim(a.email)),
                    CASE a.email_source
                        WHEN 'contact' THEN 0
                        ELSE 1
                    END
            ) addr ON TRUE
            WHERE
                %s = ''
                OR COALESCE(i.name, '') ILIKE %s
                OR COALESCE(c.company_name, '') ILIKE %s
                OR COALESCE(c.inn, '') ILIKE %s
                OR COALESCE(ct.full_name, '') ILIKE %s
                OR COALESCE(addr.email, '') ILIKE %s
                OR COALESCE(ct.email, '') ILIKE %s
                OR COALESCE(c.general_email, '') ILIKE %s
            ORDER BY
                i.created_at DESC,
                i.id DESC,
                addr.email_source,
                addr.email
            LIMIT %s
            """,
            [
                q.strip(),
                pattern, pattern, pattern, pattern,
                pattern, pattern, pattern,
                limit
            ]
        ).fetchall()

    return {"items": rows, "total": len(rows)}



@router.get("/registry-recipients")
def list_registry_recipients(q: str = "", limit: int = 500):
    limit = max(1, min(limit, 500))
    query = q.strip()
    pattern = f"%{query}%"

    with get_conn() as conn:
        rows = conn.execute(
            """
            WITH base AS (
                SELECT
                    record_id,
                    source_page,
                    source_row,
                    review_state,
                    COALESCE(
                        NULLIF(BTRIM(corrected_fields->>'internal_name'), ''),
                        NULLIF(BTRIM(payload->>'corrected_internal_name'), ''),
                        NULLIF(BTRIM(internal_name), ''),
                        NULLIF(BTRIM(payload->>'internal_name'), '')
                    ) AS internal_name,
                    COALESCE(
                        NULLIF(BTRIM(payload->>'corrected_counterparty_contacts'), ''),
                        NULLIF(BTRIM(counterparty_contacts), ''),
                        NULLIF(BTRIM(payload->>'counterparty_contacts'), '')
                    ) AS counterparty
                FROM gab_review_records
                WHERE review_state IN ('corrected', 'not_required')
            ),
            normalized AS (
                SELECT *,
                    LOWER(BTRIM(REGEXP_REPLACE(
                        counterparty, '[[:space:]]+', ' ', 'g'
                    ))) AS group_key
                FROM base
                WHERE NULLIF(BTRIM(counterparty), '') IS NOT NULL
            ),
            grouped AS (
                SELECT
                    group_key,
                    (ARRAY_AGG(record_id ORDER BY record_id))[1] AS record_id,
                    (ARRAY_AGG(counterparty ORDER BY source_page NULLS LAST,
                               source_row NULLS LAST, record_id))[1] AS counterparty,
                    COUNT(*)::int AS occurrences,
                    ARRAY_AGG(record_id ORDER BY record_id) AS record_ids,
                    STRING_AGG(
                        DISTINCT NULLIF(internal_name, ''),
                        ', ' ORDER BY NULLIF(internal_name, '')
                    ) AS object_names,
                    JSONB_AGG(
                        JSONB_BUILD_OBJECT(
                            'record_id', record_id,
                            'source_page', source_page,
                            'source_row', source_row,
                            'internal_name', internal_name,
                            'review_state', review_state
                        )
                        ORDER BY source_page NULLS LAST,
                                 source_row NULLS LAST, record_id
                    ) AS source_records
                FROM normalized
                GROUP BY group_key
            )
            SELECT record_id, counterparty, occurrences,
                   record_ids, object_names, source_records
            FROM grouped
            WHERE
                %s = ''
                OR counterparty ILIKE %s
                OR COALESCE(object_names, '') ILIKE %s
                OR record_id ILIKE %s
                OR source_records::text ILIKE %s
                OR EXISTS (
                    SELECT 1
                    FROM gab_review_enrichment e
                    WHERE e.review_record_id = ANY(record_ids)
                      AND e.inn ILIKE %s
                )
            ORDER BY counterparty
            LIMIT %s
            """,
            [query, pattern, pattern, pattern, pattern, pattern, limit]
        ).fetchall()

    return {"items": rows, "total": len(rows)}


@router.get("/")
def list_mailings():
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                m.*,
                (SELECT COUNT(*)::int
                 FROM gab_crm_mailing_attachments a
                 WHERE a.mailing_id = m.id) AS attachment_count,
                (SELECT COUNT(*)::int
                 FROM gab_crm_mailing_recipients r
                 WHERE r.mailing_id = m.id) AS recipient_count
            FROM gab_crm_mailings m
            ORDER BY m.created_at DESC
            LIMIT 200
            """
        ).fetchall()

    return {"items": rows}


@router.post("/")
def create_mailing(body: MailingCreate):
    with get_conn() as conn:
        row = conn.execute(
            """
            INSERT INTO gab_crm_mailings
                (name, subject, body_html, body_text)
            VALUES (%s, %s, %s, %s)
            RETURNING *
            """,
            [body.name.strip(), body.subject, body.body_html, body.body_text]
        ).fetchone()

    return row


@router.get("/{mailing_id}")
def get_mailing(mailing_id: int):
    with get_conn() as conn:
        mailing = conn.execute(
            "SELECT * FROM gab_crm_mailings WHERE id = %s",
            [mailing_id]
        ).fetchone()

        if not mailing:
            raise HTTPException(status_code=404, detail="Mailing not found")

        attachments = conn.execute(
            """
            SELECT id, original_filename, mime_type, size_bytes, sha256, created_at
            FROM gab_crm_mailing_attachments
            WHERE mailing_id = %s
            ORDER BY id
            """,
            [mailing_id]
        ).fetchall()

        recipients = conn.execute(
            """
            SELECT id, investor_id, company_id, contact_id, review_record_id,
                   recipient_name, email, status, error_message
            FROM gab_crm_mailing_recipients
            WHERE mailing_id = %s
            ORDER BY id
            """,
            [mailing_id]
        ).fetchall()

        events = conn.execute(
            """
            SELECT event_type, detail_json, created_at
            FROM gab_crm_mailing_events
            WHERE mailing_id = %s
            ORDER BY created_at DESC
            LIMIT 50
            """,
            [mailing_id]
        ).fetchall()

    return {
        "mailing": mailing,
        "attachments": attachments,
        "recipients": recipients,
        "events": events
    }


@router.patch("/{mailing_id}")
def update_mailing(mailing_id: int, body: MailingUpdate):
    with get_conn() as conn:
        check_mailing(conn, mailing_id)

        row = conn.execute(
            """
            UPDATE gab_crm_mailings
            SET name = %s,
                subject = %s,
                body_html = %s,
                body_text = %s,
                updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            [
                body.name.strip(),
                body.subject,
                body.body_html,
                body.body_text,
                mailing_id
            ]
        ).fetchone()

    return row


@router.post("/{mailing_id}/recipients")
def save_recipients(mailing_id: int, body: RecipientsUpdate):
    ids = list(dict.fromkeys(body.investor_ids))
    record_ids = list(dict.fromkeys(
        str(value).strip()
        for value in body.review_record_ids
        if str(value).strip()
    ))

    with get_conn() as conn:
        check_mailing(conn, mailing_id)

        conn.execute(
            "DELETE FROM gab_crm_mailing_recipients WHERE mailing_id = %s",
            [mailing_id]
        )

        if body.recipients and ids:
            raise HTTPException(
                status_code=422,
                detail="Use either recipients or legacy investor_ids, not both"
            )

        if body.recipients:
            selected = []
            requested_pairs = set()

            for item in body.recipients:
                email = item.email.strip()
                if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                    raise HTTPException(
                        status_code=422,
                        detail="One of the selected email addresses is invalid"
                    )

                key = (int(item.investor_id), email.lower())
                if key in requested_pairs:
                    continue

                requested_pairs.add(key)
                selected.append({
                    "investor_id": int(item.investor_id),
                    "email": email
                })

            selected_json = json.dumps(selected, ensure_ascii=False)

            rows = conn.execute(
                """
                WITH selected AS (
                    SELECT DISTINCT
                        investor_id,
                        lower(btrim(email)) AS email
                    FROM jsonb_to_recordset(%s::jsonb)
                        AS x(investor_id bigint, email text)
                )
                SELECT
                    i.id AS investor_id,
                    i.company_id,
                    CASE
                        WHEN addr.email_source = 'contact' THEN ct.id
                        ELSE NULL
                    END AS contact_id,
                    COALESCE(
                        NULLIF(BTRIM(ct.full_name), ''),
                        i.name,
                        ''
                    ) AS recipient_name,
                    btrim(addr.email) AS email
                FROM selected s
                JOIN gab_crm_investors i ON i.id = s.investor_id
                LEFT JOIN gab_crm_companies c ON c.id = i.company_id
                LEFT JOIN gab_crm_contacts ct
                    ON ct.id = i.primary_contact_id
                CROSS JOIN LATERAL (
                    SELECT a.email, a.email_source
                    FROM (
                        SELECT
                            ct.email::text AS email,
                            'contact'::text AS email_source
                        UNION ALL
                        SELECT e.value, 'contact'::text
                        FROM jsonb_array_elements_text(
                            COALESCE(ct.emails, '[]'::jsonb)
                        ) AS e(value)
                        UNION ALL
                        SELECT
                            c.general_email::text,
                            'company'::text
                        UNION ALL
                        SELECT e.value, 'company'::text
                        FROM jsonb_array_elements_text(
                            COALESCE(c.emails, '[]'::jsonb)
                        ) AS e(value)
                    ) a
                    WHERE NULLIF(btrim(a.email), '') IS NOT NULL
                      AND lower(btrim(a.email)) = s.email
                    ORDER BY CASE a.email_source
                        WHEN 'contact' THEN 0
                        ELSE 1
                    END
                    LIMIT 1
                ) addr
                ORDER BY i.id, lower(btrim(addr.email))
                """,
                [selected_json]
            ).fetchall()

            found_pairs = {
                (int(row["investor_id"]), row["email"].strip().lower())
                for row in rows
            }

            if found_pairs != requested_pairs:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Some selected email addresses no longer belong "
                        "to the selected investor"
                    )
                )

            # One destination email may appear under several investors.
            # Keep only its first selected CRM association in this campaign.
            seen_emails = set()

            for row in rows:
                email_key = row["email"].strip().lower()
                if email_key in seen_emails:
                    continue
                seen_emails.add(email_key)

                conn.execute(
                    """
                    INSERT INTO gab_crm_mailing_recipients
                        (mailing_id, investor_id, company_id, contact_id,
                         recipient_name, email, status)
                    VALUES (%s, %s, %s, %s, %s, %s, 'pending')
                    ON CONFLICT DO NOTHING
                    """,
                    [
                        mailing_id,
                        row["investor_id"],
                        row["company_id"],
                        row["contact_id"],
                        row["recipient_name"],
                        row["email"]
                    ]
                )

        elif ids:
            # Backwards-compatible path for clients that still send investor_ids.
            rows = conn.execute(
                """
                SELECT
                    i.id AS investor_id,
                    i.company_id,
                    CASE
                        WHEN NULLIF(BTRIM(ct.email), '') IS NOT NULL
                            THEN ct.id
                        ELSE NULL
                    END AS contact_id,
                    COALESCE(NULLIF(BTRIM(ct.full_name), ''), i.name, '') AS recipient_name,
                    COALESCE(
                        NULLIF(BTRIM(ct.email), ''),
                        NULLIF(BTRIM(c.general_email), '')
                    ) AS email
                FROM gab_crm_investors i
                LEFT JOIN gab_crm_companies c ON c.id = i.company_id
                LEFT JOIN gab_crm_contacts ct ON ct.id = i.primary_contact_id
                WHERE i.id = ANY(%s)
                """,
                [ids]
            ).fetchall()

            seen_emails = set()
            for row in rows:
                email_key = (row["email"] or "").strip().lower()
                if email_key and email_key in seen_emails:
                    continue
                if email_key:
                    seen_emails.add(email_key)

                conn.execute(
                    """
                    INSERT INTO gab_crm_mailing_recipients
                        (mailing_id, investor_id, company_id, contact_id,
                         recipient_name, email, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    [
                        mailing_id,
                        row["investor_id"],
                        row["company_id"],
                        row["contact_id"],
                        row["recipient_name"],
                        row["email"],
                        "pending" if row["email"] else "no_email"
                    ]
                )

        if record_ids:
            registry_rows = conn.execute(
                """
                WITH normalized AS (
                    SELECT
                        record_id,
                        COALESCE(
                            NULLIF(BTRIM(payload->>'corrected_counterparty_contacts'), ''),
                            NULLIF(BTRIM(counterparty_contacts), ''),
                            NULLIF(BTRIM(payload->>'counterparty_contacts'), '')
                        ) AS counterparty,
                        COALESCE(
                            NULLIF(BTRIM(corrected_fields->>'internal_name'), ''),
                            NULLIF(BTRIM(payload->>'corrected_internal_name'), ''),
                            NULLIF(BTRIM(internal_name), ''),
                            NULLIF(BTRIM(payload->>'internal_name'), '')
                        ) AS internal_name
                    FROM gab_review_records
                    WHERE review_state IN ('corrected', 'not_required')
                )
                SELECT record_id, counterparty, internal_name
                FROM normalized
                WHERE record_id = ANY(%s)
                """,
                [record_ids]
            ).fetchall()

            for row in registry_rows:
                recipient_name = (
                    row["counterparty"]
                    or row["internal_name"]
                    or row["record_id"]
                )

                conn.execute(
                    """
                    INSERT INTO gab_crm_mailing_recipients
                        (mailing_id, review_record_id,
                         recipient_name, email, status)
                    VALUES (%s, %s, %s, NULL, 'no_email')
                    ON CONFLICT DO NOTHING
                    """,
                    [
                        mailing_id,
                        row["record_id"],
                        recipient_name
                    ]
                )

        counts = conn.execute(
            """
            SELECT
                COUNT(*)::int AS selected,
                COUNT(*) FILTER (WHERE email IS NOT NULL)::int AS with_email,
                COUNT(*) FILTER (WHERE email IS NULL)::int AS without_email,
                COUNT(*) FILTER (WHERE review_record_id IS NOT NULL)::int
                    AS registry_selected
            FROM gab_crm_mailing_recipients
            WHERE mailing_id = %s
            """,
            [mailing_id]
        ).fetchone()

    return {"saved": True, **counts}


@router.post("/{mailing_id}/attachments")
async def upload_attachment(mailing_id: int, file: UploadFile = File(...)):
    original = (file.filename or "attachment").replace("\\", "/").split("/")[-1]
    extension = Path(original).suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail="Allowed formats: PDF, DOC, DOCX, XLS, XLSX, CSV, JPG, PNG, WEBP"
        )

    content = await file.read(MAX_FILE_SIZE + 1)

    if not content:
        raise HTTPException(status_code=400, detail="File is empty")

    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="Maximum file size is 20 MB")

    storage_name = uuid.uuid4().hex + extension
    target_dir = STORAGE_DIR / str(mailing_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / storage_name

    try:
        with get_conn() as conn:
            check_mailing(conn, mailing_id)

            target.write_bytes(content)

            row = conn.execute(
                """
                INSERT INTO gab_crm_mailing_attachments
                    (mailing_id, original_filename, storage_filename,
                     mime_type, size_bytes, sha256)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id, original_filename, mime_type, size_bytes, sha256
                """,
                [
                    mailing_id,
                    original,
                    storage_name,
                    file.content_type or mimetypes.guess_type(original)[0] or "application/octet-stream",
                    len(content),
                    hashlib.sha256(content).hexdigest()
                ]
            ).fetchone()

        return row

    except Exception:
        target.unlink(missing_ok=True)
        raise


@router.get("/{mailing_id}/attachments/{attachment_id}/download")
def download_attachment(mailing_id: int, attachment_id: int):
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT original_filename, storage_filename, mime_type
            FROM gab_crm_mailing_attachments
            WHERE id = %s AND mailing_id = %s
            """,
            [attachment_id, mailing_id]
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Attachment not found")

    path = (STORAGE_DIR / str(mailing_id) / row["storage_filename"]).resolve()

    if not path.is_file() or STORAGE_DIR.resolve() not in path.parents:
        raise HTTPException(status_code=404, detail="Stored file not found")

    return FileResponse(
        path,
        media_type=row["mime_type"],
        filename=row["original_filename"]
    )


@router.delete("/{mailing_id}/attachments/{attachment_id}")
def delete_attachment(mailing_id: int, attachment_id: int):
    with get_conn() as conn:
        check_mailing(conn, mailing_id)

        row = conn.execute(
            """
            DELETE FROM gab_crm_mailing_attachments
            WHERE id = %s AND mailing_id = %s
            RETURNING storage_filename
            """,
            [attachment_id, mailing_id]
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Attachment not found")

    path = STORAGE_DIR / str(mailing_id) / row["storage_filename"]
    path.unlink(missing_ok=True)

    return {"deleted": True, "attachment_id": attachment_id}


@router.post("/{mailing_id}/test-send")
def send_test_email(mailing_id: int, body: TestSendRequest):
    to_email = body.to_email.strip()

    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", to_email):
        raise HTTPException(status_code=422, detail="Проверь адрес электронной почты")

    if not WEBHOOK_URL.startswith("https://n8n.techsmysl.ru/webhook/"):
        raise HTTPException(status_code=503, detail="Webhook отправки не настроен")

    if not WEBHOOK_TOKEN:
        raise HTTPException(status_code=503, detail="Токен webhook не настроен")

    # Сначала читаем кампанию и список вложений.
    with get_conn() as conn:
        mailing = check_mailing(conn, mailing_id)

        if not mailing["subject"].strip():
            raise HTTPException(status_code=422, detail="Укажи тему письма")

        if not mailing["body_html"].strip() and not mailing["body_text"].strip():
            raise HTTPException(status_code=422, detail="Добавь текст письма")

        attachments = conn.execute(
            """
            SELECT id, original_filename, storage_filename,
                   mime_type, size_bytes, sha256
            FROM gab_crm_mailing_attachments
            WHERE mailing_id = %s
            ORDER BY id
            """,
            [mailing_id]
        ).fetchall()

    total_bytes = sum(int(item["size_bytes"]) for item in attachments)
    if total_bytes > MAX_TEST_ATTACHMENTS_TOTAL:
        raise HTTPException(
            status_code=413,
            detail="Для тестового письма общий размер вложений не должен превышать 8 МБ"
        )

    encoded_attachments = []

    for item in attachments:
        path = (
            STORAGE_DIR / str(mailing_id) / item["storage_filename"]
        ).resolve()

        if STORAGE_DIR.resolve() not in path.parents or not path.is_file():
            raise HTTPException(status_code=404, detail="Файл вложения не найден")

        content = path.read_bytes()

        if len(content) != int(item["size_bytes"]):
            raise HTTPException(status_code=409, detail="Размер файла изменился")

        digest = hashlib.sha256(content).hexdigest()
        if digest != item["sha256"]:
            raise HTTPException(status_code=409, detail="Проверка целостности файла не пройдена")

        encoded_attachments.append({
            "filename": item["original_filename"],
            "mime_type": item["mime_type"],
            "data_base64": base64.b64encode(content).decode("ascii")
        })

    payload = {
        "request_id": uuid.uuid4().hex,
        "mailing_id": mailing_id,
        "to_email": to_email,
        "subject": mailing["subject"],
        "body_html": mailing["body_html"],
        "body_text": mailing["body_text"],
        "attachments": encoded_attachments
    }

    request = urllib.request.Request(
        WEBHOOK_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-GAB-Webhook-Token": WEBHOOK_TOKEN
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            response_body = response.read(100_000)
            response_code = response.status
    except urllib.error.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"n8n отклонил отправку: HTTP {exc.code}"
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Не удалось связаться с n8n: {type(exc).__name__}"
        )

    if response_code < 200 or response_code >= 300:
        raise HTTPException(status_code=502, detail="n8n не подтвердил отправку")

    try:
        provider_result = json.loads(response_body.decode("utf-8"))
    except Exception:
        provider_result = {}

    if provider_result.get("sent") is not True:
        raise HTTPException(
            status_code=502,
            detail="n8n не вернул подтверждение успешной отправки"
        )

    # Фиксируем только подтверждённую тестовую отправку.
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE gab_crm_mailings
            SET status = 'test_sent',
                test_sent_at = now(),
                updated_at = now()
            WHERE id = %s AND bulk_run_id IS NULL
            """,
            [mailing_id]
        )

        conn.execute(
            """
            INSERT INTO gab_crm_mailing_events
                (mailing_id, event_type, detail_json)
            VALUES (%s, 'test_email_sent', %s::jsonb)
            """,
            [
                mailing_id,
                json.dumps({
                    "to_email": to_email,
                    "attachment_count": len(encoded_attachments),
                    "request_id": payload["request_id"]
                })
            ]
        )

    return {
        "sent": True,
        "mailing_id": mailing_id,
        "to_email": to_email,
        "attachment_count": len(encoded_attachments)
    }
