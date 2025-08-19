import logging
import uuid

import stripe
from fastapi import APIRouter
from fastapi import Header
from fastapi import Request
from fastapi import Response

from app import settings
from app.api.webhooks import shared
from app.repositories import users

router = APIRouter()

# Configure Stripe
stripe.api_key = settings.STRIPE_SECRET_KEY

ACCEPTED_CURRENCIES = {"usd"}


@router.post("/webhooks/stripe")
async def process_stripe_webhook(
    request: Request,
    x_request_id: str = Header(default_factory=uuid.uuid4),
):
    request_data = await request.body()
    logging.info(
        "Received Stripe webhook notification",
        extra={
            "request_id": x_request_id,
        },
    )

    # Get the signature from headers
    signature = request.headers.get("stripe-signature")
    if not signature:
        logging.error(
            "Failed to process Stripe webhook",
            extra={
                "reason": "missing_stripe_signature",
                "request_id": x_request_id,
            },
        )
        shared.schedule_failure_webhook(
            fields={
                "Reason": "missing_stripe_signature",
                "Request ID": x_request_id,
            },
            provider="Stripe",
        )
        return Response(status_code=400)

    try:
        # Verify the webhook signature
        event = stripe.Webhook.construct_event(
            request_data, signature, settings.STRIPE_WEBHOOK_SECRET
        )
    except ValueError as e:
        logging.error(
            "Failed to process Stripe webhook",
            extra={
                "reason": "invalid_payload",
                "error": str(e),
                "request_id": x_request_id,
            },
        )
        shared.schedule_failure_webhook(
            fields={
                "Reason": "invalid_payload",
                "Error": str(e),
                "Request ID": x_request_id,
            },
            provider="Stripe",
        )
        return Response(status_code=400)
    except stripe.SignatureVerificationError as e:
        logging.error(
            "Failed to process Stripe webhook",
            extra={
                "reason": "invalid_signature",
                "error": str(e),
                "request_id": x_request_id,
            },
        )
        shared.schedule_failure_webhook(
            fields={
                "Reason": "invalid_signature",
                "Error": str(e),
                "Request ID": x_request_id,
            },
            provider="Stripe",
        )
        return Response(status_code=400)

    # Handle the event
    if event["type"] == "checkout.session.completed":
        await handle_checkout_session_completed(event["data"]["object"], x_request_id)
    elif event["type"] == "payment_intent.succeeded":
        await handle_payment_intent_succeeded(event["data"]["object"], x_request_id)
    else:
        logging.info(
            "Unhandled Stripe event type",
            extra={
                "event_type": event["type"],
                "request_id": x_request_id,
            },
        )

    return Response(status_code=200)


async def handle_checkout_session_completed(session: dict, x_request_id: str) -> None:
    """Handle completed checkout sessions from Stripe."""
    transaction_id = session["id"]

    if session["payment_status"] != "paid":
        logging.warning(
            "Failed to process Stripe checkout session",
            extra={
                "reason": "incomplete_payment",
                "payment_status": session["payment_status"],
                "transaction_id": transaction_id,
                "request_id": x_request_id,
            },
        )
        shared.schedule_failure_webhook(
            fields={
                "Reason": "incomplete_payment",
                "Payment Status": session["payment_status"],
                "Transaction ID": transaction_id,
                "Request ID": x_request_id,
            },
            provider="Stripe",
        )
        return

    # Check for duplicate processing
    is_duplicate, error_reason = await shared.check_duplicate_transaction(
        transaction_id, x_request_id, "Stripe"
    )
    if is_duplicate:
        shared.schedule_failure_webhook(
            fields={
                "Reason": error_reason,
                "Transaction ID": transaction_id,
                "Request ID": x_request_id,
            },
            provider="Stripe",
        )
        return

    # Extract metadata from the session
    metadata = session.get("metadata", {})
    custom_fields = metadata

    # Find user by metadata
    user, error_reason = await shared.find_user_by_metadata(custom_fields, x_request_id)
    if user is None:
        shared.schedule_failure_webhook(
            fields={
                "Reason": error_reason,
                "Request ID": x_request_id,
            },
            provider="Stripe",
        )
        return

    # Process the payment
    await process_donation_payment(session, user, transaction_id, x_request_id)


async def handle_payment_intent_succeeded(
    payment_intent: dict, x_request_id: str
) -> None:
    """Handle successful payment intents from Stripe."""
    transaction_id = payment_intent["id"]

    # Check for duplicate processing
    is_duplicate, error_reason = await shared.check_duplicate_transaction(
        transaction_id, x_request_id, "Stripe"
    )
    if is_duplicate:
        shared.schedule_failure_webhook(
            fields={
                "Reason": error_reason,
                "Transaction ID": transaction_id,
                "Request ID": x_request_id,
            },
            provider="Stripe",
        )
        return

    # Extract metadata from the payment intent
    metadata = payment_intent.get("metadata", {})
    custom_fields = metadata

    # Find user by metadata
    user, error_reason = await shared.find_user_by_metadata(custom_fields, x_request_id)
    if user is None:
        shared.schedule_failure_webhook(
            fields={
                "Reason": error_reason,
                "Request ID": x_request_id,
            },
            provider="Stripe",
        )
        return

    # Process the payment
    await process_donation_payment(payment_intent, user, transaction_id, x_request_id)


async def process_donation_payment(
    payment_data: dict, user: users.User, transaction_id: str, x_request_id: str
) -> None:
    """Process a donation payment and grant perks to the user."""
    # Extract payment information
    donation_amount = payment_data["amount"] / 100  # Convert from cents
    donation_currency = payment_data["currency"]

    if donation_currency not in ACCEPTED_CURRENCIES:
        logging.warning(
            "Failed to process Stripe payment",
            extra={
                "reason": "non_accepted_currency",
                "currency": donation_currency,
                "accepted_currencies": ACCEPTED_CURRENCIES,
                "request_id": x_request_id,
            },
        )
        shared.schedule_failure_webhook(
            fields={
                "Reason": "non_accepted_currency",
                "Currency": donation_currency,
                "Accepted Currencies": ACCEPTED_CURRENCIES,
                "Request ID": x_request_id,
            },
            provider="Stripe",
        )
        return

    # Extract donation details from metadata
    metadata = payment_data.get("metadata", {})
    donation_tier = metadata.get("donation_tier", "premium")
    donation_months = int(metadata.get("donation_months", "1"))

    # Validate donation tier
    is_valid_tier, error_reason = await shared.validate_donation_tier(
        donation_tier, user["id"], user["username"], x_request_id
    )
    if not is_valid_tier:
        shared.schedule_failure_webhook(
            fields={
                "Reason": error_reason,
                "User ID": user["id"],
                "Username": user["username"],
                "Request ID": x_request_id,
            },
            provider="Stripe",
        )
        return

    # Calculate and validate donation amount
    if donation_tier == "premium":
        calculated_price = shared.calculate_premium_price(donation_months)
    else:
        calculated_price = 0  # This should not happen due to validation above

    is_valid_amount, error_reason = await shared.validate_donation_amount(
        donation_amount, calculated_price, x_request_id
    )
    if not is_valid_amount:
        shared.schedule_failure_webhook(
            fields={
                "Reason": error_reason,
                "Donation Amount": donation_amount,
                "Calculated Price": calculated_price,
                "Request ID": x_request_id,
            },
            provider="Stripe",
        )
        return

    # Process the donation perks
    await shared.process_donation_perks(
        user=user,
        donation_tier=donation_tier,
        donation_months=donation_months,
        donation_amount=donation_amount,
        donation_currency=donation_currency,
        transaction_id=transaction_id,
        x_request_id=x_request_id,
        provider="Stripe",
    )
