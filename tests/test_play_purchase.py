"""Phase D - Google Play Developer API purchase verification.

Tests mock `_call_purchases_products_get` so CI doesn't need real Google
service-account credentials or network access. Covers:
  - Happy path: canned Play response decodes into PlayPurchasePayload
  - Missing service-account credentials raises hard
  - product_id == roam_unlimited flips grandfather flag
  - Missing orderId in response raises
  - Empty inputs raise
  - The base64 + path credential loading branches
"""

from __future__ import annotations

import base64
import json

import pytest

from app.services.play_purchase import (
    PlayPurchaseError,
    PlayPurchasePayload,
    _load_service_account_info,
    _ms_to_datetime,
    _write_temp_creds,
    verify_purchase_token,
)


_FAKE_SA = {
    "type": "service_account",
    "project_id": "play-test",
    "private_key_id": "kid",
    "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
    "client_email": "play-test@example.iam.gserviceaccount.com",
    "client_id": "0",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
}


# ── verify_purchase_token ────────────────────────────────────────────────


class TestVerifyPurchaseToken:
    def test_happy_path_month_pass(self, monkeypatch):
        from app.core import settings as settings_mod
        from app.services import play_purchase as pp

        monkeypatch.setattr(
            settings_mod.settings,
            "google_play_service_account_json_b64",
            base64.b64encode(json.dumps(_FAKE_SA).encode()).decode(),
        )
        monkeypatch.setattr(
            settings_mod.settings, "google_play_service_account_json_path", ""
        )

        canned = {
            "orderId": "GPA.0000-0000-0000-00001",
            "purchaseTimeMillis": "1717000000000",
            "purchaseState": 0,
            "purchaseToken": "tok",
            "productId": "glovebox_pass_month",
            "kind": "androidpublisher#productPurchase",
        }
        monkeypatch.setattr(pp, "_call_purchases_products_get", lambda **_kw: canned)

        result = verify_purchase_token(
            purchase_token="tok-from-play",
            product_id="glovebox_pass_month",
        )
        assert isinstance(result, PlayPurchasePayload)
        assert result.transaction_id == "GPA.0000-0000-0000-00001"
        assert result.product_id == "glovebox_pass_month"
        assert result.purchase_state == 0
        assert result.is_grandfather_eligible is False
        assert result.purchase_date.year == 2024

    def test_grandfather_sku_flips_flag(self, monkeypatch):
        from app.core import settings as settings_mod
        from app.services import play_purchase as pp

        monkeypatch.setattr(
            settings_mod.settings,
            "google_play_service_account_json_b64",
            base64.b64encode(json.dumps(_FAKE_SA).encode()).decode(),
        )

        canned = {
            "orderId": "GPA.legacy",
            "purchaseTimeMillis": "1700000000000",
            "purchaseState": 0,
            "productId": "roam_unlimited",
        }
        monkeypatch.setattr(pp, "_call_purchases_products_get", lambda **_kw: canned)

        result = verify_purchase_token(
            purchase_token="tok-legacy",
            product_id="roam_unlimited",
        )
        assert result.is_grandfather_eligible is True

    def test_missing_creds_raises(self, monkeypatch):
        from app.core import settings as settings_mod

        monkeypatch.setattr(
            settings_mod.settings, "google_play_service_account_json_b64", ""
        )
        monkeypatch.setattr(
            settings_mod.settings, "google_play_service_account_json_path", ""
        )

        with pytest.raises(PlayPurchaseError, match="not configured"):
            verify_purchase_token(
                purchase_token="tok", product_id="glovebox_pass_month"
            )

    def test_missing_order_id_raises(self, monkeypatch):
        from app.core import settings as settings_mod
        from app.services import play_purchase as pp

        monkeypatch.setattr(
            settings_mod.settings,
            "google_play_service_account_json_b64",
            base64.b64encode(json.dumps(_FAKE_SA).encode()).decode(),
        )

        canned = {
            "purchaseTimeMillis": "1717000000000",
            "purchaseState": 0,
        }
        monkeypatch.setattr(pp, "_call_purchases_products_get", lambda **_kw: canned)

        with pytest.raises(PlayPurchaseError, match="orderId"):
            verify_purchase_token(
                purchase_token="tok", product_id="glovebox_pass_month"
            )

    def test_empty_purchase_token_raises(self):
        with pytest.raises(PlayPurchaseError, match="purchase_token is required"):
            verify_purchase_token(purchase_token="", product_id="x")

    def test_empty_product_id_raises(self):
        with pytest.raises(PlayPurchaseError, match="product_id is required"):
            verify_purchase_token(purchase_token="tok", product_id="")


# ── _load_service_account_info ───────────────────────────────────────────


class TestLoadServiceAccountInfo:
    def test_path_wins_over_b64(self, monkeypatch, tmp_path):
        from app.core import settings as settings_mod

        path_creds = dict(_FAKE_SA, project_id="from-path")
        b64_creds = dict(_FAKE_SA, project_id="from-b64")
        path = _write_temp_creds(path_creds)

        monkeypatch.setattr(
            settings_mod.settings, "google_play_service_account_json_path", path
        )
        monkeypatch.setattr(
            settings_mod.settings,
            "google_play_service_account_json_b64",
            base64.b64encode(json.dumps(b64_creds).encode()).decode(),
        )

        loaded = _load_service_account_info()
        assert loaded is not None
        assert loaded["project_id"] == "from-path"

    def test_b64_used_when_path_unset(self, monkeypatch):
        from app.core import settings as settings_mod

        monkeypatch.setattr(
            settings_mod.settings, "google_play_service_account_json_path", ""
        )
        monkeypatch.setattr(
            settings_mod.settings,
            "google_play_service_account_json_b64",
            base64.b64encode(json.dumps(_FAKE_SA).encode()).decode(),
        )

        loaded = _load_service_account_info()
        assert loaded is not None
        assert loaded["project_id"] == "play-test"

    def test_nonexistent_path_falls_back_to_b64(self, monkeypatch):
        from app.core import settings as settings_mod

        monkeypatch.setattr(
            settings_mod.settings,
            "google_play_service_account_json_path",
            "/does/not/exist.json",
        )
        monkeypatch.setattr(
            settings_mod.settings,
            "google_play_service_account_json_b64",
            base64.b64encode(json.dumps(_FAKE_SA).encode()).decode(),
        )
        loaded = _load_service_account_info()
        assert loaded is not None
        assert loaded["project_id"] == "play-test"

    def test_neither_set_returns_none(self, monkeypatch):
        from app.core import settings as settings_mod

        monkeypatch.setattr(
            settings_mod.settings, "google_play_service_account_json_path", ""
        )
        monkeypatch.setattr(
            settings_mod.settings, "google_play_service_account_json_b64", ""
        )
        assert _load_service_account_info() is None


# ── _ms_to_datetime ──────────────────────────────────────────────────────


class TestMsToDatetime:
    def test_zero_returns_epoch(self):
        assert _ms_to_datetime(0).year == 1970

    def test_known_ms(self):
        dt = _ms_to_datetime(1717000000000)
        assert dt.year == 2024
        assert dt.tzinfo is not None
