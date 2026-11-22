from typing import cast
from typing import TypedDict

from app import clients


class UserBadge(TypedDict):
    user: int
    badge: int


async def fetch_all(user_id: int, *, for_update: bool = False) -> list[UserBadge]:
    lock_clause = "FOR UPDATE" if for_update else ""
    recs = await clients.database.fetch_all(
        query=f"""\
            SELECT user, badge
              FROM user_badges
             WHERE user = :user_id
             {lock_clause}
        """,
        values={"user_id": user_id},
    )
    return cast(list[UserBadge], recs)


async def delete_by_user_id(user_id: int) -> None:
    await clients.database.execute(
        query="""\
            DELETE FROM user_badges
                  WHERE user = :user_id
        """,
        values={"user_id": user_id},
    )


async def insert(user_id: int, badge_id: int) -> None:
    await clients.database.execute(
        query="""\
            INSERT INTO user_badges (user, badge)
                 VALUES (:user_id, :badge_id)
        """,
        values={"user_id": user_id, "badge_id": badge_id},
    )
