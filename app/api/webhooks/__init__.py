from fastapi import APIRouter

from app.api.webhooks import paypal

webhooks_router = APIRouter()

webhooks_router.include_router(paypal.router)
