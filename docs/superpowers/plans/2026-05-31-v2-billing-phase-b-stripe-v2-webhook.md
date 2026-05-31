# Phase B - Stripe v2 Webhook + Tier-Picker Checkout

**Worker:** GB-BACKEND-02
**Date:** 2026-05-31
**Depends on:** phase A (entitlements substrate)

## Goal

Make web purchases of the three new v2 SKUs flow into the new `entitlements`
table. v1 (`/stripe/checkout` flat $20, `roam_unlimited` via RevenueCat) keeps
writing to `user_entitlements` untouched.

## Scope

1. New POST `/stripe/checkout/v2` accepting `{tier: month|season|lifetime}`
   and creating a Stripe Checkout Session against the matching
   `stripe_price_*` setting. Returns `{url}` like v1.
2. Webhook extension in `app/api/stripe.py` `_handle_stripe_webhook`:
   `checkout.session.completed` keeps its v1 behavior, plus a new branch
   that fires whenever the session's metadata names a v2 tier OR a line
   item's price matches one of `stripe_price_month / _season / _lifetime`.
   The v2 branch writes to `entitlements` via
   `app.services.entitlements.upsert_entitlement`.
3. Tests covering: v2 tier-picker create-checkout returns a Stripe URL with
   the right price selected; v1 path still works (no regression); webhook
   routes v1 sessions to `user_entitlements` and v2 sessions to
   `entitlements`.

## Out of scope

- Apple / Google receipt verification (phase C / D).
- Unified `POST /entitlement/redeem` (phase E).
- Provisioning the actual Stripe Product / Price IDs (phase F).

## Idempotency

Stripe's `checkout.session.completed` events deliver at-least-once; the
webhook may fire the same session twice on retry. The `entitlements` table's
`(source_platform, transaction_id)` unique constraint absorbs the duplicate:
we use the Stripe `payment_intent` as the transaction id for web purchases.

## TDD outline

```
class TestCheckoutV2:
    def test_creates_session_for_month_tier(client, fake_stripe):
        # POST /stripe/checkout/v2 {tier: 'month'} -> returns Stripe URL,
        # fake_stripe assertion: line_items[0].price == settings.stripe_price_month
        ...
    def test_unknown_tier_returns_400(client):
        ...
    def test_missing_price_id_returns_500(client, monkeypatch):
        # If stripe_price_lifetime is "" -> 500 'Tier not configured'
        ...
    def test_unauthed_returns_401(unauthed_client):
        ...

class TestWebhookV1Backcompat:
    def test_v1_session_writes_user_entitlements(client, fake_supabase, fake_stripe):
        # session has no v2 metadata + no v2 price -> writes user_entitlements
        ...

class TestWebhookV2:
    def test_session_with_tier_metadata_writes_entitlements(client, fake_supabase):
        # metadata.tier='lifetime' -> entitlements row, tier='lifetime'
        ...
    def test_session_with_v2_price_writes_entitlements(client, fake_supabase):
        # line_item price matches stripe_price_month -> month + expires_at +30d
        ...
    def test_duplicate_webhook_is_idempotent(client, fake_supabase):
        # Same payment_intent twice -> one row
        ...
```

`fake_stripe` is a small monkeypatch of `_get_stripe()` returning an object
with the methods the route calls. We deliberately don't try to fake Stripe's
signature verification at the Python-library level; the route passes the raw
body straight to `stripe_lib.Webhook.construct_event`. The test path
monkeypatches that call to return a pre-built `Event`-shaped dict so the
verification step is skipped in unit tests.

## Step-by-step

1. Read the existing `app/api/stripe.py` end-to-end (in-context).
2. Add the new POST `/stripe/checkout/v2` route + `CheckoutV2Request` body
   shape + tier-to-price-id helper.
3. Extend `_handle_stripe_webhook` with the v2 branch.
4. Author `tests/test_stripe_v2.py` covering checkout creation + webhook
   routing.
5. Regenerate locked OpenAPI; pytest green.
6. Commit + push. Conductor deploys.
