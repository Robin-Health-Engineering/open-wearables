"""Cellular dropshipment: create a Withings account and ship a preconfigured device in one call.

``POST /v2/dropshipment action=createuserorder`` — "ship a device to a specific member of your
program, and at the same time, create their account". It is ``createuser`` plus logistics, and
almost everything transfers: the same nonce+HMAC signing, the same profile parameters, the same
short-lived ``code`` exchanged by ``sdk_users.exchange_sdk_code``. What is new is ``order``,
``unit_pref``, and an ``orderid`` per order that this fork does not store — shipment state lives
in robin-backend, because it is commerce rather than health data.

**Cellular changes how a device reaches the internet, not how its measurements reach us.** They
land in a Withings account and we read that account through the Health Data API exactly as we
read a personally-linked one. There is no new ingestion path here, and there should not be.

Two constraints worth knowing before using this:

* The returned ``code`` is valid for **10 minutes**, materially shorter than a consumer OAuth
  code. Exchange it immediately; do not queue it.
* ``testmode`` places the order without shipping anything. It is the only way to exercise this
  end to end without sending hardware to a real address, and the entitlement question below
  means it will be the first thing anyone runs.

**Entitlement:** ``/v2/dropshipment`` is a contract-gated tier of its own. Phase 2's
``createuser`` came back **277** — "unauthorized … not allowed for this partner" — which is an
authorization verdict, not a signature (342) or validation (503) error. As of 2026-09-10 our
``client_id`` is **not** confirmed for dropshipment, so this code is written and unexercised. A
277 from here means the contract, not the request.

Reference: developer-guide/v3/integration-guide/dropship-cellular/logistics-api/create-user-order

``createorder`` (``create_order``) is the same call WITHOUT the account: dropship-only, for a device
the member activates on their own Withings account. Enabled on our client_id (2026-10-27).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from pydantic import ValidationError

from app.schemas.providers.withings.dropshipment import DropshipOrder, DropshipOrderResult, DropshipUserOrder
from app.services.providers.withings._body_logging import describe_body, upstream_reason
from app.services.providers.withings._client import WITHINGS_API_BASE_URL
from app.services.providers.withings.request_budget import acquire_request_slot
from app.services.providers.withings.sdk_users import (
    SHORTNAME_RE,
    STATUS_OK,
    measures_payload,
)
from app.services.providers.withings.signature import sign_payload
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

_DROPSHIPMENT_PATH = "/v2/dropshipment"
_ACTION = "createuserorder"
_CREATE_ORDER = "createorder"
_TIMEOUT_SECONDS = 30.0


class WithingsDropshipmentError(RuntimeError):
    """Raised when Withings declines to create the account or place the order.

    Distinct from ``WithingsSdkUserError`` because the remedies differ: an SDK failure is about
    one account, and a dropshipment failure may have created an account, placed an order, or
    both, which is what the caller has to reason about.
    """

    def __init__(
        self,
        *,
        withings_status: int | None = None,
        detail: str | None = None,
        already_exists: bool = False,
        upstream_reason: str | None = None,
    ) -> None:
        self.withings_status = withings_status
        # Withings' own words for WHY, when they gave any. Carried on the exception so the route
        # can decide whether it reaches the caller — it does not today; the log is enough, and a
        # 4xx detail on this route is the one place a leak would reach a client.
        self.upstream_reason = upstream_reason
        # See WithingsSdkUserError: the account already being ours is the caller's state, not a
        # fault, and the cellular route answers 409 for it rather than 502.
        self.already_exists = already_exists
        super().__init__(detail or f"Withings createuserorder failed (status={withings_status})")


def _post_dropshipment(
    payload: dict[str, str], *, action: str, client_id: str, client_secret: str, api_base_url: str
) -> dict[str, Any]:
    """Sign and POST one ``/v2/dropshipment`` action; return the envelope's ``body`` once Withings answered 0.

    Shared by ``createuserorder`` and ``createorder``: the signing, the shared request budget, and the
    rule that NOTHING Withings send back reaches a log or an error — the request carried a postal
    address and a signature, and the response may echo either (``redact_body`` does not cover an
    address; see ``describe_body``). Only the envelope's own ``error`` string is surfaced
    (``upstream_reason``), because without it a 503 is undiagnosable.
    """
    # Adds client_id, a fresh single-use nonce and the HMAC signature, and drops any secret.
    signed = sign_payload(payload, client_id, client_secret, api_base_url=api_base_url)

    acquire_request_slot()
    try:
        response = httpx.post(
            f"{api_base_url}{_DROPSHIPMENT_PATH}",
            data=signed,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        envelope = response.json()
    except httpx.HTTPStatusError as e:
        log_structured(
            logger,
            "error",
            f"Withings {action} HTTP error ({describe_body(e.response.text)})",
            provider="withings",
            task=action,
            status_code=e.response.status_code,
        )
        raise WithingsDropshipmentError(detail=f"Withings {action} failed (HTTP {e.response.status_code})") from e
    except Exception as e:
        log_structured(
            logger,
            "error",
            f"Withings {action} request failed: {type(e).__name__}",
            provider="withings",
            task=action,
        )
        raise WithingsDropshipmentError(detail=f"Withings {action} request failed") from e

    if not isinstance(envelope, dict):
        # A shape the leniency policy does not cover, arriving after the order may already be placed.
        raise WithingsDropshipmentError(detail=f"Withings {action} returned a non-object response")

    status = envelope.get("status")
    if status != STATUS_OK:
        reason = upstream_reason(envelope)
        log_structured(
            logger,
            "error",
            f"Withings {action} returned a non-zero status",
            provider="withings",
            task=action,
            withings_status=status,
            withings_error=reason,
        )
        raise WithingsDropshipmentError(
            withings_status=status, upstream_reason=reason, detail=f"Withings {action} failed (status={status})"
        )
    return envelope.get("body") or {}


def _acknowledged_orders(body: dict[str, Any], *, action: str) -> list[DropshipOrderResult]:
    """The orders a status-0 answer acknowledged, parsed; raise this module's error if none or any is unreadable.

    A status-0 answer acknowledging NO order is either a silent no-ship or a key-name miss on a REAL
    order (we send the block under ``order`` and read it back under ``orders``; nobody has seen the
    response), and neither may read as a clean success with an empty list. Leniency covers unexpected
    KEYS (``extra="allow"``, every field optional); an unexpected TYPE still lands here, after the
    order is placed, and is re-raised as this module's own error so the caller can tell it from a
    transport failure.
    """
    raw_orders = body.get("orders") or []
    if not raw_orders:
        log_structured(
            logger,
            "error",
            f"Withings {action} succeeded but acknowledged no orders",
            provider="withings",
            task=action,
            body_keys=sorted(body.keys()),
        )
        raise WithingsDropshipmentError(detail=f"Withings {action} returned no orders")
    if not isinstance(raw_orders, list):
        raise WithingsDropshipmentError(detail=f"Withings {action} returned an unreadable order")
    try:
        return [DropshipOrderResult.model_validate(o) for o in raw_orders]
    except ValidationError as e:
        raise WithingsDropshipmentError(detail=f"Withings {action} returned an unreadable order") from e


def create_user_order(
    *,
    client_id: str,
    client_secret: str,
    external_id: str,
    email: str,
    shortname: str,
    birthdate: int,
    gender: int,
    weight_kg: float,
    height_m: float,
    preflang: str,
    timezone: str,
    mailingpref: int,
    unit_pref: dict[str, Any],
    orders: list[DropshipOrder],
    firstname: str | None = None,
    lastname: str | None = None,
    phonenumber: str | None = None,
    recovery_code: str | None = None,
    testmode: bool = False,
    api_base_url: str = WITHINGS_API_BASE_URL,
) -> DropshipUserOrder:
    """Create a Withings account and place a dropshipment order for it, in one signed call.

    Returns the account's short-lived ``code`` alongside the orders Withings acknowledged. The
    caller exchanges the code for tokens and stores a connection; the orders go back out to
    robin-backend, which owns shipment state.

    ``unit_pref`` is required by this action where ``createuser`` defaulted it, and is what the
    device renders in — sending an empty object gives the member a scale in the wrong units.
    """
    if not SHORTNAME_RE.match(shortname):
        raise ValueError(f"shortname must match {SHORTNAME_RE.pattern} (Withings renders it on the device screen)")
    if gender not in (0, 1):
        raise ValueError("gender must be 0 (male) or 1 (female) per the Withings API")
    if mailingpref not in (0, 1):
        raise ValueError("mailingpref must be 0 (refused) or 1 (accepted)")
    if not unit_pref:
        # The one required field whose bad value is not an opaque status but a SUCCESSFUL order
        # for a device that renders the wrong units on its own screen — nothing downstream ever
        # flags it. Every other consequential field here is front-loaded for a weaker reason.
        raise ValueError("unit_pref is required for createuserorder; an empty object ships wrong units")
    if not orders:
        # A createuserorder with no order is a createuser with extra steps, and Withings would
        # either reject it or — worse — create an account nothing is shipping to.
        raise ValueError("createuserorder needs at least one order; use create_sdk_user to make an account alone")

    payload: dict[str, str] = {
        "action": _ACTION,
        "birthdate": str(birthdate),
        "email": email,
        "external_id": external_id,
        "gender": str(gender),
        "mailingpref": str(mailingpref),
        "measures": measures_payload(weight_kg, height_m),
        "order": json.dumps([order.model_dump(exclude_none=True) for order in orders]),
        "preflang": preflang,
        "shortname": shortname,
        "timezone": timezone,
        "unit_pref": json.dumps(unit_pref),
    }
    for key, value in (
        ("firstname", firstname),
        ("lastname", lastname),
        ("phonenumber", phonenumber),
        ("recovery_code", recovery_code),
    ):
        if value:
            payload[key] = value
    if testmode:
        payload["testmode"] = "1"

    body = _post_dropshipment(
        payload, action=_ACTION, client_id=client_id, client_secret=client_secret, api_base_url=api_base_url
    )
    user = body.get("user") or {}
    code = user.get("code")
    if not code:
        # status 0 with no code is a contract change, not a member-facing condition — and a
        # dangerous one, because the account and the order may both exist upstream.
        raise WithingsDropshipmentError(detail="Withings createuserorder returned no code")

    # Return OUR external_id, never the echo — the same choice ``create_sdk_user`` makes, and for
    # the same reason: ``withings_sdk_account.external_id`` says "Ours, not Withings'", it is the
    # join back to the member, and it is what the UNIQUE constraint is on. Storing a normalised
    # echo instead would break that join silently, and an echo over 128 characters would fail the
    # flush AFTER the account exists and the order is placed. A mismatch is not fatal, so it is
    # logged rather than raised.
    echoed = user.get("external_id")
    if echoed and echoed != external_id:
        log_structured(
            logger,
            "warning",
            "Withings echoed a different external_id than the one sent",
            provider="withings",
            task=_ACTION,
            sent_external_id=external_id,
            echoed_external_id=echoed,
        )

    order_results = _acknowledged_orders(body, action=_ACTION)

    log_structured(
        logger,
        "info",
        "Withings cellular user and order created",
        provider="withings",
        task=_ACTION,
        order_count=len(order_results),
        testmode=testmode,
    )
    return DropshipUserOrder(
        code=code,
        external_id=external_id,
        orders=order_results,
    )


def _with_orderid(raw: object) -> object:
    """One acknowledged order with its id under ``orderid``, as a string, as the caller reads it.

    ``createuserorder`` answers ``orderid`` and ``getdetail`` answers ``order_id``; nobody has seen a
    ``createorder`` answer, so either key is accepted, and a numeric id becomes a string rather than an
    "unreadable order" after the parcel is placed. Anything that is not an object passes through
    untouched, for ``model_validate`` to refuse.
    """
    if not isinstance(raw, dict):
        return raw
    order_id = raw.get("orderid") or raw.get("order_id")
    return {**raw, "orderid": str(order_id) if order_id is not None else None}


def create_order(
    *,
    client_id: str,
    client_secret: str,
    orders: list[DropshipOrder],
    testmode: bool = False,
    api_base_url: str = WITHINGS_API_BASE_URL,
) -> list[DropshipOrderResult]:
    """Place a dropshipment order WITHOUT creating a Withings account (``action=createorder``, spec O1).

    For a device the member sets up themselves (the non-cellular Body Comp): Withings ship the parcel
    and that is all. The member creates or signs in to their OWN Withings account in Health Mate and
    links it to Robin later through consumer OAuth. Same signing and the same ``order`` block as
    ``createuserorder``; no profile, no ``unit_pref``, no code, and nothing is stored here.

    An acknowledged order may come back without an id (``orderid`` None): the route resolves it from
    getdetail. Not idempotent at Withings — the route's pre-flight is what stops a retry shipping twice.
    """
    if not orders:
        raise ValueError("createorder needs at least one order")
    payload: dict[str, str] = {
        "action": _CREATE_ORDER,
        "order": json.dumps([order.model_dump(exclude_none=True) for order in orders]),
    }
    if testmode:
        payload["testmode"] = "1"

    body = _post_dropshipment(
        payload, action=_CREATE_ORDER, client_id=client_id, client_secret=client_secret, api_base_url=api_base_url
    )
    raw_orders = body.get("orders")
    results = _acknowledged_orders(
        {**body, "orders": [_with_orderid(o) for o in raw_orders] if isinstance(raw_orders, list) else raw_orders},
        action=_CREATE_ORDER,
    )

    log_structured(
        logger,
        "info",
        "Withings order placed without an account",
        provider="withings",
        task=_CREATE_ORDER,
        order_count=len(results),
        testmode=testmode,
    )
    return results
