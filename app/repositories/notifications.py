import json
from datetime import datetime
from typing import Any
from typing import TypedDict

from pymysql.err import IntegrityError

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
            SELECT 1
              FROM notifications
             WHERE transaction_id = :transaction_id
             LIMIT 1
        """,
        values={"transaction_id": transaction_id},
    )
    return rec is not None


async def claim(transaction_id: str) -> bool:
    try:
        await clients.database.execute(
            "INSERT INTO paypal_ipn_receipts (transaction_id) VALUES (:transaction_id)",
            {"transaction_id": transaction_id},
        )
    except IntegrityError as exc:
        if exc.args[0] != 1062:
            raise
        return False
    return True


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
