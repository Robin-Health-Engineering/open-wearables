"""``orderv2-getdetail``: the only place a device MAC address comes from.

Two jobs, and they pull in opposite directions, which is what these pin.

As the **MAC source** it must be generous: the response is read after a device has shipped, so a
field Withings add or a type they widen must not turn a successful lookup into an exception. They
report the MACs in two shapes at once and neither is guaranteed present.

As the **retry guard** in front of ``createuserorder`` it must be exact: "no order for this
customer_ref_id" is what allows a parcel to be sent, so the empty answer and the failed answer can
never be confused. Everything about `get_order_detail` raising rather than returning `[]` on a
failure is deliberate and is pinned here.

**Unexercised against the live API.** The entitlement arrived on 2026-09-15 and no order has been
placed, so these pin the request we send and the documented response shape — transcribed from
``dropshipment_get_order_status_object`` in Withings' OpenAPI document — not a response Withings
have actually given us.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.services.providers.withings.order_detail import WithingsOrderDetailError, get_order_detail

_POST = "app.services.providers.withings.order_detail.httpx.post"
_SIGN = "app.services.providers.withings.order_detail.sign_payload"
_SLOT = "app.services.providers.withings.order_detail.acquire_request_slot"

_CREDS = {"client_id": "client-id", "client_secret": "client-secret"}


def _response(payload: dict[str, Any]) -> MagicMock:
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def _ok(orders: list[dict[str, Any]]) -> MagicMock:
    return _response({"status": 0, "body": {"orders": orders}})


def _call(post: MagicMock, **kwargs: Any) -> Any:
    with (
        patch(_POST, post),
        patch(_SIGN, side_effect=lambda payload, *_a, **_k: {**payload, "signature": "sig"}),
        patch(_SLOT),
    ):
        return get_order_detail(**_CREDS, **kwargs)


class TestRequest:
    def test_sends_our_reference_as_a_json_array(self) -> None:
        # customer_ref_ids is documented as type json, not a repeated form field. Sending it as a
        # bare string is the mistake Withings answer with an opaque non-zero status.
        post = MagicMock(return_value=_ok([]))

        _call(post, customer_ref_ids=["order-1", "order-2"])

        sent = post.call_args.kwargs["data"]
        assert sent["action"] == "getdetail"
        assert json.loads(sent["customer_ref_ids"]) == ["order-1", "order-2"]
        assert "order_ids" not in sent

    def test_sends_withings_own_id_when_that_is_what_the_caller_has(self) -> None:
        post = MagicMock(return_value=_ok([]))

        _call(post, order_ids=["D12345678"])

        sent = post.call_args.kwargs["data"]
        assert json.loads(sent["order_ids"]) == ["D12345678"]
        assert "customer_ref_ids" not in sent

    def test_refuses_both_keys_and_neither(self) -> None:
        # "DO NOT USE WITH FOLLOWING PARAMS" on each of the two, and neither asks about nothing.
        # Refused before the round trip because Withings answer both mistakes identically.
        with pytest.raises(ValueError, match="exactly one"):
            get_order_detail(**_CREDS, customer_ref_ids=["a"], order_ids=["b"])
        with pytest.raises(ValueError, match="exactly one"):
            get_order_detail(**_CREDS)

    def test_goes_through_the_request_budget(self) -> None:
        # Same shared Withings budget as every other call in this fork; a pre-flight on every
        # fulfilment must not be the thing that exhausts it silently.
        post = MagicMock(return_value=_ok([]))
        with patch(_POST, post), patch(_SIGN, side_effect=lambda p, *a, **k: p), patch(_SLOT) as slot:
            get_order_detail(**_CREDS, customer_ref_ids=["order-1"])
        slot.assert_called_once()


class TestMacs:
    def test_reads_the_flat_mac_list(self) -> None:
        orders = _call(
            MagicMock(
                return_value=_ok(
                    [{"customer_ref_id": "order-1", "products": [{"mac_addresses": ["00:24:e4:aa:bb:cc"]}]}]
                )
            ),
            customer_ref_ids=["order-1"],
        )

        assert orders[0].macs == ["00:24:e4:aa:bb:cc"]

    def test_reads_the_nested_per_device_mac(self) -> None:
        # Withings document BOTH shapes and neither is marked required, so reading only the flat
        # list would return no MAC for a response that plainly contains one — and an empty MAC list
        # is indistinguishable from "not shipped yet", which is the silent version of this bug.
        orders = _call(
            MagicMock(
                return_value=_ok(
                    [
                        {
                            "customer_ref_id": "order-1",
                            "products": [{"devices": [{"mac_address": "00:24:e4:aa:bb:cc", "model": "Body Pro"}]}],
                        }
                    ]
                )
            ),
            customer_ref_ids=["order-1"],
        )

        assert orders[0].macs == ["00:24:e4:aa:bb:cc"]

    def test_the_same_mac_in_both_shapes_is_returned_once(self) -> None:
        # The usual case: Withings populate both. End of Program is addressed by MAC, so a
        # duplicate is a second termination call for a device already terminated.
        orders = _call(
            MagicMock(
                return_value=_ok(
                    [
                        {
                            "products": [
                                {
                                    "mac_addresses": ["00:24:e4:aa:bb:cc"],
                                    "devices": [{"mac_address": "00:24:e4:aa:bb:cc"}],
                                }
                            ]
                        }
                    ]
                )
            ),
            customer_ref_ids=["order-1"],
        )

        assert orders[0].macs == ["00:24:e4:aa:bb:cc"]

    def test_two_devices_on_one_order_both_come_back_in_order(self) -> None:
        orders = _call(
            MagicMock(
                return_value=_ok([{"products": [{"mac_addresses": ["00:24:e4:aa:bb:cc", "00:24:e4:dd:ee:ff"]}]}])
            ),
            customer_ref_ids=["order-1"],
        )

        assert orders[0].macs == ["00:24:e4:aa:bb:cc", "00:24:e4:dd:ee:ff"]

    def test_an_unshipped_order_has_no_macs_and_that_is_not_an_error(self) -> None:
        # Withings populate MAC addresses only once the parcel has left. Before that the order is
        # perfectly real and simply has none — the caller reads `status` to tell the difference.
        orders = _call(
            MagicMock(return_value=_ok([{"customer_ref_id": "order-1", "status": "PROCESSING", "products": [{}]}])),
            customer_ref_ids=["order-1"],
        )

        assert orders[0].status == "PROCESSING"
        assert orders[0].macs == []


class TestResponse:
    def test_keeps_both_status_vocabularies_verbatim(self) -> None:
        # The order's own state and the carrier's are different facts; this fork translates
        # neither, because the system that draws a timeline from them is robin-backend.
        orders = _call(
            MagicMock(
                return_value=_ok(
                    [
                        {
                            "order_id": "D12345678",
                            "customer_ref_id": "order-1",
                            "status": "SHIPPED",
                            "parcel_status": "in_transit",
                            "carrier": "UPS Parcel",
                            "carrier_service": "Next Day Air",
                            "tracking_number": "1ZY1111111",
                            "ship_date": "2026-03-15",
                        }
                    ]
                )
            ),
            customer_ref_ids=["order-1"],
        )

        order = orders[0]
        assert (order.status, order.parcel_status) == ("SHIPPED", "in_transit")
        assert order.carrier == "UPS Parcel"
        assert order.tracking_number == "1ZY1111111"

    def test_an_unknown_reference_is_an_empty_list_not_an_error(self) -> None:
        # THE load-bearing case for the retry guard. "Withings hold no order under this ref" is
        # what allows a parcel to be sent; if it raised, the guard could not distinguish it from a
        # transport failure and would either refuse every first order or ship on every failure.
        assert _call(MagicMock(return_value=_ok([])), customer_ref_ids=["never-placed"]) == []

    def test_survives_fields_withings_add_later(self) -> None:
        # Read after a device has shipped, so strictness here would turn a successful lookup into
        # an exception for no gain. Same leniency DropshipOrderResult has, for the same reason.
        orders = _call(
            MagicMock(
                return_value=_ok(
                    [
                        {
                            "customer_ref_id": "order-1",
                            "a_field_from_2027": {"nested": True},
                            "products": [{"mac_addresses": ["00:24:e4:aa:bb:cc"], "also_new": 1}],
                        }
                    ]
                )
            ),
            customer_ref_ids=["order-1"],
        )

        assert orders[0].macs == ["00:24:e4:aa:bb:cc"]

    def test_a_replacement_names_the_order_it_replaces(self) -> None:
        orders = _call(
            MagicMock(
                return_value=_ok(
                    [{"customer_ref_id": "wth-ref", "is_replacement": True, "original_customer_ref": "order-1"}]
                )
            ),
            customer_ref_ids=["order-1"],
        )

        assert orders[0].is_replacement is True
        assert orders[0].original_customer_ref == "order-1"


class TestFailures:
    """Every one of these must RAISE, never return an empty list.

    An empty list means "Withings hold no order", and on the retry-guard path that is the answer
    that permits a shipment. A failure that degraded to `[]` would ship a second parcel on every
    transport blip — the exact failure this guard exists to prevent, caused by the guard.
    """

    def test_a_non_zero_status_raises_and_carries_it(self) -> None:
        post = MagicMock(return_value=_response({"status": 277, "body": {}}))

        with pytest.raises(WithingsOrderDetailError) as raised:
            _call(post, customer_ref_ids=["order-1"])

        assert raised.value.withings_status == 277

    def test_an_http_error_raises(self) -> None:
        response = MagicMock(status_code=500, text="upstream exploded")
        response.raise_for_status.side_effect = httpx.HTTPStatusError("boom", request=MagicMock(), response=response)

        with pytest.raises(WithingsOrderDetailError):
            _call(MagicMock(return_value=response), customer_ref_ids=["order-1"])

    def test_a_transport_failure_raises(self) -> None:
        with pytest.raises(WithingsOrderDetailError):
            _call(MagicMock(side_effect=httpx.ConnectError("no route")), customer_ref_ids=["order-1"])

    def test_a_non_object_response_raises(self) -> None:
        with pytest.raises(WithingsOrderDetailError):
            _call(MagicMock(return_value=_response(["not", "an", "object"])), customer_ref_ids=["order-1"])

    def test_an_unreadable_order_raises(self) -> None:
        # Leniency covers unexpected KEYS; an unexpected TYPE still lands here. Safe to fail:
        # unlike createuserorder, nothing has shipped because of this call.
        with pytest.raises(WithingsOrderDetailError):
            _call(MagicMock(return_value=_ok([{"products": "not a list"}])), customer_ref_ids=["order-1"])

    def test_the_upstream_body_is_never_echoed_in_the_error(self) -> None:
        # The response echoes the order, and an order carries the member's home address.
        response = MagicMock(status_code=400, text='{"address": "Via Roma 1, Milano"}')
        response.raise_for_status.side_effect = httpx.HTTPStatusError("bad", request=MagicMock(), response=response)

        with pytest.raises(WithingsOrderDetailError) as raised:
            _call(MagicMock(return_value=response), customer_ref_ids=["order-1"])

        assert "Via Roma" not in str(raised.value)
        assert "Milano" not in str(raised.value)
