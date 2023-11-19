from typing import cast
from typing import TypedDict

from app import clients


class User(TypedDict):
    id: int
    username: str
    username_safe: str
    password_md5: str
    salt: str
    email: str
    register_datetime: int
    achievements_version: int
    latest_activity: int
    silence_end: int
    silence_reason: str
    password_version: int
    privileges: int
    donor_expire: int
    frozen: int
    flags: int
    notes: str
    aqn: int
    ban_datetime: int
    switch_notifs: int
    previous_overwrite: int
    whitelist: int
    clan_id: int
    clan_privileges: int
    userpage_allowed: int
    converted: int
    freeze_reason: str


async def fetch_by_user_id(user_id: int) -> User | None:
    user = await clients.database.fetch_one(
        query=f"""\
            SELECT *
            FROM users
            WHERE id = :user_id
        """,
        values={"user_id": user_id},
    )
    return cast(User, dict(user._mapping)) if user is not None else None


async def fetch_by_username(username: str) -> User | None:
    user = await clients.database.fetch_one(
        query=f"""\
            SELECT *
            FROM users
            WHERE username = :username
        """,
        values={"username": username},
    )
    return cast(User, dict(user._mapping)) if user is not None else None


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
            SET donor_expire = COALESCE(:donor_expire, donor_expire),
                privileges = COALESCE(:privileges, privileges)
            WHERE id = :user_id
        """,
        values={
            "user_id": user_id,
            "donor_expire": donor_expire,
            "privileges": privileges,
        },
    )
    return None
