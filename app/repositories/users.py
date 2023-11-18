from typing import Any

from app import clients


async def fetch_by_user_id(user_id: int) -> dict[str, Any] | None:
    user = await clients.database.fetch_one(
        query=f"""\
            SELECT *
            FROM users
            WHERE id = :user_id
        """,
        values={"user_id": user_id},
    )
    return dict(user._mapping) if user is not None else None


async def fetch_by_username(username: str) -> dict[str, Any] | None:
    user = await clients.database.fetch_one(
        query=f"""\
            SELECT *
            FROM users
            WHERE username = :username
        """,
        values={"username": username},
    )
    return dict(user._mapping) if user is not None else None


async def partial_update(
    user_id: int,
    donor_expire: int | None = None,
    privileges: int | None = None,
) -> None:
    if donor_expire is None and privileges is None:
        return None

    await clients.database.execute(
        query=f"""\
            UPDATE users
            SET donor_expire = COALESCE(donor_expire, :donor_expire),
                privileges = COALESCE(privileges, :privileges)
            WHERE id = :user_id
        """,
        values={
            "user_id": user_id,
            "donor_expire": donor_expire,
            "privileges": privileges,
        },
    )
    return None
