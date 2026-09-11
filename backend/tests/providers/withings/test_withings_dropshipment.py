"""``createuserorder``: what we send Withings, and what we refuse to send.

This is the call that creates a member's Withings account AND puts hardware in the post, in one
signed request. Two consequences shape these tests.

Everything validated locally is validated because the alternative is a physical failure with a
cost — a device shipped to an address Withings could not parse, or an account whose shortname
renders wrong on the hardware itself. Withings answers all of those with the same opaque non-zero
status, so a check here is worth more than the round trip.

And the response's two halves belong to different systems. The ``code`` becomes a connection in
this fork; the orders belong to robin-backend. These pin that the orders come back out rather
than being stored.

**Unexercised against the live API.** Our client_id is not confirmed for /v2/dropshipment as of
2026-09-10 — see the module docstring. These pin the request we would send, not a response
Withings has actually given us.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.schemas.providers.withings.dropshipment import (
    DropshipAddress,
    DropshipOrder,
    DropshipProduct,
    DropshipUserOrder,
)
from app.services.providers.withings.dropshipment import WithingsDropshipmentError, create_user_order

_POST = "app.services.providers.withings.dropshipment.httpx.post"
_SIGN = "app.services.providers.withings.dropshipment.sign_payload"
_SLOT = "app.services.providers.withings.dropshipment.acquire_request_slot"

_PROFILE = {
    "client_id": "client-id",
    "client_secret": "client-secret",
    "external_id": "profile-1#order-1",
    "email": "member@example.com",
    "shortname": "FRA",
    "birthdate": 643248000,
    "gender": 0,
    "weight_kg": 75.4,
    "height_m": 1.78,
    "preflang": "it_IT",
    "timezone": "Europe/Rome",
    "mailingpref": 0,
    "unit_pref": {"weight": 1, "height": 6},
}


def _address(**overrides: Any) -> DropshipAddress:
    return DropshipAddress(
        name="Francesco Rossi",
        email="member@example.com",
        address1="Via Roma 1",
        city="Milano",
        zip="20121",
        country="IT",
        **overrides,
    )


def _order(**overrides: Any) -> DropshipOrder:
    fields: dict[str, Any] = {
        "customer_ref_id": "order-1",
        "address": _address(),
        "products": [DropshipProduct(quantity=1, ean="3700546705526")],
    }
    fields.update(overrides)
    return DropshipOrder(**fields)


def _ok(body: dict | None = None, status: int = 0) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {
        "status": status,
        "body": body
        if body is not None
        else {
            "user": {"code": "auth-code", "external_id": "profile-1#order-1"},
            "orders": [{"orderid": "WO-1", "status": "PENDING", "address": {"city": "Milano"}}],
        },
    }
    response.raise_for_status.return_value = None
    return response


def _call(post: Any, **overrides: Any) -> DropshipUserOrder:
    kwargs = {**_PROFILE, "orders": [_order()], "api_base_url": "https://wbsapi.example.net"}
    kwargs.update(overrides)
    with (
        patch(_SIGN, side_effect=lambda payload, *_a, **_k: {**payload, "signature": "sig", "nonce": "n"}),
        patch(_SLOT),
        patch(_POST, post),
    ):
        return create_user_order(**kwargs)


class TestTheRequest:
    def test_posts_a_signed_createuserorder_to_the_dropshipment_service(self) -> None:
        post = MagicMock(return_value=_ok())

        _call(post)

        assert post.call_args.args[0] == "https://wbsapi.example.net/v2/dropshipment"
        sent = post.call_args.kwargs["data"]
        assert sent["action"] == "createuserorder"
        assert sent["signature"] == "sig"
        assert "client_secret" not in sent

    def test_sends_the_order_block_as_json(self) -> None:
        import json

        post = MagicMock(return_value=_ok())

        _call(post)

        order = json.loads(post.call_args.kwargs["data"]["order"])[0]
        assert order["customer_ref_id"] == "order-1"
        assert order["address"]["country"] == "IT"
        assert order["products"] == [{"quantity": 1, "ean": "3700546705526"}]

    def test_sends_unit_pref_which_createuser_did_not_need(self) -> None:
        # Required by this action, and it is what the hardware renders in — an empty object
        # gives the member a scale in the wrong units.
        import json

        post = MagicMock(return_value=_ok())

        _call(post)

        assert json.loads(post.call_args.kwargs["data"]["unit_pref"]) == {"weight": 1, "height": 6}

    def test_omits_optional_fields_that_were_not_given(self) -> None:
        post = MagicMock(return_value=_ok())

        _call(post)

        sent = post.call_args.kwargs["data"]
        for absent in ("firstname", "lastname", "phonenumber", "recovery_code", "testmode"):
            assert absent not in sent

    def test_testmode_places_the_order_without_shipping(self) -> None:
        # The only way to exercise this end to end without sending hardware to a real address,
        # and therefore the first thing anyone runs once the entitlement lands.
        post = MagicMock(return_value=_ok())

        _call(post, testmode=True)

        assert post.call_args.kwargs["data"]["testmode"] == "1"


class TestWhatItRefusesToSend:
    def test_rejects_a_shortname_the_hardware_cannot_render(self) -> None:
        # Withings' own /^[a-zA-Z0-9]{3}$/. Wrong, it is visible on the device screen.
        with pytest.raises(ValueError, match="shortname"):
            _call(MagicMock(), shortname="Francesco")

    def test_rejects_an_order_with_no_products(self) -> None:
        with pytest.raises(ValueError, match="at least 1 item"):
            _order(products=[])

    def test_rejects_a_product_identified_by_neither_ean_nor_partner_ref(self) -> None:
        # Withings needs exactly one, and answers "neither" and "both" with the same opaque
        # status, so the local check is worth more than the round trip.
        with pytest.raises(ValueError, match="exactly one"):
            DropshipProduct(quantity=1)

    def test_rejects_a_product_identified_by_both(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            DropshipProduct(quantity=1, ean="3700546705526", partner_ref="robin-bpm")

    def test_rejects_an_empty_unit_pref(self) -> None:
        # Not a formatting concern: this is what the scale displays to the member, and Withings
        # answers a bad one with a SUCCESSFUL order rather than an error.
        with pytest.raises(ValueError, match="unit_pref"):
            _call(MagicMock(), unit_pref={})

    def test_rejects_a_call_with_no_order_at_all(self) -> None:
        # A createuserorder with no order would create an account nothing ships to — worse than
        # a rejection, because the account is real.
        with pytest.raises(ValueError, match="at least one order"):
            _call(MagicMock(), orders=[])

    def test_makes_no_request_when_validation_fails(self) -> None:
        post = MagicMock()

        with pytest.raises(ValueError, match="shortname"):
            _call(post, shortname="")

        post.assert_not_called()


class TestTheResponse:
    def test_returns_the_code_and_the_orders_separately(self) -> None:
        # The split that matters: the code becomes a connection here, the orders go back to
        # robin-backend, and nothing about an order is stored in this fork.
        post = MagicMock(return_value=_ok())

        result = _call(post)

        assert result.code == "auth-code"
        assert result.external_id == "profile-1#order-1"
        assert [o.orderid for o in result.orders] == ["WO-1"]
        assert result.orders[0].status == "PENDING"

    def test_tolerates_an_order_shape_withings_extended(self) -> None:
        # Their vocabulary, and they add to it. Failing to parse a response would fail a
        # shipment that has already been placed, which is the one outcome worth avoiding.
        post = MagicMock(
            return_value=_ok(
                {
                    "user": {"code": "auth-code", "external_id": "profile-1#order-1"},
                    "orders": [{"orderid": "WO-1", "status": "NEW_STATUS", "tracking_url": "https://x"}],
                }
            )
        )

        result = _call(post)

        assert result.orders[0].orderid == "WO-1"

    def test_a_non_zero_status_raises(self) -> None:
        # 277 is the one to expect until the contract is in place: "unauthorized … not allowed
        # for this partner", an entitlement verdict rather than a bad request.
        post = MagicMock(return_value=_ok(status=277))

        with pytest.raises(WithingsDropshipmentError) as exc:
            _call(post)

        assert exc.value.withings_status == 277

    def test_returns_our_external_id_not_the_one_withings_echoed(self) -> None:
        # The column is ours ("Ours, not Withings'") and it is the join back to robin-backend's
        # order row, under #7's {profileId}#{orderRef} UNIQUE discipline. createuser makes this
        # same choice deliberately; this path storing the echo instead would put two writers with
        # opposite policies on one unique column.
        #
        # The fixture must echo a DIFFERENT value, or the assertion cannot tell the policies
        # apart — which is exactly how the original version of this test passed on the bug.
        post = MagicMock(
            return_value=_ok(
                {
                    "user": {"code": "auth-code", "external_id": "WITHINGS-NORMALISED-999"},
                    "orders": [{"orderid": "WO-1", "status": "PENDING"}],
                }
            )
        )

        result = _call(post)

        assert result.external_id == "profile-1#order-1"

    def test_a_success_that_acknowledged_no_orders_raises(self) -> None:
        # An account with nothing shipping to it — the state the request-side guard exists to
        # prevent, reached from the response side. Also how a key-name miss degrades: we send the
        # block under `order` and read it back under `orders`, and nobody has seen this response.
        post = MagicMock(
            return_value=_ok({"user": {"code": "auth-code", "external_id": "profile-1#order-1"}, "orders": []})
        )

        with pytest.raises(WithingsDropshipmentError, match="no orders"):
            _call(post)

    def test_an_order_arriving_under_the_singular_key_is_not_silently_dropped(self) -> None:
        # The concrete shape of that miss: a REAL placed order, read as none.
        post = MagicMock(
            return_value=_ok(
                {"user": {"code": "auth-code", "external_id": "profile-1#order-1"}, "order": [{"orderid": "WO-1"}]}
            )
        )

        with pytest.raises(WithingsDropshipmentError, match="no orders"):
            _call(post)

    def test_an_unreadable_order_raises_this_modules_error_not_a_pydantic_one(self) -> None:
        # Leniency absorbs unexpected KEYS; an unexpected TYPE still lands here, after the order
        # is placed. The caller must be able to tell it from a transport failure.
        post = MagicMock(
            return_value=_ok({"user": {"code": "auth-code", "external_id": "profile-1#order-1"}, "orders": ["WO-1"]})
        )

        with pytest.raises(WithingsDropshipmentError, match="unreadable order"):
            _call(post)

    def test_a_success_with_no_code_raises_rather_than_returning_half_a_result(self) -> None:
        # status 0 and no code is a contract change, and a dangerous one: the account and the
        # order may both exist upstream.
        post = MagicMock(return_value=_ok({"user": {}, "orders": []}))

        with pytest.raises(WithingsDropshipmentError, match="no code"):
            _call(post)

    def test_never_puts_the_response_body_in_the_error(self) -> None:
        # The request carried a signature AND a postal address; the response may echo either.
        import httpx

        response = MagicMock(status_code=400, text="invalid address: Via Roma 1, Milano")
        post = MagicMock(side_effect=httpx.HTTPStatusError("boom", request=MagicMock(), response=response))

        with pytest.raises(WithingsDropshipmentError) as exc:
            _call(post)

        assert "Via Roma" not in str(exc.value)
        assert "HTTP 400" in str(exc.value)
