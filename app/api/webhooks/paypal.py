from fastapi import APIRouter
from fastapi import Response
from fastapi import Request

from app import clients
from app import settings
import urllib.parse
import logging


router = APIRouter()

# PAYPAL_VERIFY_URL_PROD = "https://ipnpb.paypal.com/cgi-bin/webscr"
PAYPAL_VERIFY_URL_TEST = "https://ipnpb.sandbox.paypal.com/cgi-bin/webscr"

PAYPAL_VERIFY_URL = (
    # PAYPAL_VERIFY_URL_PROD
    # if settings.APP_ENV == "production"
    # else
    PAYPAL_VERIFY_URL_TEST
)


@router.post("/webhooks/paypal_ipn")
async def process_notification(request: Request):
    response = await clients.http.post(
        url=PAYPAL_VERIFY_URL,
        params=(
            urllib.parse.parse_qs((await request.body()).decode())
            | {"cmd": "_notify-validate"}
        ),
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
                "request_body": await request.json(),
            },
        )
    elif response.text == "INVALID":
        logging.warning(
            "PayPal IPN invalid",
            extra={
                "request_body": await request.json(),
            },
        )
    else:
        logging.error(
            "PayPal IPN verification status unknown",
            extra={
                "response_text": response.text,
                "request_body": await request.json(),
            },
        )
        return Response(status_code=400)

    return Response(status_code=200)
