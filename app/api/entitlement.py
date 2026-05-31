# app/api/entitlement.py
#
# v2 tiered-entitlement endpoints.
#
#   GET  /entitlement         - current effective tier for the authed user
#   POST /entitlement/redeem  - client-supplied receipt -> server verifies -> grant
#
# Both endpoints return `EntitlementResponse` shaped payloads so a successful
# redeem can be consumed by the same client code path that consumes GET.

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.core.auth import AuthUser, get_current_user
from app.core.billing_models import (
    EntitlementResponse,
    RedeemRequest,
    RedeemResponse,
    SourcePlatform,
    Tier,
)
from app.core.error_models import ErrorResponse
from app.services.apple_receipt import (
    AppleTransactionPayload,
    ReceiptError,
    verify_signed_transaction,
)
from app.services.entitlements import (
    expiry_for_tier,
    get_current_entitlement,
    tier_from_product_id,
    upsert_entitlement,
)
from app.services.play_purchase import (
    PlayPurchaseError,
    PlayPurchasePayload,
    verify_purchase_token,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/entitlement", tags=["entitlement"])


def _error(message: str, status_code: int) -> JSONResponse:
    """Match the existing `{"error": "..."}` shape used across the backend."""

    return JSONResponse(
        ErrorResponse(error=message).model_dump(), status_code=status_code
    )


@router.get(
    "",
    response_model=EntitlementResponse,
    responses={
        401: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def get_entitlement(
    user: AuthUser = Depends(get_current_user),
) -> EntitlementResponse:
    """Resolve and return the authenticated user's current effective tier.

    Source of truth for all three native clients (`glovebox-ios`,
    `glovebox-android`, `glovebox-web`). Resolution order is in
    `app/services/entitlements.py`. The endpoint is cheap (one indexed read,
    optional second read for the legacy grandfather check) and called on
    every cold start of the v2 clients plus before any paywalled action.
    """

    return get_current_entitlement(user.id)


@router.post(
    "/redeem",
    response_model=RedeemResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
)
async def redeem_entitlement(
    body: RedeemRequest,
    user: AuthUser = Depends(get_current_user),
) -> RedeemResponse | JSONResponse:
    """Verify a platform-specific receipt and grant the corresponding tier.

    iOS and Android receipts are verified server-to-server against Apple's
    JWS public keys / Google's Play Developer API. The web path is
    deliberately rejected: web purchases flow through the Stripe Checkout +
    webhook path, never through this endpoint.

    Grandfather behaviour: when the verified receipt names the legacy
    `roam_unlimited` SKU, the server grants `Tier.LIFETIME` regardless of
    the tier the client suggested. The response's `grandfathered=True`
    flag tells the client to update its UI without surprise.

    Idempotency: the `(source_platform, transaction_id)` unique index on
    `public.entitlements` absorbs duplicate redemptions from flaky clients
    or auto-retries. The endpoint always returns the user's current
    effective entitlement after the call, so a duplicate redeem returns
    the same `granted=True` shape as the first one.
    """

    if body.platform == SourcePlatform.LEGACY:
        return _error("legacy platform is read-side only", 400)
    if body.platform == SourcePlatform.WEB:
        return _error(
            "web purchases use the Stripe webhook; do not POST /entitlement/redeem",
            400,
        )

    try:
        if body.platform == SourcePlatform.IOS:
            verified = _verify_ios(body)
        elif body.platform == SourcePlatform.ANDROID:
            verified = _verify_android(body)
        else:
            return _error(f"unsupported platform {body.platform.value!r}", 400)
    except (ReceiptError, PlayPurchaseError) as exc:
        logger.info(
            "[entitlement/redeem] verification rejected for user=%s platform=%s: %s",
            user.id,
            body.platform.value,
            exc,
        )
        return _error(str(exc), 403)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(
            "[entitlement/redeem] verifier raised unexpected error for user=%s: %s",
            user.id,
            exc,
            exc_info=True,
        )
        return _error("receipt verification failed", 502)

    # Resolve tier. Grandfather wins: if the verified receipt names the legacy
    # roam_unlimited SKU we always grant Lifetime, even if the client asked
    # for a Month pass with the wrong product id.
    grandfathered = verified["is_grandfather_eligible"]
    if grandfathered:
        tier = Tier.LIFETIME
    else:
        try:
            tier = tier_from_product_id(verified["product_id"])
        except ValueError as exc:
            return _error(str(exc), 400)
        if tier == Tier.FREE:
            return _error("verified product maps to free tier", 400)

    try:
        upsert_entitlement(
            user_id=user.id,
            tier=tier,
            source_platform=body.platform,
            product_id=verified["product_id"],
            transaction_id=verified["transaction_id"],
            expires_at=expiry_for_tier(tier, verified["purchase_date"]),
            raw_receipt=verified["raw_payload"],
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(
            "[entitlement/redeem] upsert failed for user=%s txn=%s: %s",
            user.id,
            verified["transaction_id"],
            exc,
            exc_info=True,
        )
        return _error("could not record entitlement", 502)

    current = get_current_entitlement(user.id)
    return RedeemResponse(
        granted=True,
        entitlement=current,
        grandfathered=grandfathered,
    )


# ── Per-platform verification adapters ────────────────────────────────────


def _verify_ios(body: RedeemRequest) -> dict[str, Any]:
    """Pull the iOS JWS string from the receipt envelope and verify it.

    The client sends `receipt: {"signed_transaction_info": "<JWS>"}`. Raises
    ReceiptError when the envelope is missing the expected key or the JWS
    fails verification.
    """

    signed = body.receipt.get("signed_transaction_info") or body.receipt.get(
        "signedTransactionInfo"
    )
    if not signed:
        raise ReceiptError(
            "ios receipt must contain signed_transaction_info (JWS string)"
        )
    payload: AppleTransactionPayload = verify_signed_transaction(signed)
    return {
        "transaction_id": payload.transaction_id,
        "product_id": payload.product_id,
        "purchase_date": payload.purchase_date,
        "is_grandfather_eligible": payload.is_grandfather_eligible,
        "raw_payload": payload.raw_payload,
    }


def _verify_android(body: RedeemRequest) -> dict[str, Any]:
    """Pull the Play purchase token + product id from the envelope and verify.

    The client sends `receipt: {"purchase_token": "...", "product_id": "..."}`
    (`product_id` in the envelope may differ from `body.product_id` for the
    grandfather case; the envelope value is authoritative for the Play
    lookup so the server can verify the legacy SKU when the client doesn't
    know to ask for it).
    """

    token = body.receipt.get("purchase_token") or body.receipt.get("purchaseToken")
    inner_product_id = (
        body.receipt.get("product_id")
        or body.receipt.get("productId")
        or body.product_id
    )
    if not token:
        raise PlayPurchaseError("android receipt must contain purchase_token")
    payload: PlayPurchasePayload = verify_purchase_token(
        purchase_token=token, product_id=inner_product_id
    )
    if payload.purchase_state != 0:
        raise PlayPurchaseError(
            f"play purchase_state is {payload.purchase_state}, expected 0 (purchased)"
        )
    return {
        "transaction_id": payload.transaction_id,
        "product_id": payload.product_id,
        "purchase_date": payload.purchase_date,
        "is_grandfather_eligible": payload.is_grandfather_eligible,
        "raw_payload": payload.raw_payload,
    }
