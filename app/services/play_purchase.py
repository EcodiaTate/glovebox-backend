# app/services/play_purchase.py
#
# Google Play Developer API purchase verification.
#
# The Android client hands the server a `purchaseToken` + `productId` from
# the Play Billing Library after a successful purchase. The server calls
# `androidpublisher.purchases.products.get(packageName, productId, token)`
# to confirm the purchase exists on Google's servers and pull the canonical
# `orderId` + `purchaseTimeMillis`.
#
# All three v2 SKUs (month, season, lifetime) are modelled as one-time
# Play products. Per the v2-billing-model-spec, Month and Season passes are
# consumables on Android: the app consumes them so the user can re-buy when
# the server-side expiry hits. The server treats them as duration passes
# with explicit expires_at; the Play side just delivers the receipt.
#
# Auth: service-account JSON with the `androidpublisher` OAuth scope, added
# to Play Console -> Users and Permissions for the `au.ecodia.roam` app.
# Path or inline-base64 in settings; the path takes precedence.

from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.settings import settings

logger = logging.getLogger(__name__)


# ── Public types ──────────────────────────────────────────────────────────


class PlayPurchaseError(Exception):
    """Raised when Play Developer API rejects the token or the response
    doesn't pass basic claim checks."""


@dataclass
class PlayPurchasePayload:
    """Trustworthy subset of a `purchases.products.get` response."""

    transaction_id: str  # Google's `orderId`
    product_id: str
    purchase_date: datetime
    purchase_state: int  # 0 = purchased, 1 = canceled, 2 = pending
    is_grandfather_eligible: bool
    raw_payload: dict[str, Any]


# ── Verifier ──────────────────────────────────────────────────────────────


def verify_purchase_token(
    *, purchase_token: str, product_id: str
) -> PlayPurchasePayload:
    """Confirm a Play purchase token with Google and return the parsed payload.

    Raises `PlayPurchaseError` on missing input, missing service-account
    credentials, or any Google API error.
    """

    if not purchase_token or not isinstance(purchase_token, str):
        raise PlayPurchaseError("purchase_token is required and must be a string")
    if not product_id or not isinstance(product_id, str):
        raise PlayPurchaseError("product_id is required and must be a string")

    creds_info = _load_service_account_info()
    if not creds_info:
        # Mirror the apple_receipt dev-mode posture: when no creds, we cannot
        # verify, so raise rather than silently trust.
        raise PlayPurchaseError(
            "Google Play service account is not configured "
            "(GOOGLE_PLAY_SERVICE_ACCOUNT_JSON_PATH/B64 unset)"
        )

    response = _call_purchases_products_get(
        package_name=settings.google_play_package_name,
        product_id=product_id,
        token=purchase_token,
        creds_info=creds_info,
    )

    order_id = response.get("orderId") or response.get("order_id")
    purchase_time_ms = response.get("purchaseTimeMillis") or response.get(
        "purchase_time_millis"
    )
    purchase_state = response.get("purchaseState")
    if purchase_state is None:
        purchase_state = response.get("purchase_state", 0)

    if not order_id:
        raise PlayPurchaseError(
            "Play API response missing orderId; refusing to grant entitlement"
        )

    return PlayPurchasePayload(
        transaction_id=str(order_id),
        product_id=str(product_id),
        purchase_date=_ms_to_datetime(int(purchase_time_ms or 0)),
        purchase_state=int(purchase_state),
        is_grandfather_eligible=(product_id == settings.legacy_lifetime_sku),
        raw_payload=response,
    )


# ── Internal ──────────────────────────────────────────────────────────────


def _call_purchases_products_get(
    *,
    package_name: str,
    product_id: str,
    token: str,
    creds_info: dict[str, Any],
) -> dict[str, Any]:
    """Build the androidpublisher v3 service and call purchases.products.get.

    Lazy import so the rest of the app can import this module without the
    Google client libraries installed (relevant in dev environments and in
    test runs that mock this function).
    """

    try:
        from google.oauth2 import service_account  # type: ignore[import-not-found]
        from googleapiclient.discovery import build  # type: ignore[import-not-found]
        from googleapiclient.errors import HttpError  # type: ignore[import-not-found]
    except ImportError as exc:
        raise PlayPurchaseError(
            "google-api-python-client / google-auth not available; "
            "install via requirements.txt"
        ) from exc

    creds = service_account.Credentials.from_service_account_info(
        creds_info,
        scopes=["https://www.googleapis.com/auth/androidpublisher"],
    )
    service = build("androidpublisher", "v3", credentials=creds, cache_discovery=False)

    try:
        response = (
            service.purchases()
            .products()
            .get(packageName=package_name, productId=product_id, token=token)
            .execute()
        )
    except HttpError as exc:
        raise PlayPurchaseError(
            f"Google Play Developer API rejected the token: {exc}"
        ) from exc

    return response or {}


def _load_service_account_info() -> Optional[dict[str, Any]]:
    """Load service-account credentials from path-or-inline-base64 settings.

    Path wins over base64 because the path-based path is the standard
    Cloud Run pattern (mount the JSON as a Secret volume). Returns None when
    neither is set; callers should treat that as a hard fail.
    """

    path = settings.google_play_service_account_json_path
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.error("[play_purchase] failed to read %s: %s", path, exc)
            return None

    b64 = settings.google_play_service_account_json_b64
    if b64:
        try:
            return json.loads(base64.b64decode(b64).decode("utf-8"))
        except Exception as exc:
            logger.error("[play_purchase] failed to decode b64 creds: %s", exc)
            return None

    return None


def _ms_to_datetime(ms: int) -> datetime:
    """Convert Google's `purchaseTimeMillis` to a UTC datetime."""

    if not ms:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


# ── Test helper ──────────────────────────────────────────────────────────


def _write_temp_creds(creds: dict[str, Any]) -> str:
    """Used by tests that want to exercise the file-path branch without
    touching the runtime service-account file."""

    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(creds, f)
    return path
