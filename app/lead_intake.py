import re
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, HTTPException
from app.crm_api import get_conn

router = APIRouter(prefix="/api/crm", tags=["CRM intake"])


def text_value(value):
    return str(value).strip() if value is not None else ""


def bool_value(value):
    return value is True or str(value).lower() in ("true", "1", "yes")


def number_value(value, label):
    if value is None or str(value).strip() == "":
        return None
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError()
        return result
    except (InvalidOperation, ValueError):
        raise HTTPException(422, f"Некорректное значение: {label}")


@router.post("/intake/lead")
def intake_lead(body: dict[str, Any]):
    if body.get("consent_accepted") is not True:
        raise HTTPException(400, "Не подтверждено согласие на обработку данных")

    name = text_value(body.get("name"))
    if not name:
        raise HTTPException(422, "Не указано имя инвестора")

    phone = text_value(body.get("phone"))
    email = text_value(body.get("email")).lower()
    phone_digits = re.sub(r"[^0-9]", "", phone)

    if not phone_digits and not email:
        raise HTTPException(422, "Нужен телефон или email")

    budget_min = number_value(body.get("budget_min_rub"), "минимальный бюджет")
    budget_max = number_value(body.get("budget_max_rub"), "максимальный бюджет")

    if budget_min is not None and budget_max is not None and budget_min > budget_max:
        raise HTTPException(422, "Минимальный бюджет больше максимального")

    company_name = text_value(body.get("company"))
    inn = re.sub(r"[^0-9]", "", text_value(body.get("inn")))
    city = text_value(body.get("city"))
    asset_title = text_value(body.get("interested_asset"))
    source_funds = text_value(body.get("source_of_funds"))

    horizon = text_value(body.get("investment_horizon"))
    allowed_horizons = {
        "up_to_3_months", "3_to_6_months",
        "6_to_12_months", "over_12_months", "unknown"
    }
    if horizon not in allowed_horizons:
        horizon = "unknown"

    with get_conn() as conn:
        # Блокируем одновременную обработку одинакового телефона/email.
        for key in filter(None, [
            "phone:" + phone_digits if phone_digits else "",
            "email:" + email if email else ""
        ]):
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                [key]
            )

        duplicate_by = []
        matched_ids = []

        if phone_digits:
            row = conn.execute("""
                SELECT id FROM gab_crm_contacts
                WHERE regexp_replace(COALESCE(phone,''), '[^0-9]', '', 'g') = %s
                LIMIT 1
            """, [phone_digits]).fetchone()
            if row:
                duplicate_by.append("phone")
                matched_ids.append(row["id"])

        if email:
            row = conn.execute("""
                SELECT id FROM gab_crm_contacts
                WHERE lower(btrim(COALESCE(email,''))) = %s
                LIMIT 1
            """, [email]).fetchone()
            if row:
                duplicate_by.append("email")
                if row["id"] not in matched_ids:
                    matched_ids.append(row["id"])

        if duplicate_by:
            return {
                "created": False,
                "duplicate": True,
                "duplicate_by": duplicate_by,
                "matched_contact_ids": matched_ids
            }

        # Ищем существующую компанию сначала по ИНН, затем по названию.
        company_id = None

        if inn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ["inn:" + inn]
            )
            row = conn.execute(
                "SELECT id FROM gab_crm_companies WHERE inn = %s LIMIT 1",
                [inn]
            ).fetchone()
            if row:
                company_id = row["id"]

        if company_id is None and company_name:
            row = conn.execute("""
                SELECT id FROM gab_crm_companies
                WHERE lower(btrim(company_name)) = lower(btrim(%s))
                ORDER BY id LIMIT 1
            """, [company_name]).fetchone()
            if row:
                company_id = row["id"]

        company_created = False
        if company_id is None and company_name:
            row = conn.execute("""
                INSERT INTO gab_crm_companies (company_name, inn, city)
                VALUES (%s, %s, %s)
                RETURNING id
            """, [company_name, inn or None, city or None]).fetchone()
            company_id = row["id"]
            company_created = True

        # Связываем новый контакт с компанией.
        contact = conn.execute("""
            INSERT INTO gab_crm_contacts
                (company_id, full_name, phone, email, city,
                 is_decision_maker)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
        """, [
            company_id, name, phone or None, email or None,
            city or None, bool_value(body.get("is_decision_maker"))
        ]).fetchone()

        contact_id = contact["id"]

        # Если объект уже есть в каталоге, связываем его с инвестором.
        asset_id = None
        if asset_title:
            row = conn.execute("""
                SELECT id FROM gab_crm_assets
                WHERE lower(btrim(title)) = lower(btrim(%s))
                AND asset_status <> 'archived'
                ORDER BY id LIMIT 1
            """, [asset_title]).fetchone()
            if row:
                asset_id = row["id"]

        interested = bool_value(body.get("interested_in_asset")) or bool(asset_title)
        within_6_months = (
            bool_value(body.get("horizon_within_6_months"))
            or horizon in ("up_to_3_months", "3_to_6_months")
        )
        has_budget = (
            bool_value(body.get("has_budget_source"))
            or (budget_max is not None and bool(source_funds))
        )

        lead_source = (
            text_value(body.get("source"))
            or text_value(body.get("utm_source"))
            or "web_form"
        )

        notes = "; ".join(
            f"{key}: {text_value(body.get(key))}"
            for key in ("utm_source", "utm_medium", "utm_campaign")
            if text_value(body.get(key))
        )
        notes = (notes + "; " if notes else "") + "Согласие на обработку данных подтверждено."

        motivation = text_value(body.get("motivation"))
        if not motivation and asset_title:
            motivation = "Интерес к объекту: " + asset_title

        investor = conn.execute("""
            INSERT INTO gab_crm_investors (
                name, company_id, primary_contact_id,
                budget_min_rub, budget_max_rub, source_of_funds,
                interested_asset_id, investment_horizon, motivation,
                temperature, response_status, pipeline_stage,
                has_budget_source, interested_in_asset,
                horizon_within_6_months, decision_maker_access,
                agreed_next_step, lead_source, owner, next_step, notes
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                'cold', 'replied', 'new',
                %s, %s, %s, %s, %s, %s, %s,
                'Первичный контакт с инвестором', %s
            )
            RETURNING id, qualification_score, qualified
        """, [
            name, company_id, contact_id,
            budget_min, budget_max, source_funds or None,
            asset_id, horizon, motivation or None,
            has_budget, interested, within_6_months,
            bool_value(body.get("decision_maker_access")),
            bool_value(body.get("agreed_next_step")),
            lead_source, text_value(body.get("owner")) or None, notes
        ]).fetchone()

        conn.execute("""
            INSERT INTO gab_crm_interactions (
                investor_id, company_id, contact_id, asset_id,
                interaction_type, direction, subject, body
            )
            VALUES (%s, %s, %s, %s, 'other', 'inbound', %s, %s)
        """, [
            investor["id"], company_id, contact_id, asset_id,
            "Новая заявка с формы",
            f"Источник: {lead_source}. Согласие на обработку данных подтверждено."
        ])

    return {
        "created": True,
        "duplicate": False,
        "company_created": company_created,
        "company_id": company_id,
        "contact_id": contact_id,
        "investor_id": investor["id"],
        "qualification_score": investor["qualification_score"],
        "qualified": investor["qualified"],
        "temperature": "cold",
        "pipeline_stage": "new"
    }
