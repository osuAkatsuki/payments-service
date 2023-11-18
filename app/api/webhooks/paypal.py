from fastapi import APIRouter
from fastapi import Response
from fastapi import Request

from app import clients
from app import settings
import logging


router = APIRouter()

PAYPAL_VERIFY_URL_PROD = "https://ipnpb.paypal.com/cgi-bin/webscr"
PAYPAL_VERIFY_URL_TEST = "https://ipnpb.sandbox.paypal.com/cgi-bin/webscr"

PAYPAL_VERIFY_URL = (
    PAYPAL_VERIFY_URL_PROD
    if settings.APP_ENV == "production"
    else PAYPAL_VERIFY_URL_TEST
)


@router.post("/webhooks/paypal_ipn")
async def process_notification(request: Request):
    response = await clients.http.post(
        url=PAYPAL_VERIFY_URL,
        params=dict(request.query_params) | {"cmd": "_notify-validate"},
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "user-agent": "Python-IPN-Verification-Script",
        },
    )
    response.raise_for_status()

    if response.text == "VERIFIED":
        logging.info(
            "PayPal IPN verified",
            extra={
                "query_params": dict(request.query_params),
                "request_body": await request.body(),
            },
        )
    elif response.text == "INVALID":
        logging.warning(
            "PayPal IPN invalid",
            extra={
                "query_params": dict(request.query_params),
                "request_body": await request.body(),
            },
        )
    else:
        logging.error(
            "PayPal IPN verification status unknown",
            extra={
                "response_text": response.text,
                "query_params": dict(request.query_params),
                "request_body": await request.body(),
            },
        )
        return Response(status_code=400)

    return Response(status_code=200)
