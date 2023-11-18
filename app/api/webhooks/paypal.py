from fastapi import APIRouter
from fastapi import Response
from fastapi import Request

from app import clients
from app import settings

import json
import urllib.parse
import logging


router = APIRouter()

PAYPAL_VERIFY_URL_PROD = "https://ipnpb.paypal.com/cgi-bin/webscr"
PAYPAL_VERIFY_URL_TEST = "https://ipnpb.sandbox.paypal.com/cgi-bin/webscr"

PAYPAL_VERIFY_URL = (
    PAYPAL_VERIFY_URL_PROD
    if settings.APP_ENV == "production"
    else PAYPAL_VERIFY_URL_TEST
)

ACCEPTED_CURRENCIES = {"EUR"}


@router.post("/webhooks/paypal_ipn")
async def process_notification(request: Request):
    request_params = urllib.parse.parse_qsl((await request.body()).decode())

    response = await clients.http.post(
        url=PAYPAL_VERIFY_URL,
        headers={"content-type": "application/x-www-form-urlencoded"},
        params=[("cmd", "_notify-validate")] + request_params,  # type: ignore
    )
    response.raise_for_status()

    notification = dict(request_params)

    if response.text == "VERIFIED":
        logging.info(
            "PayPal IPN verified",
            extra={"notification": notification},
        )
        if notification["payment_status"] != "Completed":
            logging.warning(
                "Non completed transaction",
                extra={"notification": notification},
            )
            return Response(status_code=200)

        # TODO: check if transaction has already been processed
        transaction_id = notification["txn_id"]

        if notification["receiver_email"] != settings.PAYPAL_BUSINESS_EMAIL:
            logging.warning(
                "Wrong paypal receiver email",
                extra={"notification": notification},
            )
            return Response(status_code=200)

        if notification["mc_currency"] not in ACCEPTED_CURRENCIES:
            logging.warning(
                "Wrong paypal currency",
                extra={"notification": notification},
            )
            return Response(status_code=200)

        custom_fields = json.loads(notification["custom"])
        user_id = custom_fields["user_id"]

        logging.info(
            "Granting donation perks to user",
            extra={"user_id": user_id, "notification": notification},
        )

        # TODO: determine support tier, and number of months

        # TODO: fetch user from database, write donation perks

    elif response.text == "INVALID":
        logging.warning(
            "PayPal IPN invalid",
            extra={
                "response_text": response.text,
                "notification": notification,
            },
        )
        # fallthrough (do not let the client know of the invalidity)
    else:
        logging.error(
            "PayPal IPN verification status unknown",
            extra={
                "response_text": response.text,
                "notification": notification,
            },
        )
        return Response(status_code=400)

    return Response(status_code=200)
