"""Phase E - POST /entitlement/redeem.

Tests stub the iOS + Android verifier services so we exercise the route's
per-platform routing, grandfather logic, tier resolution, and idempotency
without needing real receipts or real Apple/Play creds.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest


# ── Fakes ─────────────────────────────────────────────────────────────


def _ios_payload(*, product_id: str, txn_id: str, grandfather: bool = False) -> Any:
    from app.services.apple_receipt import AppleTransactionPayload

    return AppleTransactionPayload(
        transaction_id=txn_id,
        original_transaction_id=txn_id,
        product_id=product_id,
        bundle_id="au.ecodia.roam",
        purchase_date=datetime.now(timezone.utc),
        is_grandfather_eligible=grandfather,
        raw_payload={"productId": product_id, "transactionId": txn_id},
    )


def _play_payload(
    *,
    product_id: str,
    order_id: str,
    grandfather: bool = False,
    purchase_state: int = 0,
) -> Any:
    from app.services.play_purchase import PlayPurchasePayload

    return PlayPurchasePayload(
        transaction_id=order_id,
        product_id=product_id,
        purchase_date=datetime.now(timezone.utc),
        purchase_state=purchase_state,
        is_grandfather_eligible=grandfather,
        raw_payload={"orderId": order_id, "productId": product_id},
    )


@pytest.fixture
def fake_apple_verifier(monkeypatch):
    """Patch the verifier reference imported into the route module."""
    import app.api.entitlement as ent_route

    holder: dict[str, Any] = {"payload": None, "error": None}

    def _verify(_jws: str):
        if holder["error"]:
            raise holder["error"]
        return holder["payload"]

    monkeypatch.setattr(ent_route, "verify_signed_transaction", _verify)
    return holder


@pytest.fixture
def fake_play_verifier(monkeypatch):
    import app.api.entitlement as ent_route

    holder: dict[str, Any] = {"payload": None, "error": None}

    def _verify(*, purchase_token: str, product_id: str):
        if holder["error"]:
            raise holder["error"]
        return holder["payload"]

    monkeypatch.setattr(ent_route, "verify_purchase_token", _verify)
    return holder


# ── Platform rejection paths ─────────────────────────────────────────────


class TestPlatformRejections:
    def test_web_platform_rejected(self, client):
        resp = client.post(
            "/entitlement/redeem",
            json={
                "platform": "web",
                "product_id": "glovebox_lifetime",
                "receipt": {"stripe_session_id": "cs_x"},
            },
        )
        assert resp.status_code == 400
        assert "Stripe" in resp.json()["error"]

    def test_legacy_platform_rejected(self, client):
        resp = client.post(
            "/entitlement/redeem",
            json={
                "platform": "legacy",
                "product_id": "roam_unlimited",
                "receipt": {},
            },
        )
        assert resp.status_code == 400

    def test_unauthed_returns_401(self, unauthed_client):
        resp = unauthed_client.post(
            "/entitlement/redeem",
            json={
                "platform": "ios",
                "product_id": "glovebox_pass_month",
                "receipt": {"signed_transaction_info": "jws"},
            },
        )
        assert resp.status_code == 401


# ── iOS path ─────────────────────────────────────────────────────────────


class TestRedeemIOS:
    def test_month_pass_grants_with_expiry(
        self, client, fake_apple_verifier, fake_supabase, authed_user_id
    ):
        fake_apple_verifier["payload"] = _ios_payload(
            product_id="glovebox_pass_month", txn_id="ios-tx-month-1"
        )

        resp = client.post(
            "/entitlement/redeem",
            json={
                "platform": "ios",
                "product_id": "glovebox_pass_month",
                "receipt": {"signed_transaction_info": "jws-month"},
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["granted"] is True
        assert body["grandfathered"] is False
        assert body["entitlement"]["tier"] == "month"
        assert body["entitlement"]["expires_at"] is not None

        # Row landed in fake supabase with idempotency key shape
        rows = fake_supabase.tables["entitlements"]
        assert len(rows) == 1
        assert rows[0]["transaction_id"] == "ios-tx-month-1"
        assert rows[0]["source_platform"] == "ios"

    def test_lifetime_grants_with_null_expires_at(
        self, client, fake_apple_verifier, fake_supabase, authed_user_id
    ):
        fake_apple_verifier["payload"] = _ios_payload(
            product_id="glovebox_lifetime", txn_id="ios-tx-life-1"
        )
        body = client.post(
            "/entitlement/redeem",
            json={
                "platform": "ios",
                "product_id": "glovebox_lifetime",
                "receipt": {"signed_transaction_info": "jws-life"},
            },
        ).json()
        assert body["entitlement"]["tier"] == "lifetime"
        assert body["entitlement"]["expires_at"] is None

    def test_grandfather_legacy_sku_lands_as_lifetime(
        self, client, fake_apple_verifier, fake_supabase, authed_user_id
    ):
        fake_apple_verifier["payload"] = _ios_payload(
            product_id="roam_unlimited", txn_id="ios-tx-legacy-1", grandfather=True
        )
        body = client.post(
            "/entitlement/redeem",
            json={
                "platform": "ios",
                "product_id": "glovebox_pass_month",  # wrong on purpose
                "receipt": {"signed_transaction_info": "jws-legacy"},
            },
        ).json()
        assert body["granted"] is True
        assert body["grandfathered"] is True
        assert body["entitlement"]["tier"] == "lifetime"

    def test_idempotent_on_duplicate_txn(
        self, client, fake_apple_verifier, fake_supabase, authed_user_id
    ):
        fake_apple_verifier["payload"] = _ios_payload(
            product_id="glovebox_pass_season", txn_id="ios-tx-season-1"
        )
        first = client.post(
            "/entitlement/redeem",
            json={
                "platform": "ios",
                "product_id": "glovebox_pass_season",
                "receipt": {"signed_transaction_info": "jws"},
            },
        )
        second = client.post(
            "/entitlement/redeem",
            json={
                "platform": "ios",
                "product_id": "glovebox_pass_season",
                "receipt": {"signed_transaction_info": "jws"},
            },
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert len(fake_supabase.tables["entitlements"]) == 1

    def test_missing_jws_returns_403(self, client, fake_apple_verifier):
        resp = client.post(
            "/entitlement/redeem",
            json={
                "platform": "ios",
                "product_id": "glovebox_lifetime",
                "receipt": {},
            },
        )
        assert resp.status_code == 403
        assert "signed_transaction_info" in resp.json()["error"]

    def test_verification_rejection_returns_403(self, client, fake_apple_verifier):
        from app.services.apple_receipt import ReceiptError

        fake_apple_verifier["error"] = ReceiptError("bundleId mismatch: foo vs bar")
        resp = client.post(
            "/entitlement/redeem",
            json={
                "platform": "ios",
                "product_id": "glovebox_lifetime",
                "receipt": {"signed_transaction_info": "jws"},
            },
        )
        assert resp.status_code == 403
        assert "bundleId mismatch" in resp.json()["error"]


# ── Android path ─────────────────────────────────────────────────────────


class TestRedeemAndroid:
    def test_month_pass_grants(
        self, client, fake_play_verifier, fake_supabase, authed_user_id
    ):
        fake_play_verifier["payload"] = _play_payload(
            product_id="glovebox_pass_month", order_id="GPA.0000-month"
        )
        body = client.post(
            "/entitlement/redeem",
            json={
                "platform": "android",
                "product_id": "glovebox_pass_month",
                "receipt": {
                    "purchase_token": "play-tok",
                    "product_id": "glovebox_pass_month",
                },
            },
        ).json()
        assert body["granted"] is True
        assert body["entitlement"]["tier"] == "month"
        rows = fake_supabase.tables["entitlements"]
        assert len(rows) == 1
        assert rows[0]["transaction_id"] == "GPA.0000-month"
        assert rows[0]["source_platform"] == "android"

    def test_grandfather_legacy_sku_on_android(
        self, client, fake_play_verifier, fake_supabase, authed_user_id
    ):
        fake_play_verifier["payload"] = _play_payload(
            product_id="roam_unlimited",
            order_id="GPA.legacy",
            grandfather=True,
        )
        body = client.post(
            "/entitlement/redeem",
            json={
                "platform": "android",
                "product_id": "roam_unlimited",
                "receipt": {
                    "purchase_token": "play-tok-legacy",
                    "product_id": "roam_unlimited",
                },
            },
        ).json()
        assert body["entitlement"]["tier"] == "lifetime"
        assert body["grandfathered"] is True

    def test_non_purchased_state_returns_403(
        self, client, fake_play_verifier, fake_supabase
    ):
        fake_play_verifier["payload"] = _play_payload(
            product_id="glovebox_pass_month",
            order_id="GPA.pending",
            purchase_state=2,  # pending
        )
        resp = client.post(
            "/entitlement/redeem",
            json={
                "platform": "android",
                "product_id": "glovebox_pass_month",
                "receipt": {
                    "purchase_token": "tok",
                    "product_id": "glovebox_pass_month",
                },
            },
        )
        assert resp.status_code == 403
        assert "purchase_state" in resp.json()["error"]

    def test_missing_token_returns_403(self, client, fake_play_verifier):
        resp = client.post(
            "/entitlement/redeem",
            json={
                "platform": "android",
                "product_id": "glovebox_pass_month",
                "receipt": {"product_id": "glovebox_pass_month"},
            },
        )
        assert resp.status_code == 403
        assert "purchase_token" in resp.json()["error"]
