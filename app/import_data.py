import json
import os
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

BASE = Path(__file__).resolve().parent
DATA_PATH = BASE.parent / "data" / "records.json"
DATABASE_URL = os.environ["DATABASE_URL"]

def main():
    records = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    schema = (BASE / "schema.sql").read_text(encoding="utf-8")
    inserted = 0
    with psycopg.connect(DATABASE_URL) as conn:
        for statement in schema.split(";"):
            if statement.strip():
                conn.execute(statement)
        for payload in records:
            record_id = payload["record_id"]
            review_required = bool(payload.get("review_required"))
            review_state = "pending" if review_required else "not_required"
            conn.execute(
                """
                INSERT INTO gab_review_records (
                    record_id, source_page, source_row, internal_name,
                    counterparty_contacts, address, review_required,
                    review_status, review_reasons, additional_review_note,
                    review_state, reviewer_decision, reviewer_notes,
                    corrected_fields, payload
                ) VALUES (
                    %(record_id)s, %(source_page)s, %(source_row)s, %(internal_name)s,
                    %(counterparty_contacts)s, %(address)s, %(review_required)s,
                    %(review_status)s, %(review_reasons)s, %(additional_review_note)s,
                    %(review_state)s, %(reviewer_decision)s, %(reviewer_notes)s,
                    %(corrected_fields)s, %(payload)s
                )
                ON CONFLICT (record_id) DO NOTHING
                """,
                {
                    "record_id": record_id,
                    "source_page": payload.get("source_page"),
                    "source_row": payload.get("source_row"),
                    "internal_name": payload.get("internal_name"),
                    "counterparty_contacts": payload.get("counterparty_contacts"),
                    "address": payload.get("address"),
                    "review_required": review_required,
                    "review_status": payload.get("review_status"),
                    "review_reasons": payload.get("review_reasons"),
                    "additional_review_note": payload.get("additional_review_note"),
                    "review_state": review_state,
                    "reviewer_decision": payload.get("reviewer_decision"),
                    "reviewer_notes": payload.get("reviewer_notes"),
                    "corrected_fields": Jsonb({}),
                    "payload": Jsonb(payload),
                },
            )
            inserted += 1
        total = conn.execute("SELECT COUNT(*) FROM gab_review_records").fetchone()[0]
    print(f"Import finished. JSON rows: {len(records)}; attempted: {inserted}; table total: {total}.")
    print("Existing record IDs are not overwritten (ON CONFLICT DO NOTHING).")

if __name__ == "__main__":
    main()
