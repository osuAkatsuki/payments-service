import json
from datetime import datetime
from typing import Any
from typing import TypedDict

from app import clients


class Notification(TypedDict):
    id: int
    transaction_id: str
    created_at: datetime
    last_updated_at: datetime
    notification: dict[str, Any]


async def already_processed(transaction_id: str) -> bool:
    rec = await clients.database.fetch_one(
        query="""\
            SELECT *
              FROM notifications
             WHERE transaction_id = :transaction_id
        """,
        values={"transaction_id": transaction_id},
    )
    return rec is not None


async def insert(transaction_id: str, notification: dict[str, Any]) -> None:
    await clients.database.execute(
        query="""\
            INSERT INTO notifications (transaction_id, notification)
                 VALUES (:transaction_id, :notification)
        """,
        values={
            "transaction_id": transaction_id,
            "notification": json.dumps(notification),
        },
    )
    return None
