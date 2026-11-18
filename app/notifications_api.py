from fastapi import APIRouter, HTTPException, Query

from app.crm_api import get_conn

router = APIRouter(
    prefix="/api/crm/notifications",
    tags=["CRM notifications"]
)


@router.get("")
def list_notifications(
    limit: int = Query(default=30, ge=1, le=100),
    unread_only: bool = False
):
    with get_conn() as conn:
        unread_count = conn.execute("""
            SELECT COUNT(*)::int AS total
            FROM gab_crm_notifications
            WHERE read_at IS NULL
        """).fetchone()["total"]

        where = "WHERE n.read_at IS NULL" if unread_only else ""

        items = conn.execute(f"""
            SELECT
                n.id,
                n.investor_id,
                n.event_type,
                n.title,
                n.message,
                n.created_at,
                n.read_at,
                i.name AS investor_name,
                i.temperature,
                i.qualification_score,
                i.qualified
            FROM gab_crm_notifications n
            JOIN gab_crm_investors i ON i.id = n.investor_id
            {where}
            ORDER BY n.created_at DESC, n.id DESC
            LIMIT %s
        """, [limit]).fetchall()

    return {
        "unread_count": unread_count,
        "items": items
    }


@router.patch("/{notification_id}/read")
def mark_notification_read(notification_id: int):
    with get_conn() as conn:
        row = conn.execute("""
            UPDATE gab_crm_notifications
            SET read_at = COALESCE(read_at, now())
            WHERE id = %s
            RETURNING id, read_at
        """, [notification_id]).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Уведомление не найдено")

    return {"saved": True, **row}


@router.post("/read-all")
def mark_all_notifications_read():
    with get_conn() as conn:
        result = conn.execute("""
            UPDATE gab_crm_notifications
            SET read_at = now()
            WHERE read_at IS NULL
        """)

        updated = result.rowcount

    return {"saved": True, "updated": updated}
