import os
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from fastapi import APIRouter, HTTPException, Query


DATABASE_URL = os.environ.get("DATABASE_URL", "")

router = APIRouter(prefix="/api/crm", tags=["CRM"])


ENTITIES = {
    "companies": {
        "table": "gab_crm_companies",
        "fields": (
            "company_name", "inn", "city", "website",
            "general_email", "general_phone", "emails", "phones", "notes"
        ),
        "required": ("company_name",),
        "search": ("company_name", "inn", "city"),
        "json": ("emails", "phones"),
        "updated": True,
    },
    "contacts": {
        "table": "gab_crm_contacts",
        "fields": (
            "company_id", "full_name", "position", "phone",
            "email", "city", "is_decision_maker", "emails", "phones", "notes"
        ),
        "required": ("full_name",),
        "search": ("full_name", "position", "phone", "email"),
        "json": ("emails", "phones"),
        "updated": True,
    },
    "assets": {
        "table": "gab_crm_assets",
        "fields": (
            "title", "asset_type", "city", "address",
            "price_rub", "area_sqm", "annual_income_rub",
            "expected_yield_pct", "asset_status",
            "description", "key_data"
        ),
        "required": ("title",),
        "search": ("title", "city", "address", "asset_type"),
        "json": ("key_data",),
        "updated": True,
    },
    "investors": {
        "table": "gab_crm_investors",
        "fields": (
            "name", "company_id", "primary_contact_id",
            "budget_min_rub", "budget_max_rub", "source_of_funds",
            "preferred_asset_types", "interested_asset_id",
            "investment_horizon", "motivation", "temperature",
            "response_status", "pipeline_stage",
            "has_budget_source", "interested_in_asset",
            "horizon_within_6_months", "decision_maker_access",
            "agreed_next_step", "lead_source", "owner",
            "next_step", "next_step_at", "notes"
        ),
        "required": ("name",),
        "search": ("name", "source_of_funds", "motivation", "lead_source"),
        "json": ("preferred_asset_types",),
        "updated": True,
    },
    "interactions": {
        "table": "gab_crm_interactions",
        "fields": (
            "investor_id", "company_id", "contact_id", "asset_id",
            "interaction_type", "direction", "subject", "body",
            "happened_at"
        ),
        "required": ("interaction_type",),
        "search": ("subject", "body"),
        "json": (),
        "updated": False,
    },
    "tasks": {
        "table": "gab_crm_tasks",
        "fields": (
            "title", "description", "task_type", "due_at",
            "status", "priority", "assigned_to",
            "investor_id", "company_id", "contact_id",
            "asset_id", "completed_at"
        ),
        "required": ("title",),
        "search": ("title", "description", "assigned_to"),
        "json": (),
        "updated": True,
    },
}


def get_conn():
    if not DATABASE_URL:
        raise HTTPException(
            status_code=503,
            detail="DATABASE_URL is not configured"
        )

    try:
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Database connection failed: {exc.__class__.__name__}"
        )


def get_entity(entity: str):
    spec = ENTITIES.get(entity)
    if not spec:
        raise HTTPException(status_code=404, detail="CRM раздел не найден")
    return spec


def prepare_data(spec: dict, body: dict[str, Any], creating: bool):
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Ожидается JSON-объект")

    allowed = set(spec["fields"])
    unknown = set(body) - allowed

    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Неизвестные поля: {', '.join(sorted(unknown))}"
        )

    if not body:
        raise HTTPException(status_code=422, detail="Нет данных для сохранения")

    if creating:
        for field in spec["required"]:
            value = body.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                raise HTTPException(
                    status_code=422,
                    detail=f"Обязательное поле: {field}"
                )

    result = dict(body)

    for field in spec["json"]:
        if field in result and result[field] is not None:
            result[field] = Jsonb(result[field])

    return result


@router.get("/summary")
def crm_summary():
    with get_conn() as conn:
        counts = conn.execute("""
            SELECT
                (SELECT COUNT(*)::int FROM gab_crm_companies) AS companies,
                (SELECT COUNT(*)::int FROM gab_crm_contacts) AS contacts,
                (SELECT COUNT(*)::int FROM gab_crm_assets) AS assets,
                (SELECT COUNT(*)::int FROM gab_crm_investors) AS investors,
                (SELECT COUNT(*)::int FROM gab_crm_interactions) AS interactions,
                (SELECT COUNT(*)::int FROM gab_crm_tasks) AS tasks,
                (SELECT COUNT(*)::int FROM gab_crm_investors
                    WHERE qualified) AS qualified_investors
        """).fetchone()

        temperatures = conn.execute("""
            SELECT temperature, COUNT(*)::int AS total
            FROM gab_crm_investors
            GROUP BY temperature
        """).fetchall()

        stages = conn.execute("""
            SELECT pipeline_stage, COUNT(*)::int AS total
            FROM gab_crm_investors
            GROUP BY pipeline_stage
        """).fetchall()

    counts["investors_by_temperature"] = {
        row["temperature"]: row["total"] for row in temperatures
    }
    counts["investors_by_stage"] = {
        row["pipeline_stage"]: row["total"] for row in stages
    }

    return counts


@router.get("/{entity}")
def list_entities(
    entity: str,
    q: str = Query(default="", max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
):
    spec = get_entity(entity)
    table = spec["table"]
    params = []
    clause = ""

    if q.strip():
        conditions = [
            f"COALESCE({field}::text, '') ILIKE %s"
            for field in spec["search"]
        ]
        clause = "WHERE (" + " OR ".join(conditions) + ")"
        params = [f"%{q.strip()}%"] * len(conditions)

    offset = (page - 1) * page_size

    with get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*)::int AS total FROM {table} {clause}",
            params
        ).fetchone()["total"]

        rows = conn.execute(
            f"""
                SELECT * FROM {table}
                {clause}
                ORDER BY id DESC
                LIMIT %s OFFSET %s
            """,
            params + [page_size, offset]
        ).fetchall()

    return {
        "items": rows,
        "total": total,
        "page": page,
        "page_size": page_size
    }


@router.post("/{entity}", status_code=201)
def create_entity(entity: str, body: dict[str, Any]):
    spec = get_entity(entity)
    data = prepare_data(spec, body, creating=True)

    columns = list(data)
    column_sql = ", ".join(columns)
    values_sql = ", ".join(["%s"] * len(columns))
    values = [data[column] for column in columns]

    try:
        with get_conn() as conn:
            row = conn.execute(
                f"""
                    INSERT INTO {spec['table']} ({column_sql})
                    VALUES ({values_sql})
                    RETURNING *
                """,
                values
            ).fetchone()
    except psycopg.Error as exc:
        code = exc.sqlstate or "unknown"
        status = 409 if code == "23505" else 400
        raise HTTPException(
            status_code=status,
            detail=f"Не удалось сохранить запись (код {code})"
        ) from exc

    return row


@router.get("/{entity}/{row_id}")
def get_entity_by_id(entity: str, row_id: int):
    spec = get_entity(entity)

    with get_conn() as conn:
        row = conn.execute(
            f"SELECT * FROM {spec['table']} WHERE id = %s",
            [row_id]
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Запись не найдена")

    return row


@router.patch("/{entity}/{row_id}")
def update_entity(entity: str, row_id: int, body: dict[str, Any]):
    spec = get_entity(entity)
    data = prepare_data(spec, body, creating=False)

    assignments = [f"{column} = %s" for column in data]

    if spec["updated"]:
        assignments.append("updated_at = now()")

    values = list(data.values()) + [row_id]

    try:
        with get_conn() as conn:
            row = conn.execute(
                f"""
                    UPDATE {spec['table']}
                    SET {", ".join(assignments)}
                    WHERE id = %s
                    RETURNING *
                """,
                values
            ).fetchone()
    except psycopg.Error as exc:
        code = exc.sqlstate or "unknown"
        status = 409 if code == "23505" else 400
        raise HTTPException(
            status_code=status,
            detail=f"Не удалось обновить запись (код {code})"
        ) from exc

    if not row:
        raise HTTPException(status_code=404, detail="Запись не найдена")

    return row
