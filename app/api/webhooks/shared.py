import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from discord_webhook import AsyncDiscordWebhook
from discord_webhook.webhook import DiscordEmbed
from tenacity import retry
from tenacity import stop_after_attempt
from tenacity import wait_exponential_jitter

from app import clients
from app import settings
from app.reliability import retry_if_exception_network_related
from app.repositories import notifications
from app.repositories import user_badges
from app.repositories import users


# Shared constants
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


def schedule_failure_webhook(fields: dict[str, Any], provider: str = "") -> None:
    title = "Failed to grant donation perks to user"
    if provider:
        title += f" ({provider})"

    webhook = AsyncDiscordWebhook(
        url=settings.DISCORD_WEBHOOK_URL,
        embeds=[
            DiscordEmbed(
                title=title,
                fields=[{"name": k, "value": str(v)} for k, v in fields.items()],
                color=0xFF0000,
            ),
        ],
    )
    asyncio.create_task(send_discord_webhook(webhook))


def schedule_success_webhook(fields: dict[str, Any], provider: str = "") -> None:
    title = "Successfully granted donation perks to user"
    if provider:
        title += f" ({provider})"

    webhook = AsyncDiscordWebhook(
        url=settings.DISCORD_WEBHOOK_URL,
        embeds=[
            DiscordEmbed(
                title=title,
                fields=[{"name": k, "value": str(v)} for k, v in fields.items()],
                color=0x00FF00,
            ),
        ],
    )
    asyncio.create_task(send_discord_webhook(webhook))


async def find_user_by_metadata(
    custom_fields: dict,
    x_request_id: str,
) -> tuple[users.User | None, str]:
    """Find user by metadata fields (userid or username)."""
    if "userid" in custom_fields:
        user = await users.fetch_by_user_id(int(custom_fields["userid"]))
    elif "username" in custom_fields:
        user = await users.fetch_by_username(custom_fields["username"])
    else:
        logging.error(
            "Failed to process payment",
            extra={
                "reason": "no_user_identification",
                "request_id": x_request_id,
            },
        )
        return None, "no_user_identification"

    if user is None:
        logging.error(
            "Failed to process payment",
            extra={
                "reason": "user_not_found",
                "custom_fields": custom_fields,
                "request_id": x_request_id,
            },
        )
        return None, "user_not_found"

    return user, ""


async def validate_donation_tier(
    donation_tier: str,
    user_id: int,
    username: str,
    x_request_id: str,
) -> tuple[bool, str]:
    """Validate donation tier and return success status and error reason if failed."""
    if donation_tier == "premium":
        return True, ""
    elif donation_tier == "supporter":
        logging.warning(
            "A user attempted to purchase supporter after it's been deprecated",
            extra={
                "request_id": x_request_id,
                "user_id": user_id,
                "username": username,
            },
        )
        return False, "supporter_deprecated"
    else:
        logging.error(
            "Failed to process payment",
            extra={
                "reason": "invalid_donation_tier",
                "donation_tier": donation_tier,
                "request_id": x_request_id,
            },
        )
        return False, "invalid_donation_tier"


async def validate_donation_amount(
    donation_amount: float,
    calculated_price: float,
    x_request_id: str,
    tolerance: float = 0.01,
) -> tuple[bool, str]:
    """Validate donation amount matches calculated price within tolerance."""
    if abs(donation_amount - calculated_price) > tolerance:
        logging.error(
            "Failed to process payment",
            extra={
                "reason": "invalid_donation_amount",
                "donation_amount": donation_amount,
                "calculated_price": calculated_price,
                "request_id": x_request_id,
            },
        )
        return False, "invalid_donation_amount"
    return True, ""


async def process_donation_perks(
    user: users.User,
    donation_tier: str,
    donation_months: int,
    donation_amount: float,
    donation_currency: str,
    transaction_id: str,
    x_request_id: str,
    provider: str = "",
) -> None:
    """Process donation and grant perks to the user."""
    user_id = user["id"]
    username = user["username"]

    # TODO: remove this after supporter perk migration is complete
    has_supporter = user["privileges"] & Privileges.SUPPORTER != 0

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
        (
            f"Granting donation perks to user ({provider})"
            if provider
            else "Granting donation perks to user"
        ),
        extra={
            "user_id": user_id,
            "username": username,
            "donation_tier": donation_tier,
            "donation_months": donation_months,
            "donation_amount": donation_amount,
            "donation_currency": donation_currency,
            "new_privileges": privileges,
            "new_donor_expire": donor_expire,
            "new_user_badges": user_badge_ids,
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
        provider=provider,
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
                notification={
                    "user_id": user_id,
                    "username": username,
                    "donation_tier": donation_tier,
                    "donation_months": donation_months,
                    "donation_amount": donation_amount,
                    "donation_currency": donation_currency,
                    "provider": provider,
                },
            )


async def check_duplicate_transaction(
    transaction_id: str, x_request_id: str, provider: str = ""
) -> tuple[bool, str]:
    """Check if transaction has already been processed."""
    if (
        settings.SHOULD_ENFORCE_UNIQUE_PAYMENTS
        and await notifications.already_processed(transaction_id)
    ):
        logging.warning(
            (
                f"Failed to process {provider} payment"
                if provider
                else "Failed to process payment"
            ),
            extra={
                "reason": "transaction_already_processed",
                "transaction_id": transaction_id,
                "request_id": x_request_id,
            },
        )
        return True, "transaction_already_processed"
    return False, ""
