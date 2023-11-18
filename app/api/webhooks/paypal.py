import logging
import time
import urllib.parse
import uuid
from typing import Literal
from typing import TypedDict

from fastapi import APIRouter
from fastapi import Header
from fastapi import Request
from fastapi import Response

from app import clients
from app import settings
from app.repositories import notifications
from app.repositories import user_badges
from app.repositories import users


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
    # the sandbox env only supports USD
    ACCEPTED_CURRENCIES.add("USD")

PRIVILEGE_BITS_MAPPING = {"supporter": 4, "premium": 8388608}

BADGE_LIMIT = 6
SUPPORTER_BADGE_ID = 36
PREMIUM_BADGE_ID = 59


class BadgeChange(TypedDict):
    action: Literal["insert", "delete"]
    id: int


def calculate_supporter_price(months: int) -> float:
    return (months * 30 * 0.2) ** 0.72


def calculate_premium_price(months: int) -> float:
    return (months * 68 * 0.15) ** 0.93


def work_out_final_badges(
    user_id: int,
    user_badges: list[user_badges.UserBadge],
    badge_changes: list[BadgeChange],
) -> list[user_badges.UserBadge]:
    # sort so all deletes are first
    badge_changes.sort(key=lambda x: x["action"] == "delete")

    # apply changes
    for action in badge_changes:
        if action["action"] == "delete":
            user_badges = [
                badge for badge in user_badges if badge["badge"] != action["id"]
            ]

        # only apply inserts while there are under BADGE_LIMIT badges
        elif action["action"] == "insert":
            if len(user_badges) < BADGE_LIMIT:
                user_badges.append({"user": user_id, "badge": action["id"]})
            else:
                logging.info(
                    "Skipping badge insert due to badge limit",
                    extra={"badge": action["id"], "user_id": user_id},
                )

    return user_badges


@router.post("/webhooks/paypal_ipn")
async def process_notification(
    request: Request,
    x_request_id: str = Header(default_factory=uuid.uuid4),
):
    request_data = await request.body()
    logging.info(  # TODO: change to debug once stabilized
        "Received PayPal IPN notification",
        extra={
            "request_data": request_data,
            "request_id": x_request_id,
        },
    )

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
                    "reason": "incomplete_payment",
                    "payment_status": notification["payment_status"],
                    "request_id": x_request_id,
                },
            )
            return Response(status_code=400)

        transaction_id = notification["txn_id"]
        if await notifications.already_processed(transaction_id):
            logging.warning(
                "Failed to process IPN notification",
                extra={
                    "reason": "transaction_already_processed",
                    "transaction_id": transaction_id,
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
                    "request_id": x_request_id,
                },
            )
            return Response(status_code=400)

        donation_currency = notification["mc_currency"]
        if donation_currency not in ACCEPTED_CURRENCIES:
            logging.warning(
                "Failed to process IPN notification",
                extra={
                    "reason": "non_accpeted_currency",
                    "currency": donation_currency,
                    "accepted_currencies": ACCEPTED_CURRENCIES,
                    "request_id": x_request_id,
                },
            )
            return Response(status_code=400)

        custom_fields = dict(urllib.parse.parse_qsl(notification["custom"]))

        if "userid" in custom_fields:
            user = await users.fetch_by_user_id(int(custom_fields["userid"]))
        elif "username" in custom_fields:
            user = await users.fetch_by_username(custom_fields["username"])
        else:
            logging.error(
                "Failed to process IPN notification",
                extra={
                    "reason": "no_user_identification",
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
                    "custom_fields": custom_fields,
                    "request_id": x_request_id,
                },
            )
            return Response(status_code=400)

        user_id = user["id"]
        username = user["username"]

        # TODO: potentially clean this up
        donation_tier = (
            notification["option_name2"]
            .removeprefix(
                "Akatsuki user to give ",
            )
            .removesuffix(":")
        )
        donation_months = int(
            notification["option_selection1"].removesuffix("s").removesuffix(" month"),
        )

        if donation_tier == "supporter":
            calculated_price = calculate_supporter_price(donation_months)
        elif donation_tier == "premium":
            calculated_price = calculate_premium_price(donation_months)
        else:
            logging.error(
                "Failed to process IPN notification",
                extra={
                    "reason": "invalid_donation_tier",
                    "donation_tier": donation_tier,
                    "request_id": x_request_id,
                },
            )
            return Response(status_code=400)

        # copy hanayo rounding behaviour on price
        calculated_price = round(calculated_price, 2)

        donation_amount = float(notification["mc_gross"])
        if donation_amount != calculated_price:
            logging.error(
                "Failed to process IPN notification",
                extra={
                    "reason": "invalid_donation_amount",
                    "donation_amount": donation_amount,
                    "calculated_price": calculated_price,
                    "request_id": x_request_id,
                },
            )
            return Response(status_code=400)

        new_privileges = user["privileges"] | PRIVILEGE_BITS_MAPPING[donation_tier]
        new_donor_expire = min(
            (1 << 31) - 1,  # i32 max
            max(user["donor_expire"], time.time())
            + donation_months * (60 * 60 * 24 * 30),
        )

        badge_changes: list[BadgeChange] = []

        if donation_tier == "premium":
            badge_changes.append({"action": "insert", "id": PREMIUM_BADGE_ID})
        elif donation_tier == "supporter":
            badge_changes.append({"action": "insert", "id": SUPPORTER_BADGE_ID})

        exchange_rate = calculate_premium_price(1) / calculate_supporter_price(1)

        # if a premium user buys supporter, convert them to a supporter
        # and exchange their premium months to supporter months
        if (
            user["privileges"] & PRIVILEGE_BITS_MAPPING["premium"] != 0
            and donation_tier == "supporter"
        ):
            new_privileges &= ~PRIVILEGE_BITS_MAPPING["premium"]
            badge_changes.append({"action": "delete", "id": PREMIUM_BADGE_ID})

            donation_months = int(donation_months * exchange_rate)

        # if a supporter user buys premium, convert them to premium
        # and exchange their supporter months to premium months
        elif (
            user["privileges"] & PRIVILEGE_BITS_MAPPING["supporter"] != 0
            and donation_tier == "premium"
        ):
            donation_months = int(donation_months / exchange_rate)

        current_badges = await user_badges.fetch_all(user_id)
        final_badges = work_out_final_badges(
            user_id,
            current_badges,
            badge_changes,
        )

        logging.info(
            "Granting donation perks to user",
            extra={
                "user_id": user_id,
                "username": username,
                "donation_tier": donation_tier,
                "donation_months": donation_months,
                "donation_amount": donation_amount,
                "donation_currency": donation_currency,
                "new_privileges": new_privileges,
                "new_donor_expiry": new_donor_expire,
                "badge_changes": badge_changes,
                "transaction_id": transaction_id,
                "request_id": x_request_id,
            },
        )

        # make writes to the database
        if settings.SHOULD_WRITE_TO_USERS_DB:
            async with clients.database.transaction():
                await users.partial_update(
                    user_id=user_id,
                    privileges=new_privileges,
                    donor_expire=new_donor_expire,
                )

                await user_badges.delete_by_user_id(user_id)
                for badge in final_badges:
                    await user_badges.insert(user_id, badge["badge"])

                await notifications.insert(
                    transaction_id=transaction_id,
                    notification=notification,
                )

        return Response(status_code=200)

    elif response.text == "INVALID":
        logging.warning(
            "PayPal IPN invalid",
            extra={
                "response_text": response.text,
                "request_id": x_request_id,
            },
        )
        # fallthrough (do not let the client know of the invalidity)
        return Response(status_code=400)

    else:
        logging.error(
            "PayPal IPN verification status unknown",
            extra={
                "response_text": response.text,
                "request_id": x_request_id,
            },
        )
        return Response(status_code=400)
