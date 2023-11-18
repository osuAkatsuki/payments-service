from typing import TYPE_CHECKING

import httpx
from databases import Database

from app import settings
from app.adapters import postgres

if TYPE_CHECKING:
    ...

http = httpx.AsyncClient()
database = Database(
    url=postgres.create_database_url(
        dialect=settings.DB_DIALECT,
        user=settings.DB_USER,
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        database=settings.DB_NAME,
        driver=settings.DB_DRIVER,
        password=settings.DB_PASS,
    ),
)
