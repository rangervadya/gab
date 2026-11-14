import hmac
import json
import os
from pathlib import Path
from typing import Literal

import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATABASE_URL = os.environ.get("DATABASE_URL", "")
APP_USER = os.environ.get("GAB_APP_USER", "")
APP_PASSWORD = os.environ.get("GAB_APP_PASSWORD", "")

app = FastAPI(title="GAB Review Center", docs_url=None, redoc_url=None)

class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not APP_USER or not APP_PASSWORD:
            return JSONResponse({"detail": "GAB_APP_USER and GAB_APP_PASSWORD must be configured"}, status_code=503)
        auth = request.headers.get("authorization", "")
        try:
            scheme, encoded = auth.split(" ", 1)
            import base64
            username, password = base64.b64decode(encoded).decode("utf-8").split(":", 1)
        except Exception:
            username = password = ""
            scheme = ""
        valid = (
            scheme.lower() == "basic"
            and hmac.compare_digest(username, APP_USER)
            and hmac.compare_digest(password, APP_PASSWORD)
        )
        if not valid:
            return JSONResponse(
                {"detail": "Authentication required"},
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="GAB Review Center"'},
            )
        return await call_next(request)

app.add_middleware(BasicAuthMiddleware)

def get_conn():
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    try:
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database connection failed: {exc.__class__.__name__}")

class ReviewUpdate(BaseModel):
    review_state: Literal["pending", "approved", "corrected", "needs_source_check", "not_required"]
    reviewer_decision: str = ""
    reviewer_notes: str = ""
    corrected_fields: dict = Field(default_factory=dict)

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")

@app.get("/api/health")
def health():
    with get_conn() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "service": "GAB Review Center"}

@app.get("/api/summary")
def summary():
    with get_conn() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*)::int AS total_records,
                COUNT(*) FILTER (WHERE review_required)::int AS review_required,
                COUNT(*) FILTER (WHERE review_required AND review_state='pending')::int AS pending,
                COUNT(*) FILTER (WHERE review_state IN ('approved','corrected','needs_source_check'))::int AS reviewed,
                COUNT(DISTINCT NULLIF(BTRIM(counterparty_contacts), ''))::int AS distinct_contact_strings
            FROM gab_review_records
        """).fetchone()
    return row

@app.get("/api/records")
def list_records(
    scope: Literal["pending", "all", "reviewed", "not_required"] = "pending",
    q: str = Query(default="", max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
):
    where = []
    params = []
    if scope == "pending":
        where.append("review_required = TRUE AND review_state = 'pending'")
    elif scope == "reviewed":
        where.append("review_state IN ('approved','corrected','needs_source_check')")
    elif scope == "not_required":
        where.append("review_required = FALSE")
    if q.strip():
        where.append("""(
            record_id ILIKE %s OR COALESCE(internal_name,'') ILIKE %s
            OR COALESCE(counterparty_contacts,'') ILIKE %s
            OR COALESCE(address,'') ILIKE %s
            OR COALESCE(review_reasons,'') ILIKE %s
            OR COALESCE(additional_review_note,'') ILIKE %s
        )""")
        pattern = f"%{q.strip()}%"
        params.extend([pattern] * 6)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    offset = (page - 1) * page_size
    with get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*)::int AS n FROM gab_review_records {clause}", params
        ).fetchone()["n"]
        rows = conn.execute(
            f"""SELECT record_id, source_page, source_row, internal_name,
                       counterparty_contacts, address, review_required,
                       review_status, review_reasons, additional_review_note,
                       review_state, reviewer_decision
                FROM gab_review_records {clause}
                ORDER BY review_required DESC, source_page, source_row
                LIMIT %s OFFSET %s""",
            params + [page_size, offset],
        ).fetchall()
    return {"total": total, "page": page, "page_size": page_size, "items": rows}

@app.get("/api/records/{record_id}")
def get_record(record_id: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM gab_review_records WHERE record_id = %s",
            [record_id],
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Record not found")
    return row

@app.patch("/api/records/{record_id}")
def update_record(record_id: str, body: ReviewUpdate):
    with get_conn() as conn:
        row = conn.execute(
            """UPDATE gab_review_records
               SET review_state = %s,
                   reviewer_decision = %s,
                   reviewer_notes = %s,
                   corrected_fields = %s::jsonb,
                   updated_at = now()
               WHERE record_id = %s
               RETURNING record_id""",
            [
                body.review_state,
                body.reviewer_decision,
                body.reviewer_notes,
                json.dumps(body.corrected_fields, ensure_ascii=False),
                record_id,
            ],
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Record not found")
    return {"saved": True, "record_id": record_id}
