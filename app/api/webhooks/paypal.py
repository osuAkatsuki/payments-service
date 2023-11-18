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
    request_body = await request.body()
    request_data = urllib.parse.parse_qsl(request_body)

    print("req body raw", request_body)
    response = await clients.http.post(
        url=PAYPAL_VERIFY_URL,
        headers={"content-type": "application/x-www-form-urlencoded"},
        params=[("cmd", "_notify-validate")] + request_data,  # type: ignore
    )
    response.raise_for_status()

    if response.text == "VERIFIED":
        logging.info("PayPal IPN verified", extra={"request_data": request_data})
    elif response.text == "INVALID":
        logging.warning(
            "PayPal IPN invalid",
            extra={
                "response_text": response.text,
                "request_data": request_data,
            },
        )
        # fallthrough (do not let the client know of the invalidity)
    else:
        logging.error(
            "PayPal IPN verification status unknown",
            extra={
                "response_text": response.text,
                "request_data": request_data,
            },
        )
        return Response(status_code=400)

    return Response(status_code=200)
