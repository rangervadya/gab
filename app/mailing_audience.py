"""Recipient groups computed from saved, sourced company revenue evidence."""
from datetime import date
from decimal import Decimal, InvalidOperation
import re
from urllib.parse import urlsplit

SCOPES = {"all", "revenue_100m"}
THRESHOLD = Decimal("100000000")


def revenue_details(recipient):
    evidence = recipient.get("revenue_evidence") or {}
    if not isinstance(evidence, dict):
        return None
    # An entrepreneur's related company and an unresolved name match do not
    # establish the revenue of this recipient's legal entity.
    if (evidence.get("identity_confirmed") is not True
            or evidence.get("entity_type") != "company"
            or evidence.get("status_code") != "001"
            or not re.fullmatch(r"\d{10}", str(evidence.get("inn", "")))):
        return None
    year = evidence.get("year")
    if isinstance(year, bool) or not isinstance(year, int) or not 2000 <= year < date.today().year:
        return None
    source = evidence.get("source_url")
    if not isinstance(source, str):
        return None
    try:
        url = urlsplit(source)
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            return None
        amount = Decimal(str(evidence.get("amount_rub")))
    except (ValueError, InvalidOperation):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return {"inn": evidence["inn"], "year": year, "approximate": evidence.get("approximate") is True,
            "amount_rub": str(amount), "source_url": source}


def decorate_recipient(recipient):
    details = revenue_details(recipient)
    return {**recipient, "company_revenue": details,
            "revenue_segment_eligible": bool(details and Decimal(details["amount_rub"]) >= THRESHOLD)}


def in_scope(recipient, scope):
    if scope == "all":
        return True
    if scope != "revenue_100m":
        raise ValueError("Unknown recipient group")
    return decorate_recipient(recipient)["revenue_segment_eligible"]
