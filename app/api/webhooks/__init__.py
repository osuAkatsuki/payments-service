from fastapi import APIRouter

from app.api.webhooks import paypal
from app.api.webhooks import stripe

webhooks_router = APIRouter()

webhooks_router.include_router(paypal.router)
webhooks_router.include_router(stripe.router)
