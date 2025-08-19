import logging
import urllib.parse
import uuid

from fastapi import APIRouter
from fastapi import Header
from fastapi import Request
from fastapi import Response

from app import clients
from app import settings
from app.api.webhooks import shared


router = APIRouter()

PAYPAL_VERIFY_URL_PROD = "https://ipnpb.paypal.com/cgi-bin/webscr"
PAYPAL_VERIFY_URL_TEST = "https://ipnpb.sandbox.paypal.com/cgi-bin/webscr"

PAYPAL_VERIFY_URL = (
    PAYPAL_VERIFY_URL_PROD
    if settings.APP_ENV == "production"
    else PAYPAL_VERIFY_URL_TEST
)

ACCEPTED_CURRENCIES = {"USD"}


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
            shared.schedule_failure_webhook(
                fields={
                    "Reason": "ipn_verification_failed",
                    "Response Text": response.text,
                    "Request ID": x_request_id,
                },
                provider="PayPal",
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
        shared.schedule_failure_webhook(
            fields={
                "Reason": "incomplete_payment",
                "Payment Status": notification["payment_status"],
                "Transaction ID": transaction_id,
                "Request ID": x_request_id,
            },
            provider="PayPal",
        )
        return Response(status_code=200)

    # Check for duplicate processing
    is_duplicate, error_reason = await shared.check_duplicate_transaction(
        transaction_id, x_request_id, "PayPal",
    )
    if is_duplicate:
        shared.schedule_failure_webhook(
            fields={
                "Reason": error_reason,
                "Transaction ID": transaction_id,
                "Request ID": x_request_id,
            },
            provider="PayPal",
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
        shared.schedule_failure_webhook(
            fields={
                "Reason": "wrong_paypal_business_email",
                "Business": notification["business"],
                "Expected Business": settings.PAYPAL_BUSINESS_EMAIL,
                "Request ID": x_request_id,
            },
            provider="PayPal",
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
        shared.schedule_failure_webhook(
            fields={
                "Reason": "non_accpeted_currency",
                "Currency": donation_currency,
                "Accepted Currencies": ACCEPTED_CURRENCIES,
                "Request ID": x_request_id,
            },
            provider="PayPal",
        )
        return Response(status_code=200)

    custom_fields = dict(urllib.parse.parse_qsl(notification["custom"]))

    # Find user by metadata
    user, error_reason = await shared.find_user_by_metadata(custom_fields, x_request_id)
    if user is None:
        shared.schedule_failure_webhook(
            fields={
                "Reason": error_reason,
                "Request ID": x_request_id,
            },
            provider="PayPal",
        )
        return Response(status_code=200)

    # Extract donation details from PayPal notification
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

    # Validate donation tier
    is_valid_tier, error_reason = await shared.validate_donation_tier(
        donation_tier, user["id"], user["username"], x_request_id,
    )
    if not is_valid_tier:
        shared.schedule_failure_webhook(
            fields={
                "Reason": error_reason,
                "User ID": user["id"],
                "Username": user["username"],
                "Request ID": x_request_id,
            },
            provider="PayPal",
        )
        return Response(status_code=200)

    # Calculate and validate donation amount
    if donation_tier == "premium":
        calculated_price = shared.calculate_premium_price(donation_months)
    else:
        calculated_price = 0  # This should not happen due to validation above

    donation_amount = float(notification["mc_gross"])
    is_valid_amount, error_reason = await shared.validate_donation_amount(
        donation_amount, calculated_price, x_request_id,
    )
    if not is_valid_amount:
        shared.schedule_failure_webhook(
            fields={
                "Reason": error_reason,
                "Donation Amount": donation_amount,
                "Calculated Price": calculated_price,
                "Request ID": x_request_id,
            },
            provider="PayPal",
        )
        return Response(status_code=200)

    # Process the donation perks
    await shared.process_donation_perks(
        user=user,
        donation_tier=donation_tier,
        donation_months=donation_months,
        donation_amount=donation_amount,
        donation_currency=donation_currency,
        transaction_id=transaction_id,
        x_request_id=x_request_id,
        provider="PayPal",
    )

    return Response(status_code=200)
