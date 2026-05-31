"""Phase B - Stripe v2 tier-picker checkout + webhook routing.

Covers:
  - POST /stripe/checkout/v2 picks the right Stripe price per tier
  - Tier=free is rejected with 400
  - Missing tier price config returns 500
  - 401 path
  - The existing webhook still writes user_entitlements (v1 backcompat)
  - The webhook ALSO writes the new entitlements table when v2 metadata
    or a v2 price id is present
  - Duplicate webhook deliveries are idempotent on (source_platform,
    transaction_id)
"""

from __future__ import annotations

from typing import Any

import pytest


# ── Fakes ─────────────────────────────────────────────────────────────


class _FakeCheckoutSession:
    """Minimal stand-in for `client.checkout.sessions.create()`'s return."""

    def __init__(self, url: str = "https://checkout.stripe.com/c/pay/cs_test_v2"):
        self.url = url

    @staticmethod
    def from_create(params: dict[str, Any]) -> "_FakeCheckoutSession":
        # Record the params on a class-level slot so tests can inspect them.
        _FakeStripeClient.last_params = params
        return _FakeCheckoutSession()


class _FakeStripeClient:
    """Captures the params passed to checkout.sessions.create()."""

    last_params: dict[str, Any] = {}

    class _Sessions:
        def create(self, *, params: dict[str, Any]) -> _FakeCheckoutSession:
            return _FakeCheckoutSession.from_create(params)

    class _Checkout:
        def __init__(self) -> None:
            self.sessions = _FakeStripeClient._Sessions()

    def __init__(self) -> None:
        self.checkout = _FakeStripeClient._Checkout()


@pytest.fixture
def fake_stripe(monkeypatch: pytest.MonkeyPatch) -> _FakeStripeClient:
    """Replace `_get_stripe()` in the stripe route module."""

    import app.api.stripe as stripe_route

    fake = _FakeStripeClient()
    monkeypatch.setattr(stripe_route, "_get_stripe", lambda: fake)
    return fake


@pytest.fixture
def v2_price_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin known Stripe price ids so the route can resolve every tier."""

    from app.core import settings as settings_mod

    monkeypatch.setattr(settings_mod.settings, "stripe_price_month", "price_v2_month")
    monkeypatch.setattr(settings_mod.settings, "stripe_price_season", "price_v2_season")
    monkeypatch.setattr(
        settings_mod.settings, "stripe_price_lifetime", "price_v2_lifetime"
    )


# ── POST /stripe/checkout/v2 ─────────────────────────────────────────


class TestCheckoutV2:
    def test_creates_session_for_month_tier(
        self, client, fake_stripe, v2_price_settings
    ):
        resp = client.post("/stripe/checkout/v2", json={"tier": "month"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["url"].startswith("https://checkout.stripe.com/")

        params = _FakeStripeClient.last_params
        assert params["line_items"][0]["price"] == "price_v2_month"
        assert params["metadata"]["v2_tier"] == "month"
        assert "supabase_user_id" in params["metadata"]

    def test_creates_session_for_season_tier(
        self, client, fake_stripe, v2_price_settings
    ):
        resp = client.post("/stripe/checkout/v2", json={"tier": "season"})
        assert resp.status_code == 200, resp.text
        assert _FakeStripeClient.last_params["line_items"][0]["price"] == (
            "price_v2_season"
        )

    def test_creates_session_for_lifetime_tier(
        self, client, fake_stripe, v2_price_settings
    ):
        resp = client.post("/stripe/checkout/v2", json={"tier": "lifetime"})
        assert resp.status_code == 200, resp.text
        assert _FakeStripeClient.last_params["line_items"][0]["price"] == (
            "price_v2_lifetime"
        )

    def test_free_tier_rejected_with_400(self, client, fake_stripe, v2_price_settings):
        resp = client.post("/stripe/checkout/v2", json={"tier": "free"})
        assert resp.status_code == 400

    def test_unknown_tier_rejected_at_validation(
        self, client, fake_stripe, v2_price_settings
    ):
        resp = client.post("/stripe/checkout/v2", json={"tier": "platinum"})
        # Pydantic rejects unknown enum value -> 422
        assert resp.status_code == 422

    def test_missing_price_id_returns_500(self, client, fake_stripe, monkeypatch):
        from app.core import settings as settings_mod

        monkeypatch.setattr(settings_mod.settings, "stripe_price_lifetime", "")
        resp = client.post("/stripe/checkout/v2", json={"tier": "lifetime"})
        assert resp.status_code == 500
        assert "not configured" in resp.json()["error"].lower()

    def test_unauthed_returns_401(self, unauthed_client, fake_stripe):
        resp = unauthed_client.post("/stripe/checkout/v2", json={"tier": "month"})
        assert resp.status_code == 401


# ── Webhook v1 backcompat + v2 routing ───────────────────────────────


def _post_webhook(client, fake_event: dict[str, Any]):
    """Bypass Stripe signature verification by monkeypatching construct_event."""

    import stripe as stripe_lib

    class _Evt:
        def __init__(self, raw: dict[str, Any]) -> None:
            self.type = raw["type"]
            self.data = type("Data", (), {"object": raw["data"]["object"]})()

    def _fake_construct(body: str, sig: str, secret: str):
        return _Evt(fake_event)

    orig = stripe_lib.Webhook.construct_event
    stripe_lib.Webhook.construct_event = staticmethod(_fake_construct)
    try:
        return client.post(
            "/stripe/webhook",
            content=b"{}",
            headers={"stripe-signature": "t=1,v1=fake"},
        )
    finally:
        stripe_lib.Webhook.construct_event = orig


class TestWebhookV1Backcompat:
    def test_v1_session_writes_user_entitlements_only(
        self, client, fake_supabase, authed_user_id
    ):
        evt = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_v1_001",
                    "metadata": {"supabase_user_id": authed_user_id},
                    "customer": "cus_v1_001",
                    "payment_intent": "pi_v1_001",
                }
            },
        }
        resp = _post_webhook(client, evt)
        assert resp.status_code == 200
        # v1 row landed
        assert len(fake_supabase.tables["user_entitlements"]) == 1
        ue = fake_supabase.tables["user_entitlements"][0]
        assert ue["user_id"] == authed_user_id
        assert ue["source"] == "stripe"
        assert ue["stripe_payment_intent"] == "pi_v1_001"
        # No v2 row written
        assert fake_supabase.tables["entitlements"] == []


class TestWebhookV2:
    def test_session_with_tier_metadata_writes_entitlements(
        self, client, fake_supabase, authed_user_id, v2_price_settings
    ):
        evt = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_v2_001",
                    "metadata": {
                        "supabase_user_id": authed_user_id,
                        "v2_tier": "lifetime",
                    },
                    "customer": "cus_v2_001",
                    "payment_intent": "pi_v2_001",
                }
            },
        }
        resp = _post_webhook(client, evt)
        assert resp.status_code == 200
        # Both tables touched
        assert len(fake_supabase.tables["user_entitlements"]) == 1
        assert len(fake_supabase.tables["entitlements"]) == 1
        ent = fake_supabase.tables["entitlements"][0]
        assert ent["user_id"] == authed_user_id
        assert ent["tier"] == "lifetime"
        assert ent["source_platform"] == "web"
        assert ent["transaction_id"] == "pi_v2_001"
        assert ent["product_id"] == "glovebox_lifetime"
        assert ent["expires_at"] is None  # lifetime never expires

    def test_session_with_month_tier_writes_expires_at(
        self, client, fake_supabase, authed_user_id, v2_price_settings
    ):
        evt = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_v2_002",
                    "metadata": {
                        "supabase_user_id": authed_user_id,
                        "v2_tier": "month",
                    },
                    "customer": "cus_v2_002",
                    "payment_intent": "pi_v2_002",
                }
            },
        }
        _post_webhook(client, evt)
        ent = fake_supabase.tables["entitlements"][0]
        assert ent["tier"] == "month"
        assert ent["expires_at"] is not None

    def test_duplicate_webhook_is_idempotent(
        self, client, fake_supabase, authed_user_id, v2_price_settings
    ):
        evt = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_v2_003",
                    "metadata": {
                        "supabase_user_id": authed_user_id,
                        "v2_tier": "season",
                    },
                    "customer": "cus_v2_003",
                    "payment_intent": "pi_v2_003",
                }
            },
        }
        _post_webhook(client, evt)
        _post_webhook(client, evt)
        # Same (source_platform, transaction_id) -> still one row.
        assert len(fake_supabase.tables["entitlements"]) == 1

    def test_session_without_user_id_returns_400(
        self, client, fake_supabase, v2_price_settings
    ):
        evt = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_v2_004",
                    "metadata": {"v2_tier": "month"},
                    "customer": "cus_v2_004",
                    "payment_intent": "pi_v2_004",
                }
            },
        }
        resp = _post_webhook(client, evt)
        assert resp.status_code == 400
        # No rows written either side
        assert fake_supabase.tables["user_entitlements"] == []
        assert fake_supabase.tables["entitlements"] == []
