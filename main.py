#!/usr/bin/env python3
import atexit
from contextlib import asynccontextmanager
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI

import app.clients
import app.exception_handling
import app.logging
from app import settings
from app.api.webhooks import webhooks_router


@asynccontextmanager
async def lifespan(asgi_app: FastAPI) -> AsyncIterator[None]:
    try:
        await app.clients.database.connect()
        yield
    finally:
        await app.clients.database.disconnect()


asgi_app = FastAPI(lifespan=lifespan)


@asgi_app.get("/_health")
async def health():
    return {"status": "ok"}


asgi_app.include_router(webhooks_router)


def main() -> int:
    app.logging.configure_logging()

    app.exception_handling.hook_exception_handlers()
    atexit.register(app.exception_handling.unhook_exception_handlers)

    uvicorn.run(
        "main:asgi_app",
        reload=settings.CODE_HOTRELOAD,
        server_header=False,
        date_header=False,
        host=settings.APP_HOST,
        port=settings.APP_PORT,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
