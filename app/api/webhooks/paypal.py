import asyncio
import logging
import time
import urllib.parse
import uuid
from datetime import datetime
from typing import Any

from discord_webhook import AsyncDiscordWebhook
from discord_webhook.webhook import DiscordEmbed
from fastapi import APIRouter
from fastapi import Header
from fastapi import Request
from fastapi import Response
from tenacity import retry
from tenacity import stop_after_attempt
from tenacity import wait_exponential_jitter

from app import clients
from app import settings
from app.reliability import retry_if_exception_network_related
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


BADGE_LIMIT = 6
SUPPORTER_BADGE_ID = 36
PREMIUM_BADGE_ID = 59

I32_MAX = (1 << 31) - 1


class Privileges:
    SUPPORTER = 4  # Deprecated legacy role
    PREMIUM = 8388608


PREMIUM_MONTHLY_PRICE = 5.0


def months_to_seconds(months: int) -> float:
    return months * (60 * 60 * 24 * 30)


def calculate_supporter_price(months: int) -> float:
    return round((months * 30 * 0.2) ** 0.72, 2)


def calculate_premium_price(months: int) -> float:
    return round(months * PREMIUM_MONTHLY_PRICE, 2)


def premium_to_supporter(donor_time_remaining: float) -> float:
    exchange_rate = calculate_premium_price(1) / calculate_supporter_price(1)
    return donor_time_remaining * exchange_rate


def supporter_to_premium(donor_time_remaining: float) -> float:
    exchange_rate = calculate_supporter_price(1) / calculate_premium_price(1)
    return donor_time_remaining * exchange_rate


@retry(
    stop=stop_after_attempt(7),
    wait=wait_exponential_jitter(initial=1, max=60, exp_base=2, jitter=1),
    retry=retry_if_exception_network_related(),
)
async def send_discord_webhook(webhook: AsyncDiscordWebhook) -> None:
    await webhook.execute()


def schedule_failure_webhook(fields: dict[str, Any]) -> None:
    webhook = AsyncDiscordWebhook(
        url=settings.DISCORD_WEBHOOK_URL,
        embeds=[
            DiscordEmbed(
                title="Failed to grant donation perks to user",
                fields=[{"name": k, "value": str(v)} for k, v in fields.items()],
                color=0xFF0000,
            ),
        ],
    )
    asyncio.create_task(send_discord_webhook(webhook))


def schedule_success_webhook(fields: dict[str, Any]) -> None:
    webhook = AsyncDiscordWebhook(
        url=settings.DISCORD_WEBHOOK_URL,
        embeds=[
            DiscordEmbed(
                title="Successfully granted donation perks to user",
                fields=[{"name": k, "value": str(v)} for k, v in fields.items()],
                color=0x00FF00,
            ),
        ],
    )
    asyncio.create_task(send_discord_webhook(webhook))


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

    if response.text != "VERIFIED":
        will_grant_donor = not settings.SHOULD_REQUIRE_IPN_VERIFICATION
        logging.warning(
            "PayPal IPN invalid",
            extra={
                "reason": "ipn_verification_failed",
                "response_text": response.text,
                "will_grant_donor": will_grant_donor,
                "request_id": x_request_id,
            },
        )

        if settings.SHOULD_REQUIRE_IPN_VERIFICATION:
            # Do not process the request any further.
            # Return a 2xx code to prevent PayPal from retrying.
            schedule_failure_webhook(
                fields={
                    "Reason": "ipn_verification_failed",
                    "Response Text": response.text,
                    "Request ID": x_request_id,
                },
            )
            return Response(status_code=200)
        else:
            pass

    notification = dict(request_params)
    transaction_id = notification["txn_id"]

    if notification["payment_status"] != "Completed":
        logging.warning(
            "Failed to process IPN notification",
            extra={
                "reason": "incomplete_payment",
                "payment_status": notification["payment_status"],
                "transaction_id": transaction_id,
                "request_id": x_request_id,
            },
        )
        schedule_failure_webhook(
            fields={
                "Reason": "incomplete_payment",
                "Payment Status": notification["payment_status"],
                "Transaction ID": transaction_id,
                "Request ID": x_request_id,
            },
        )
        return Response(status_code=200)

    if (
        settings.SHOULD_ENFORCE_UNIQUE_PAYMENTS
        and await notifications.already_processed(transaction_id)
    ):
        logging.warning(
            "Failed to process IPN notification",
            extra={
                "reason": "transaction_already_processed",
                "transaction_id": transaction_id,
                "request_id": x_request_id,
            },
        )
        schedule_failure_webhook(
            fields={
                "Reason": "transaction_already_processed",
                "Transaction ID": transaction_id,
                "Request ID": x_request_id,
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
        schedule_failure_webhook(
            fields={
                "Reason": "wrong_paypal_business_email",
                "Business": notification["business"],
                "Expected Business": settings.PAYPAL_BUSINESS_EMAIL,
                "Request ID": x_request_id,
            },
        )
        return Response(status_code=200)

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
        schedule_failure_webhook(
            fields={
                "Reason": "non_accpeted_currency",
                "Currency": donation_currency,
                "Accepted Currencies": ACCEPTED_CURRENCIES,
                "Request ID": x_request_id,
            },
        )
        return Response(status_code=200)

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
        schedule_failure_webhook(
            fields={
                "Reason": "no_user_identification",
                "Request ID": x_request_id,
            },
        )
        return Response(status_code=200)

    if user is None:
        logging.error(
            "Failed to process IPN notification",
            extra={
                "reason": "user_not_found",
                "custom_fields": custom_fields,
                "request_id": x_request_id,
            },
        )
        schedule_failure_webhook(
            fields={
                "Reason": "user_not_found",
                "Custom Fields": custom_fields,
                "Request ID": x_request_id,
            },
        )
        return Response(status_code=200)

    user_id = user["id"]
    username = user["username"]

    # TODO: remove this after supporter perk migration is complete
    has_supporter = user["privileges"] & Privileges.SUPPORTER != 0

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

    if donation_tier == "premium":
        calculated_price = calculate_premium_price(donation_months)
    elif donation_tier == "supporter":
        logging.warning(
            "A user attempted to purchase supporter after it's been deprecated",
            extra={
                "request_id": x_request_id,
                "user_id": user_id,
                "username": username,
            },
        )
        schedule_failure_webhook(
            fields={
                "Reason": "supporter_deprecated",
                "User ID": user_id,
                "Username": username,
                "Request ID": x_request_id,
            },
        )
        return Response(status_code=200)
    else:
        logging.error(
            "Failed to process IPN notification",
            extra={
                "reason": "invalid_donation_tier",
                "donation_tier": donation_tier,
                "request_id": x_request_id,
            },
        )
        schedule_failure_webhook(
            fields={
                "Reason": "invalid_donation_tier",
                "Donation Tier": donation_tier,
                "Request ID": x_request_id,
            },
        )
        return Response(status_code=200)

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
        schedule_failure_webhook(
            fields={
                "Reason": "invalid_donation_amount",
                "Donation Amount": donation_amount,
                "Calculated Price": calculated_price,
                "Request ID": x_request_id,
            },
        )
        return Response(status_code=200)

    privileges = user["privileges"]
    donor_seconds_remaining = max(user["donor_expire"], time.time()) - time.time()
    user_badge_ids = [b["badge"] for b in await user_badges.fetch_all(user_id)]

    # 1. convert any existing supporter to premium (TODO: deprecate after perk migration)
    if has_supporter:
        donor_seconds_remaining = supporter_to_premium(donor_seconds_remaining)
        if SUPPORTER_BADGE_ID in user_badge_ids:
            user_badge_ids.remove(SUPPORTER_BADGE_ID)

    # 2. add the new donation
    privileges |= Privileges.PREMIUM | Privileges.SUPPORTER
    donor_seconds_remaining += months_to_seconds(donation_months)
    if PREMIUM_BADGE_ID not in user_badge_ids:
        user_badge_ids.append(PREMIUM_BADGE_ID)

    donor_expire = min(donor_seconds_remaining + time.time(), I32_MAX)
    donor_expire = int(donor_expire)

    # remove any badges beyond the limit
    # (these will always be ones we added)
    user_badge_ids = user_badge_ids[:BADGE_LIMIT]

    logging.info(
        "Granting donation perks to user",
        extra={
            "user_id": user_id,
            "username": username,
            "donation_tier": donation_tier,
            "donation_months": donation_months,
            "donation_amount": donation_amount,
            "donation_currency": donation_currency,
            "new_privileges": privileges,
            "new_donor_expire": donor_expire,
            "new_user_badges": user_badge_ids,  # TODO: nicer format
            "transaction_id": transaction_id,
            "request_id": x_request_id,
        },
    )
    schedule_success_webhook(
        fields={
            "User ID": user_id,
            "Username": username,
            "Donation Tier": donation_tier,
            "Donation Months": donation_months,
            "Donation Amount": round(donation_amount, 2),
            "Donation Currency": donation_currency,
            "New Privileges": privileges,
            "New Donor Expire": datetime.fromtimestamp(donor_expire),
            "New User Badges": user_badge_ids,
            "Transaction ID": transaction_id,
            "Request ID": x_request_id,
        },
    )

    # make writes to the database
    if settings.SHOULD_WRITE_TO_USERS_DB:
        async with clients.database.transaction():
            await users.partial_update(
                user_id=user_id,
                privileges=privileges,
                donor_expire=donor_expire,
            )

            await user_badges.delete_by_user_id(user_id)
            for badge_id in user_badge_ids:
                await user_badges.insert(user_id, badge_id)

            await notifications.insert(
                transaction_id=transaction_id,
                notification=notification,
            )

    return Response(status_code=200)
