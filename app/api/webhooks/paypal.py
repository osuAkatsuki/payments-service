import time
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
if settings.APP_ENV != "production":
    ACCEPTED_CURRENCIES.add("USD")

PRIVILEGE_MAPPING = {"supporter": 4, "premium": 8388608}

seen_transactions: set[str] = set()


@router.post("/webhooks/paypal_ipn")
async def process_notification(request: Request):
    request_params = urllib.parse.parse_qsl((await request.body()).decode())
    logging.info("Debug", extra={"notification": dict(request_params)})

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

        # TODO: check if transaction has already been processed in db
        transaction_id = notification["txn_id"]
        if transaction_id in seen_transactions:
            logging.warning(
                "Transaction already processed",
                extra={"notification": notification},
            )
            return Response(status_code=200)

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

        # TODO: dynamically determine support tier and number of months
        donation_tier = "supporter" or "premium"
        donation_months = 1

        user = await clients.database.fetch_one(
            query="""\
                SELECT * FROM users WHERE id = :user_id
            """,
            values={"user_id": user_id},
        )
        if user is None:
            logging.error(
                "User not found while attempting to distribute donation perks",
                extra={"user_id": user_id, "notification": notification},
            )
            return Response(status_code=400)

        new_privileges = PRIVILEGE_MAPPING[donation_tier]
        new_donor_expiry = max(user["donor_expire"], time.time()) + donation_months * (
            60 * 60 * 24 * 30
        )

        # TODO: if the user already has a donation tier, ensure we are not
        #       upgrading or downgrading them by converting the value of the
        #       different perks against eachother.

        await clients.database.execute(
            query="""\
                UPDATE users
                   SET privileges = privileges | :privileges,
                       donor_expire = :donor_expire
                 WHERE id = :user_id
            """,
            values={
                "privileges": new_privileges,
                "donor_expire": new_donor_expiry,
                "user_id": user_id,
            },
        )

        # TODO: store transaction as processed in database
        seen_transactions.add(transaction_id)

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
