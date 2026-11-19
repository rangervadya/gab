import re
import os
from decimal import Decimal, InvalidOperation
from fastapi import APIRouter, HTTPException
from psycopg.types.json import Jsonb

from app.registry_enrichment import get_conn

router = APIRouter(prefix="/imports", tags=["import-confirmation"])

EMAIL_RE = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.I)


def _text(value):
    return str(value or "").strip()


def _norm(value):
    return re.sub(r"\s+", " ", _text(value)).casefold()


def _digits(value):
    return re.sub(r"[^0-9]", "", _text(value))


def _is_reserved_email(value):
    email = _text(value).lower()
    if "@" not in email:
        return False

    domain = email.rsplit("@", 1)[1]
    reserved_domains = (
        "example.com",
        "example.net",
        "example.org",
        "localhost",
    )

    return (
        domain in reserved_domains
        or any(
            domain == suffix or domain.endswith("." + suffix)
            for suffix in ("invalid", "example", "test", "localhost")
        )
        or any(
            domain == item or domain.endswith("." + item)
            for item in reserved_domains
        )
    )


def _valid_inn(value):
    inn = _digits(value)

    # Reject empty values, wrong lengths and obvious placeholders.
    if len(inn) not in (10, 12) or len(set(inn)) == 1:
        return False

    digits = [int(x) for x in inn]

    if len(inn) == 10:
        weights = (2, 4, 10, 3, 5, 9, 4, 6, 8)
        checksum = sum(
            digits[i] * weights[i] for i in range(9)
        ) % 11 % 10
        return checksum == digits[9]

    weights_11 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
    checksum_11 = sum(
        digits[i] * weights_11[i] for i in range(10)
    ) % 11 % 10

    weights_12 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
    checksum_12 = sum(
        digits[i] * weights_12[i] for i in range(11)
    ) % 11 % 10

    return (
        checksum_11 == digits[10]
        and checksum_12 == digits[11]
    )


def _unique(values, normalizer=lambda x: x):
    result, seen = [], set()
    for value in values:
        value = _text(value)
        if not value:
            continue
        key = normalizer(value)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _row_contacts(row):
    """Contacts supplied by the import document for the named person."""
    emails = row.get("emails")
    phones = row.get("phones")

    emails = list(emails) if isinstance(emails, list) else []
    phones = list(phones) if isinstance(phones, list) else []

    emails.append(row.get("email"))
    phones.append(row.get("phone"))

    emails = _unique(
        [x for x in emails if EMAIL_RE.fullmatch(_text(x))],
        lambda x: x.lower()
    )
    phones = _unique(
        [x for x in phones if 7 <= len(_digits(x)) <= 15],
        _digits
    )

    return [x.lower() for x in emails], phones


def _row_company_contacts(row):
    """Confirmed enrichment contacts belong to the company, not a person."""
    emails = row.get("enriched_emails")
    phones = row.get("enriched_phones")

    emails = list(emails) if isinstance(emails, list) else []
    phones = list(phones) if isinstance(phones, list) else []

    # If the import row has no named person, its document contacts
    # are treated as company-level contacts.
    if not _text(row.get("full_name")):
        document_emails, document_phones = _row_contacts(row)
        emails.extend(document_emails)
        phones.extend(document_phones)

    emails = _unique(
        [x for x in emails if EMAIL_RE.fullmatch(_text(x))],
        lambda x: x.lower()
    )
    phones = _unique(
        [x for x in phones if 7 <= len(_digits(x)) <= 15],
        _digits
    )

    return [x.lower() for x in emails], phones


def _load_row(conn, batch_id, row_id, lock=False):
    sql = """
        SELECT
            r.*,
            e.emails AS enriched_emails,
            e.phones AS enriched_phones
        FROM gab_crm_import_rows r
        LEFT JOIN LATERAL (
            SELECT
                COALESCE(
                    (
                        SELECT jsonb_agg(DISTINCT email_value.value)
                        FROM gab_crm_import_enrichment x
                        CROSS JOIN LATERAL
                            jsonb_array_elements_text(
                                COALESCE(x.emails, '[]'::jsonb)
                            ) AS email_value(value)
                        WHERE x.import_row_id = r.id
                          AND x.match_status = 'confirmed'
                    ),
                    '[]'::jsonb
                ) AS emails,
                COALESCE(
                    (
                        SELECT jsonb_agg(DISTINCT phone_value.value)
                        FROM gab_crm_import_enrichment x
                        CROSS JOIN LATERAL
                            jsonb_array_elements_text(
                                COALESCE(x.phones, '[]'::jsonb)
                            ) AS phone_value(value)
                        WHERE x.import_row_id = r.id
                          AND x.match_status = 'confirmed'
                    ),
                    '[]'::jsonb
                ) AS phones
        ) e ON TRUE
        WHERE r.batch_id = %s AND r.id = %s
    """
    if lock:
        sql += " FOR UPDATE OF r"

    row = conn.execute(sql, [batch_id, row_id]).fetchone()
    if not row:
        raise HTTPException(404, "Строка импорта не найдена")
    return dict(row)


def _analyze(conn, row, decisions=None):
    decisions = decisions or {}
    company_name = _text(row.get("company_name"))
    inn = _text(row.get("inn"))
    full_name = _text(row.get("full_name"))
    emails, phones = _row_contacts(row)
    company_emails, company_phones = _row_company_contacts(row)
    all_emails = _merge(emails, company_emails, lambda x: x.lower())
    all_phones = _merge(phones, company_phones, _digits)

    reasons = []

    if not company_name and not full_name:
        reasons.append("identity_missing")

    if not all_emails and not all_phones:
        reasons.append("no_contact_details_requires_review")

    # Reserved test domains are not valid production mailing destinations.
    if any(_is_reserved_email(email) for email in all_emails):
        reasons.append("reserved_test_email_domain_requires_review")

    # Revenue eligibility is a mandatory condition for investor creation.
    if _text(row.get("revenue_status")).lower() != "eligible":
        reasons.append("revenue_status_not_eligible")

    raw_revenue = row.get("revenue_rub")
    revenue = None
    try:
        if raw_revenue is not None:
            revenue = Decimal(str(raw_revenue))
            if not revenue.is_finite():
                revenue = None
    except (InvalidOperation, TypeError, ValueError):
        revenue = None

    if revenue is None:
        reasons.append("revenue_amount_missing_or_invalid")
    elif revenue < Decimal("100000000"):
        reasons.append("revenue_below_100m")

    raw_year = row.get("revenue_year")
    try:
        revenue_year = int(raw_year) if raw_year is not None else None
    except (TypeError, ValueError):
        revenue_year = None

    if revenue_year is None or not 1900 <= revenue_year <= 2100:
        reasons.append("revenue_year_missing_or_invalid")

    if not _text(row.get("revenue_source")):
        reasons.append("revenue_source_missing")

    extraction_warnings = row.get("extraction_warnings")
    if extraction_warnings:
        reasons.append("extraction_warnings_require_manual_review")

    # Company candidates: INN is stronger than a name-only match.
    by_inn = []
    by_name = []

    if inn:
        if not _valid_inn(inn):
            reasons.append("invalid_inn_requires_review")
        else:
            by_inn = conn.execute(
                """
                SELECT id, company_name, inn, city
                FROM gab_crm_companies
                WHERE inn = %s
                ORDER BY id
                """,
                [_digits(inn)]
            ).fetchall()
    else:
        reasons.append("inn_missing_requires_review")

    if company_name:
        by_name = conn.execute(
            """
            SELECT id, company_name, inn, city
            FROM gab_crm_companies
            WHERE regexp_replace(
                lower(btrim(company_name)),
                '[[:space:]]+', ' ', 'g'
            ) = %s
            ORDER BY id
            LIMIT 20
            """,
            [_norm(company_name)]
        ).fetchall()

    company_map = {x["id"]: dict(x) for x in list(by_inn) + list(by_name)}
    company_candidates = list(company_map.values())

    company_id = None

    if len(by_inn) == 1:
        company_id = by_inn[0]["id"]
        if any(x["id"] != company_id for x in by_name):
            reasons.append("inn_and_company_name_point_to_different_companies")
    elif len(by_inn) > 1:
        reasons.append("multiple_companies_with_same_inn")
    elif len(by_name) > 1:
        reasons.append("multiple_companies_with_same_name")
    elif len(by_name) == 1:
        existing_inn = _text(by_name[0]["inn"])
        if inn and existing_inn and existing_inn != inn:
            reasons.append("company_name_match_has_different_inn")
        else:
            reasons.append("company_name_match_requires_confirmation")

    if 'company_id' in decisions:
        selected = decisions['company_id']
        if selected is None:
            if by_inn:
                raise HTTPException(409, 'Компания с этим ИНН уже существует')
            company_id = None
        elif type(selected) is not int or selected not in company_map:
            raise HTTPException(422, 'Выберите компанию из найденных кандидатов')
        else:
            candidate = company_map[selected]
            if inn and candidate.get('inn') and _digits(candidate['inn']) != _digits(inn):
                raise HTTPException(409, 'ИНН выбранной компании отличается')
            company_id = selected

    # Match contacts by exact email or normalized phone, across CRM.
    contact_candidates = []

    if full_name and (emails or phones):
        contact_candidates = conn.execute(
            """
            SELECT DISTINCT
                c.id, c.company_id, c.full_name,
                c.email, c.phone, c.position
            FROM gab_crm_contacts c
            WHERE
                lower(btrim(COALESCE(c.email, ''))) = ANY(%s)
                OR EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements_text(
                        COALESCE(c.emails, '[]'::jsonb)
                    ) e
                    WHERE lower(btrim(e)) = ANY(%s)
                )
                OR regexp_replace(
                    COALESCE(c.phone, ''), '[^0-9]', '', 'g'
                ) = ANY(%s)
                OR EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements_text(
                        COALESCE(c.phones, '[]'::jsonb)
                    ) p
                    WHERE regexp_replace(p, '[^0-9]', '', 'g') = ANY(%s)
                )
            ORDER BY c.id
            LIMIT 30
            """,
            [
                emails, emails,
                [_digits(x) for x in phones],
                [_digits(x) for x in phones]
            ]
        ).fetchall()

    contact_candidates = [dict(x) for x in contact_candidates]
    contact_id = None

    if len(contact_candidates) > 1:
        reasons.append("multiple_contacts_match_email_or_phone")
    elif len(contact_candidates) == 1:
        candidate = contact_candidates[0]

        candidate_company_id = candidate["company_id"]

        if candidate_company_id is not None:
            if company_id is None and company_name:
                reasons.append(
                    "contact_company_not_matched_to_import_company"
                )
            elif (
                company_id is not None
                and candidate_company_id != company_id
            ):
                reasons.append("contact_belongs_to_different_company")
            elif company_id is None and not company_name:
                company_id = candidate_company_id

        if (
            full_name
            and candidate["full_name"]
            and _norm(full_name) != _norm(candidate["full_name"])
        ):
            reasons.append("contact_name_conflicts_with_matched_email_or_phone")

        if not reasons or all(
            x in ("identity_missing", "no_contact_details_requires_review")
            for x in reasons
        ):
            contact_id = candidate["id"]

    # Name-only contact matches are suggestions, never automatic merges.
    if (
        not contact_candidates
        and full_name
        and company_id
    ):
        named = conn.execute(
            """
            SELECT id, company_id, full_name, email, phone, position
            FROM gab_crm_contacts
            WHERE company_id = %s
              AND lower(btrim(full_name)) = %s
            ORDER BY id
            LIMIT 20
            """,
            [company_id, _norm(full_name)]
        ).fetchall()

        if named:
            contact_candidates = [dict(x) for x in named]
            reasons.append("contact_name_match_requires_confirmation")

    if 'contact_id' in decisions:
        selected = decisions['contact_id']
        candidates = {c['id']: c for c in contact_candidates}
        if selected is None:
            contact_id = None
        elif type(selected) is not int or selected not in candidates:
            raise HTTPException(422, 'Выберите контакт из найденных кандидатов')
        elif candidates[selected]['company_id'] not in (None, company_id):
            raise HTTPException(409, 'Контакт принадлежит другой компании')
        else:
            contact_id = selected

    # Find investor candidates by company/name or matched primary contact.
    target_name = full_name or company_name
    investor_candidates = []

    clauses = []
    params = []

    if company_id and target_name:
        clauses.append(
            "(company_id = %s AND lower(btrim(name)) = %s)"
        )
        params.extend([company_id, _norm(target_name)])

    if contact_id:
        clauses.append("primary_contact_id = %s")
        params.append(contact_id)

    if clauses:
        investor_candidates = conn.execute(
            """
            SELECT id, name, company_id, primary_contact_id
            FROM gab_crm_investors
            WHERE """ + " OR ".join(clauses) + """
            ORDER BY id
            LIMIT 20
            """,
            params
        ).fetchall()

    investor_candidates = [dict(x) for x in investor_candidates]
    investor_id = None

    if len(investor_candidates) > 1:
        reasons.append("multiple_investors_match")
    elif len(investor_candidates) == 1:
        candidate = investor_candidates[0]
        if (
            contact_id
            and candidate["primary_contact_id"] is not None
            and candidate["primary_contact_id"] != contact_id
        ):
            reasons.append("investor_has_another_primary_contact")
        else:
            investor_id = candidate["id"]

    if 'investor_id' in decisions:
        selected = decisions['investor_id']
        candidates = {c['id']: c for c in investor_candidates}
        if selected is None:
            investor_id = None
        elif type(selected) is not int or selected not in candidates:
            raise HTTPException(422, 'Выберите инвестора из найденных кандидатов')
        elif candidates[selected]['company_id'] not in (None, company_id) or candidates[selected]['primary_contact_id'] not in (None, contact_id):
            raise HTTPException(409, 'Связи выбранного инвестора отличаются')
        else:
            investor_id = selected

    return {
        "emails_found": len(emails),
        "phones_found": len(phones),
        "company_emails_found": len(company_emails),
        "company_phones_found": len(company_phones),
        "company_candidates": company_candidates,
        "contact_candidates": contact_candidates,
        "investor_candidates": investor_candidates,
        "suggested_company_id": company_id,
        "suggested_contact_id": contact_id,
        "suggested_investor_id": investor_id,
        "reasons": list(dict.fromkeys(reasons)),
    }


def _merge(existing, additions, normalize=lambda x: x):
    return _unique(
        list(existing or []) + list(additions or []),
        normalize
    )


def _mark_review(conn, row, analysis):
    details = {
        "reasons": analysis["reasons"],
        "company_candidates": [
            x["id"] for x in analysis["company_candidates"]
        ],
        "contact_candidates": [
            x["id"] for x in analysis["contact_candidates"]
        ],
        "investor_candidates": [
            x["id"] for x in analysis["investor_candidates"]
        ],
    }

    reason_text = "; ".join(analysis["reasons"])
    old_notes = _text(row.get("notes"))

    if reason_text and reason_text not in old_notes:
        combined_notes = (
            old_notes + "\n" + reason_text
        ).strip() if old_notes else reason_text
    else:
        combined_notes = old_notes or reason_text

    conn.execute(
        """
        UPDATE gab_crm_import_rows
        SET import_status = 'needs_review',
            notes = %s,
            updated_at = now()
        WHERE id = %s
        """,
        [combined_notes, row["id"]]
    )

    conn.execute(
        """
        INSERT INTO gab_crm_import_events
            (import_row_id, event_type, detail_json)
        SELECT %s, 'needs_review', %s
        WHERE NOT EXISTS (
            SELECT 1
            FROM gab_crm_import_events
            WHERE import_row_id = %s
              AND event_type = 'needs_review'
              AND detail_json = %s
        )
        """,
        [
            row["id"], Jsonb(details),
            row["id"], Jsonb(details)
        ]
    )

    return {
        "status": "needs_review",
        "row_id": row["id"],
        **analysis
    }


def _acknowledgeable(decisions):
    resolvable = {'extraction_warnings_require_manual_review'}
    if 'company_id' in decisions:
        resolvable |= {'company_name_match_requires_confirmation', 'multiple_companies_with_same_name',
                       'inn_and_company_name_point_to_different_companies'}
    if 'contact_id' in decisions:
        resolvable |= {'contact_name_match_requires_confirmation', 'multiple_contacts_match_email_or_phone',
                       'contact_name_conflicts_with_matched_email_or_phone', 'contact_company_not_matched_to_import_company'}
    if 'investor_id' in decisions:
        resolvable |= {'multiple_investors_match', 'investor_has_another_primary_contact'}
    return resolvable


@router.get("/{batch_id}/rows/{row_id}/match-preview")
def import_match_preview(batch_id: int, row_id: int):
    return preview_import_decisions(batch_id, row_id, {})


@router.post("/{batch_id}/rows/{row_id}/match-preview")
def preview_import_decisions(batch_id: int, row_id: int, body: dict):
    with get_conn() as conn:
        row = _load_row(conn, batch_id, row_id)
        decisions = body.get("decisions", {})
        if not isinstance(decisions, dict) or set(decisions) - {"company_id", "contact_id", "investor_id"}:
            raise HTTPException(422, "Некорректные решения")
        analysis = _analyze(conn, row, decisions)

    return {
        "batch_id": batch_id,
        "row_id": row_id,
        "version": row.get("edit_version", 0),
        "row": row,
        "plan": {
            "company": "match" if analysis["suggested_company_id"] else "create" if row.get("company_name") else "none",
            "contact": "match" if analysis["suggested_contact_id"] else "create" if row.get("full_name") else "none",
            "investor": "match" if analysis["suggested_investor_id"] else "create",
            "merge": "Дополняются пустые поля и списки email/телефонов. Заполненные основные поля сохраняются.",
        },
        "import_status": row["import_status"],
        "company_name": row["company_name"],
        "inn": row["inn"],
        "full_name": row["full_name"],
        "emails_found": analysis["emails_found"],
        "phones_found": analysis["phones_found"],
        "company_emails_found": analysis["company_emails_found"],
        "company_phones_found": analysis["company_phones_found"],
        "company_candidates": analysis["company_candidates"],
        "contact_candidates": analysis["contact_candidates"],
        "investor_candidates": analysis["investor_candidates"],
        "suggested_company_id": analysis["suggested_company_id"],
        "suggested_contact_id": analysis["suggested_contact_id"],
        "suggested_investor_id": analysis["suggested_investor_id"],
        "reasons": analysis["reasons"],
        "acknowledgeable_reasons": [r for r in analysis["reasons"] if r in _acknowledgeable(decisions)],
        "can_auto_confirm": not analysis["reasons"],
    }


@router.post("/{batch_id}/rows/{row_id}/confirm")
def confirm_import_row(batch_id: int, row_id: int, body: dict | None = None):
    body = body or {}
    with get_conn() as conn:
        row = _load_row(conn, batch_id, row_id, lock=True)

        if row["import_status"] == "rejected":
            raise HTTPException(409, "Строка импорта отклонена")

        if row["import_status"] in ("created", "matched"):
            if row["investor_id"] is not None:
                return {
                    "status": row["import_status"],
                    "already_confirmed": True,
                    "row_id": row_id,
                    "company_id": row["company_id"],
                    "contact_id": row["contact_id"],
                    "investor_id": row["investor_id"],
                }
            raise HTTPException(
                409,
                "Строка помечена подтверждённой, но investor_id отсутствует"
            )

        if row["import_status"] not in ("pending", "needs_review"):
            raise HTTPException(
                409,
                f"Нельзя подтверждать строку со статусом {row['import_status']}"
            )

        # Serialize concurrent imports sharing the same strong identifiers.
        lock_keys = []
        if _valid_inn(row.get("inn")):
            lock_keys.append("inn:" + _digits(row["inn"]))

        lock_emails, lock_phones = _row_contacts(row)
        company_lock_emails, company_lock_phones = (
            _row_company_contacts(row)
        )
        lock_keys.extend(
            "email:" + x.lower()
            for x in lock_emails + company_lock_emails
        )
        lock_keys.extend(
            "phone:" + _digits(x)
            for x in lock_phones + company_lock_phones
        )

        for key in sorted(set(lock_keys)):
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                [key]
            )

        if body and body.get('version') != row.get('edit_version'):
            raise HTTPException(409, 'Данные изменились. Повторите предпросмотр')
        decisions = body.get('decisions', {})
        if not isinstance(decisions, dict) or set(decisions) - {'company_id', 'contact_id', 'investor_id'}:
            raise HTTPException(422, 'Некорректные решения о совпадениях')
        analysis = _analyze(conn, row, decisions)
        expected_plan = body.get('expected_plan')
        if expected_plan is not None:
            actual_plan = {key: analysis['suggested_' + key] for key in ('company_id', 'contact_id', 'investor_id')}
            if expected_plan != actual_plan:
                raise HTTPException(409, 'Совпадения изменились. Повторите предпросмотр')
        original_reasons = list(analysis['reasons'])
        acknowledged = body.get('acknowledged_warnings', [])
        if not isinstance(acknowledged, list) or any(not isinstance(x, str) for x in acknowledged):
            raise HTTPException(422, 'Некорректный список предупреждений')
        # Only these nonfinancial warnings can be explicitly accepted. Financial
        # requirements, invalid identifiers and contradictory links stay blocking.
        resolvable = _acknowledgeable(decisions)
        analysis['reasons'] = [r for r in original_reasons if not (r in resolvable and r in acknowledged)]

        if analysis["reasons"]:
            return _mark_review(conn, row, analysis)

        emails, phones = _row_contacts(row)
        company_emails, company_phones = _row_company_contacts(row)
        company_name = _text(row.get("company_name"))
        full_name = _text(row.get("full_name"))
        inn = _text(row.get("inn"))
        city = _text(row.get("city"))

        company_id = analysis["suggested_company_id"]
        contact_id = analysis["suggested_contact_id"]
        investor_id = analysis["suggested_investor_id"]

        existed = bool(company_id or contact_id or investor_id)

        # Create a company only when no unambiguous existing one was found.
        if company_name and company_id is None:
            company = conn.execute(
                """
                INSERT INTO gab_crm_companies (
                    company_name, inn, city,
                    general_email, general_phone,
                    emails, phones,
                    revenue_rub, revenue_year, revenue_source,
                    revenue_checked_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        CASE WHEN %s IS NULL THEN NULL ELSE now() END)
                RETURNING id
                """,
                [
                    company_name, inn or None, city or None,
                    company_emails[0] if company_emails else None,
                    company_phones[0] if company_phones else None,
                    Jsonb(company_emails), Jsonb(company_phones),
                    row["revenue_rub"], row["revenue_year"],
                    row["revenue_source"], row["revenue_rub"]
                ]
            ).fetchone()
            company_id = company["id"]

        elif company_id is not None:
            company = conn.execute(
                """
                SELECT * FROM gab_crm_companies
                WHERE id = %s
                FOR UPDATE
                """,
                [company_id]
            ).fetchone()

            # company_emails/company_phones were prepared separately
            # from personal contacts before entering this branch.
            merged_emails = _merge(
                list(company["emails"] or []) + (
                    [company["general_email"]]
                    if company["general_email"] else []
                ),
                company_emails,
                lambda x: x.lower()
            )
            merged_phones = _merge(
                list(company["phones"] or []) + (
                    [company["general_phone"]]
                    if company["general_phone"] else []
                ),
                company_phones,
                _digits
            )

            conn.execute(
                """
                UPDATE gab_crm_companies
                SET
                    city = COALESCE(NULLIF(city, ''), %s),
                    inn = COALESCE(NULLIF(inn, ''), %s),
                    general_email = COALESCE(
                        NULLIF(general_email, ''), %s
                    ),
                    general_phone = COALESCE(
                        NULLIF(general_phone, ''), %s
                    ),
                    emails = %s,
                    phones = %s,
                    revenue_rub = COALESCE(revenue_rub, %s),
                    revenue_year = COALESCE(revenue_year, %s),
                    revenue_source = COALESCE(revenue_source, %s),
                    revenue_checked_at = CASE
                        WHEN revenue_rub IS NULL AND %s IS NOT NULL
                        THEN now()
                        ELSE revenue_checked_at
                    END,
                    updated_at = now()
                WHERE id = %s
                """,
                [
                    city or None, inn or None,
                    company_emails[0] if company_emails else None,
                    company_phones[0] if company_phones else None,
                    Jsonb(merged_emails), Jsonb(merged_phones),
                    row["revenue_rub"], row["revenue_year"],
                    row["revenue_source"], row["revenue_rub"],
                    company_id
                ]
            )

        # Create or update a contact only when a person's name is supplied.
        if full_name and contact_id is None:
            contact = conn.execute(
                """
                INSERT INTO gab_crm_contacts (
                    company_id, full_name, position, phone, email, city,
                    emails, phones
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                [
                    company_id, full_name, row["position"],
                    phones[0] if phones else None,
                    emails[0] if emails else None,
                    city or None, Jsonb(emails), Jsonb(phones)
                ]
            ).fetchone()
            contact_id = contact["id"]

        elif contact_id is not None:
            contact = conn.execute(
                """
                SELECT * FROM gab_crm_contacts
                WHERE id = %s
                FOR UPDATE
                """,
                [contact_id]
            ).fetchone()

            merged_emails = _merge(
                list(contact["emails"] or []) + (
                    [contact["email"]] if contact["email"] else []
                ),
                emails,
                lambda x: x.lower()
            )
            merged_phones = _merge(
                list(contact["phones"] or []) + (
                    [contact["phone"]] if contact["phone"] else []
                ),
                phones,
                _digits
            )

            conn.execute(
                """
                UPDATE gab_crm_contacts
                SET
                    company_id = COALESCE(company_id, %s),
                    position = COALESCE(NULLIF(position, ''), %s),
                    city = COALESCE(NULLIF(city, ''), %s),
                    email = COALESCE(NULLIF(email, ''), %s),
                    phone = COALESCE(NULLIF(phone, ''), %s),
                    emails = %s,
                    phones = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                [
                    company_id, row["position"], city or None,
                    emails[0] if emails else None,
                    phones[0] if phones else None,
                    Jsonb(merged_emails), Jsonb(merged_phones),
                    contact_id
                ]
            )

        # Create or link the investor card.
        target_name = full_name or company_name
        if investor_id is None:
            investor = conn.execute(
                """
                INSERT INTO gab_crm_investors (
                    name, company_id, primary_contact_id,
                    lead_source, notes
                )
                VALUES (%s, %s, %s, 'document_import', %s)
                RETURNING id
                """,
                [
                    target_name,
                    company_id,
                    contact_id,
                    f"Импорт из пакета {batch_id}; строка {row_id}"
                ]
            ).fetchone()
            investor_id = investor["id"]
        else:
            conn.execute(
                """
                UPDATE gab_crm_investors
                SET
                    company_id = COALESCE(company_id, %s),
                    primary_contact_id = COALESCE(primary_contact_id, %s),
                    updated_at = now()
                WHERE id = %s
                """,
                [company_id, contact_id, investor_id]
            )

        result_status = "matched" if existed else "created"

        conn.execute(
            """
            UPDATE gab_crm_import_rows
            SET
                company_id = %s,
                contact_id = %s,
                investor_id = %s,
                import_status = %s,
                confirmed_at = now(),
                updated_at = now()
            WHERE id = %s
            """,
            [
                company_id, contact_id, investor_id,
                result_status, row_id
            ]
        )

        conn.execute(
            """
            INSERT INTO gab_crm_import_events
                (import_row_id, event_type, detail_json)
            VALUES (%s, %s, %s)
            """,
            [
                row_id,
                "confirmed_matched" if existed else "confirmed_created",
                Jsonb({
                    "company_id": company_id,
                    "contact_id": contact_id,
                    "investor_id": investor_id,
                    "actor": os.environ.get("GAB_APP_USER", "unknown"),
                    "decisions": decisions,
                    "acknowledged_warnings": [r for r in original_reasons if r in acknowledged],
                    "manual_overrides": row.get("manual_overrides", {}),
                    "email_count": len(emails),
                    "phone_count": len(phones),
                })
            ]
        )

    return {
        "status": result_status,
        "already_confirmed": False,
        "row_id": row_id,
        "company_id": company_id,
        "contact_id": contact_id,
        "investor_id": investor_id,
        "email_count": len(emails),
        "phone_count": len(phones),
    }
