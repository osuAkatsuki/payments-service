import time
import uuid

from fastapi import APIRouter
from fastapi import Header
from fastapi import Response
from fastapi import Request

from app import clients
from app import settings

import urllib.parse
import logging

from repositories import users


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


async def processs_donation() -> None:
    ...


@router.post("/webhooks/paypal_ipn")
async def process_notification(
    request: Request,
    x_request_id: str = Header(default_factory=uuid.uuid4),
):
    request_data = await request.body()
    logging.debug(
        "Received PayPal IPN notification",
        extra={
            "request_data": request_data,
            "request_id": x_request_id,
        },
    )

    # TODO: test if we can use parse_qs & cleanup here
    request_params = urllib.parse.parse_qsl(request_data.decode())
    response = await clients.http.post(
        url=PAYPAL_VERIFY_URL,
        headers={"content-type": "application/x-www-form-urlencoded"},
        params=[("cmd", "_notify-validate")] + request_params,  # type: ignore
    )
    response.raise_for_status()

    notification = dict(request_params)

    if response.text == "VERIFIED":
        if notification["payment_status"] != "Completed":
            logging.warning(
                "Failed to process IPN notification",
                extra={
                    "reason": "non_completed_transaction",
                    "payment_status": notification["payment_status"],
                    "notification": notification,
                    "request_id": x_request_id,
                },
            )
            return Response(status_code=200)

        # TODO: check if transaction has already been processed in db
        transaction_id = notification["txn_id"]
        if transaction_id in seen_transactions:
            logging.warning(
                "Failed to process IPN notification",
                extra={
                    "reason": "transaction_already_processed",
                    "transaction_id": transaction_id,
                    "notification": notification,
                    "request_id": x_request_id,
                },
            )
            return Response(status_code=200)

        if notification["business"] != settings.PAYPAL_BUSINESS_EMAIL:
            logging.warning(
                "Failed to process IPN notification",
                extra={
                    "reason": "wrong_paypal_business_email",
                    "business": notification["business"],
                    "expected_business": settings.PAYPAL_BUSINESS_EMAIL,
                    "notification": notification,
                    "request_id": x_request_id,
                },
            )
            return Response(status_code=200)

        if notification["mc_currency"] not in ACCEPTED_CURRENCIES:
            logging.warning(
                "Failed to process IPN notification",
                extra={
                    "reason": "non_accpeted_currency",
                    "currency": notification["mc_currency"],
                    "accepted_currencies": ACCEPTED_CURRENCIES,
                    "notification": notification,
                    "request_id": x_request_id,
                },
            )
            return Response(status_code=200)

        custom_fields = dict(urllib.parse.parse_qsl(notification["custom"]))
        user_id = custom_fields.get("user_id")
        if user_id is not None:
            user_id = int(user_id)
        username = custom_fields.get("username")

        # TODO: potentially clean this up
        donation_tier = notification["option_name2"].removeprefix(
            "Akatsuki user to give "
        )
        donation_months = int(notification["option_selection1"].removesuffix(" months"))

        if donation_tier == "supporter":
            donation_price = (donation_months * 30 * 0.2) ** 0.72
        elif donation_tier == "premium":
            donation_price = (donation_months * 68 * 0.15) ** 0.93
        else:
            logging.error(
                "Failed to process IPN notification",
                extra={
                    "reason": "invalid_donation_tier",
                    "donation_tier": donation_tier,
                    "notification": notification,
                    "request_id": x_request_id,
                },
            )
            return Response(status_code=200)

        # copy hanayo rounding behaviour
        donation_price = round(donation_price, 2)

        if float(notification["mc_gross"]) != donation_price:
            logging.error(
                "Failed to process IPN notification",
                extra={
                    "reason": "invalid_donation_amount",
                    "amount": notification["mc_gross"],
                    "donation_price": donation_price,
                    "notification": notification,
                    "request_id": x_request_id,
                },
            )
            return Response(status_code=200)

        if user_id is not None:
            user = await users.fetch_by_user_id(user_id)
        elif username is not None:
            user = await users.fetch_by_username(username)
        else:
            logging.error(
                "Failed to process IPN notification",
                extra={
                    "reason": "no_user_identification",
                    "notification": notification,
                    "request_id": x_request_id,
                },
            )
            return Response(status_code=400)

        if user is None:
            logging.error(
                "Failed to process IPN notification",
                "User not found while attempting to distribute donation perks",
                extra={
                    "reason": "user_not_found",
                    "user_id": user_id,
                    "username": username,
                    "notification": notification,
                    "request_id": x_request_id,
                },
            )
            return Response(status_code=400)

        user_id = user["id"]
        username = user["username"]

        new_privileges = PRIVILEGE_MAPPING[donation_tier]
        new_donor_expiry = min(
            (1 << 31) - 1,  # i32 max
            max(user["donor_expire"], time.time())
            + donation_months * (60 * 60 * 24 * 30),
        )

        # TODO: if the user already has a donation tier, ensure we are not
        #       upgrading or downgrading them by converting the value of the
        #       different perks against eachother.

        logging.info(
            "Granting donation perks to user",
            extra={
                "user_id": user_id,
                "username": username,
                "donation_tier": donation_tier,
                "donation_months": donation_months,
                "new_privileges": new_privileges,
                "new_donor_expiry": new_donor_expiry,
                "amount": donation_price,
                "notification": notification,
                "request_id": x_request_id,
            },
        )

        if settings.SHOULD_WRITE_TO_USERS_DB:
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
                    "user_id": user["id"],
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
                "request_id": x_request_id,
            },
        )
        # fallthrough (do not let the client know of the invalidity)
    else:
        logging.error(
            "PayPal IPN verification status unknown",
            extra={
                "response_text": response.text,
                "notification": notification,
                "request_id": x_request_id,
            },
        )
        return Response(status_code=400)

    return Response(status_code=200)
