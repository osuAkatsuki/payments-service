from fastapi import APIRouter
from fastapi import Response

router = APIRouter()


@router.post("/webhooks/paypal_ipn")
async def process_notification():
    return Response(status_code=200)
