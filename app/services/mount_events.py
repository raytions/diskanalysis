from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text


def log_mount_event(
    db,
    *,
    mount_point: str,
    event_type: str,
    status: str,
    message: str = "",
    mount_id: int | None = None,
    command: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    payload = json.dumps(details or {}, ensure_ascii=False)
    with db.bind.connect() as conn:
        conn.execute(text("""
            INSERT INTO mount_events
                (mount_id, mount_point, event_type, status, message, command, details, created_at)
            VALUES
                (:mount_id, :mount_point, :event_type, :status, :message, :command, :details, datetime('now'))
        """), {
            "mount_id": mount_id,
            "mount_point": mount_point,
            "event_type": event_type,
            "status": status,
            "message": message[:2000],
            "command": command[:4000],
            "details": payload,
        })
        conn.commit()


def list_mount_events(db, mount_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": limit}
    where = ""
    if mount_id is not None:
        where = "WHERE mount_id = :mount_id"
        params["mount_id"] = mount_id
    with db.bind.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT id, mount_id, mount_point, event_type, status, message, command, details, created_at
            FROM mount_events
            {where}
            ORDER BY id DESC
            LIMIT :limit
        """), params).fetchall()
    events = []
    for row in rows:
        try:
            details = json.loads(row[7] or "{}")
        except json.JSONDecodeError:
            details = {}
        events.append({
            "id": row[0],
            "mount_id": row[1],
            "mount_point": row[2],
            "event_type": row[3],
            "status": row[4],
            "message": row[5],
            "command": row[6],
            "details": details,
            "created_at": row[8],
        })
    return events
