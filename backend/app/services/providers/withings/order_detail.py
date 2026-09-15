"""Ask Withings what became of an order we placed — and, once it ships, for its device MACs.

``POST /v2/order action=getdetail``. Same signed surface as ``createuserorder``: nonce + HMAC in
the application's own name, so ``signature.sign_payload`` covers it unchanged and no new
credential appears.

**Why this fork needs it at all**, given that order state deliberately lives in robin-backend:

* **The MAC.** ``Devicev2-endpartnerprogram`` is addressed by MAC address and is the only way to
  stop a cellular plan billing after a member leaves. ``end_program`` has been written and
  unreachable since #7 because nothing produced one — the createuserorder response does not carry
  a MAC, and Withings only populate ``products[].mac_addresses`` **once the order has shipped**.
  This is the documented source, and it is a signed call, so it cannot be made from
  robin-backend: the client secret lives here and should live in exactly one place.
* **The retry guard.** ``createuserorder`` is not idempotent and Withings honour no idempotency
  key, so a repeated fulfilment ships a second parcel. ``customer_ref_id`` is ours and unique per
  order, and this endpoint accepts it as a lookup key — which makes "has this order already been
  placed?" answerable against the authority rather than against a local proxy.

It does NOT store anything. The answer goes straight back out to the caller, for the same reason
``create_user_order`` returns its orders rather than persisting them: shipment state is commerce.

One shape trap worth stating, because it is silent. ``order_ids`` and ``customer_ref_ids`` are
each documented ``required`` and each carries "DO NOT USE WITH FOLLOWING PARAMS" naming the
other, so exactly one may be sent. They are JSON arrays encoded as strings, not repeated form
fields.

Reference: api-reference#operation/orderv2-getdetail
"""

from __future__ import annotations

import json
import logging

import httpx
from pydantic import ValidationError

from app.schemas.providers.withings.order_detail import OrderDetail
from app.services.providers.withings._client import WITHINGS_API_BASE_URL
from app.services.providers.withings.oauth import describe_body
from app.services.providers.withings.request_budget import acquire_request_slot
from app.services.providers.withings.sdk_users import STATUS_OK
from app.services.providers.withings.signature import sign_payload
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

_ORDER_PATH = "/v2/order"
_ACTION = "getdetail"
_TIMEOUT_SECONDS = 30.0


class WithingsOrderDetailError(RuntimeError):
    """Raised when Withings will not tell us about an order.

    Distinct from ``WithingsDropshipmentError`` because the remedies are opposite: a dropshipment
    failure may have shipped hardware, and a failure HERE has changed nothing at all. A caller may
    retry this freely, which is exactly what must never be said of the other one.
    """

    def __init__(self, *, withings_status: int | None = None, detail: str | None = None) -> None:
        self.withings_status = withings_status
        super().__init__(detail or f"Withings getdetail failed (status={withings_status})")


def get_order_detail(
    *,
    client_id: str,
    client_secret: str,
    customer_ref_ids: list[str] | None = None,
    order_ids: list[str] | None = None,
    customerid: str | None = None,
    api_base_url: str = WITHINGS_API_BASE_URL,
) -> list[OrderDetail]:
    """Look up orders by OUR reference or by Withings'. Exactly one of the two.

    Returns what Withings acknowledged — which for a ref they do not know is an EMPTY LIST, not an
    error. That distinction is the whole value of this call as a retry guard: "no order for this
    customer_ref_id" and "an order exists for it" are different answers to the same question, and
    only one of them means a parcel is already on its way.
    """
    if bool(customer_ref_ids) == bool(order_ids):
        # Sending both is documented as forbidden and sending neither asks about nothing. Checked
        # here because Withings answer either mistake with an opaque non-zero status.
        raise ValueError("getdetail takes exactly one of customer_ref_ids or order_ids")

    payload: dict[str, str] = {"action": _ACTION}
    if customer_ref_ids:
        payload["customer_ref_ids"] = json.dumps(customer_ref_ids)
    else:
        payload["order_ids"] = json.dumps(order_ids)
    if customerid:
        payload["customerid"] = customerid

    signed = sign_payload(payload, client_id, client_secret, api_base_url=api_base_url)

    acquire_request_slot()
    try:
        response = httpx.post(
            f"{api_base_url}{_ORDER_PATH}",
            data=signed,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        envelope = response.json()
    except httpx.HTTPStatusError as e:
        # NOT logged: the response echoes the order, and an order carries the member's address.
        # See `describe_body` — redaction does not reach it.
        log_structured(
            logger,
            "error",
            f"Withings getdetail HTTP error ({describe_body(e.response.text)})",
            provider="withings",
            task=_ACTION,
            status_code=e.response.status_code,
        )
        raise WithingsOrderDetailError(detail=f"Withings getdetail failed (HTTP {e.response.status_code})") from e
    except Exception as e:
        log_structured(
            logger,
            "error",
            f"Withings getdetail request failed: {type(e).__name__}",
            provider="withings",
            task=_ACTION,
        )
        raise WithingsOrderDetailError(detail="Withings getdetail request failed") from e

    if not isinstance(envelope, dict):
        raise WithingsOrderDetailError(detail="Withings getdetail returned a non-object response")

    status_code = envelope.get("status")
    if status_code != STATUS_OK:
        # No body echo, same rule as dropshipment: the response repeats the order's address.
        log_structured(
            logger,
            "error",
            "Withings getdetail returned a non-zero status",
            provider="withings",
            task=_ACTION,
            withings_status=status_code,
        )
        raise WithingsOrderDetailError(withings_status=status_code)

    raw_orders = (envelope.get("body") or {}).get("orders") or []
    try:
        orders = [OrderDetail.model_validate(o) for o in raw_orders]
    except ValidationError as e:
        # Unlike the dropshipment equivalent this is genuinely safe to fail: nothing has shipped
        # because of this call, so the caller can retry or fall back to what it already knows.
        raise WithingsOrderDetailError(detail="Withings getdetail returned an unreadable order") from e

    log_structured(
        logger,
        "info",
        "Withings order detail fetched",
        provider="withings",
        task=_ACTION,
        order_count=len(orders),
        # Counts, never the MACs themselves — a MAC identifies a member's hardware and the End of
        # Program call is addressed by it.
        mac_count=sum(len(order.macs) for order in orders),
    )
    return orders
