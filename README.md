# payments-service

Handling all payment related processing for Akatsuki

## Webhooks

This service handles payment webhooks from both PayPal and Stripe to automatically distribute donation perks to users. The webhook handlers share common logic for donation processing, user validation, and perk distribution through the `app/api/webhooks/shared.py` module.

### PayPal Webhook

The PayPal webhook is available at `/webhooks/paypal_ipn` and processes PayPal Instant Payment Notifications (IPN).

### Stripe Webhook

The Stripe webhook is available at `/webhooks/stripe` and processes Stripe webhook events.

#### Setup

1. Add the following environment variables:

   - `STRIPE_SECRET_KEY`: Your Stripe secret key
   - `STRIPE_WEBHOOK_SECRET`: Your Stripe webhook endpoint secret

2. Configure your Stripe webhook endpoint to point to `/webhooks/stripe` and listen for:

   - `checkout.session.completed`
   - `payment_intent.succeeded`

3. When creating payments in Stripe, include the following metadata:
   - `userid`: The user's ID (integer)
   - `username`: The user's username (string)
   - `donation_tier`: Either "premium" or "supporter" (defaults to "premium")
   - `donation_months`: Number of months for the donation (defaults to 1)

#### Example Stripe Checkout Session

```javascript
const session = await stripe.checkout.sessions.create({
  payment_method_types: ["card"],
  line_items: [
    {
      price_data: {
        currency: "usd",
        product_data: {
          name: "Premium Donation",
        },
        unit_amount: 500, // $5.00 in cents
      },
      quantity: 1,
    },
  ],
  mode: "payment",
  success_url: "https://your-domain.com/success",
  cancel_url: "https://your-domain.com/cancel",
  metadata: {
    userid: "12345",
    username: "example_user",
    donation_tier: "premium",
    donation_months: "1",
  },
});
```

#### Example Payment Intent

```javascript
const paymentIntent = await stripe.paymentIntents.create({
  amount: 500, // $5.00 in cents
  currency: "usd",
  metadata: {
    userid: "12345",
    username: "example_user",
    donation_tier: "premium",
    donation_months: "1",
  },
});
```
