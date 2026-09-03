"""Tests for partner-hosted User Creation (Withings Mobile SDK, phase 2).

The failure modes here are all quiet ones. Withings answers HTTP 200 with a non-zero
``status`` in the body, so `raise_for_status` never fires; a bad `shortname` comes back as an
opaque status rather than a validation message; and `measures` sent as a float is accepted and
then silently misread. Each gets a test, because none of them would surface as an exception in
integration.
"""

from __future__ import annotations

import json
from typing import Any, NoReturn

import httpx
import pytest

from app.services.providers.withings import sdk_users
from app.services.providers.withings.sdk_users import (
    SdkUser,
    WithingsSdkUserError,
    create_sdk_user,
)

VALID: dict[str, Any] = {
    "client_id": "cid",
    "client_secret": "csecret",
    "external_id": "robin-user-1",
    "email": "member@example.com",
    "shortname": "ROB",
    "birthdate": 615168000,
    "gender": 0,
    "weight_kg": 75.4,
    "height_m": 1.82,
    "preflang": "it_IT",
    "timezone": "Europe/Rome",
    "mailingpref": 0,
}


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sdk_users, "acquire_request_slot", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _stub_signing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Signing has its own suite; here it must not reach the network for a nonce."""

    def fake_sign_payload(payload: dict[str, str], client_id: str, client_secret: str, **_: Any) -> dict[str, str]:
        return {**payload, "client_id": client_id, "nonce": "n0nce", "signature": "sig"}

    monkeypatch.setattr(sdk_users, "sign_payload", fake_sign_payload)


def _post(
    monkeypatch: pytest.MonkeyPatch,
    envelope: dict[str, Any],
    captured: dict[str, Any] | None = None,
) -> None:
    def fake_post(url: str, data: dict[str, str], headers: dict[str, str], timeout: float) -> httpx.Response:
        if captured is not None:
            captured["url"] = url
            captured["data"] = data
            captured["headers"] = headers
        return httpx.Response(200, json=envelope, request=httpx.Request("POST", url))

    monkeypatch.setattr(sdk_users.httpx, "post", fake_post)


OK_ENVELOPE = {"status": 0, "body": {"user": {"code": "abc123", "external_id": "robin-user-1"}}}


class TestCreateSdkUserSuccess:
    def test_returns_the_code_and_external_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _post(monkeypatch, OK_ENVELOPE)
        assert create_sdk_user(**VALID) == SdkUser(code="abc123", external_id="robin-user-1")

    def test_posts_form_encoded_to_the_sdk_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}
        _post(monkeypatch, OK_ENVELOPE, captured)
        create_sdk_user(**VALID)
        assert captured["url"].endswith("/v2/sdk")
        assert captured["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
        assert captured["data"]["action"] == "createuser"

    def test_carries_a_nonce_and_signature_and_never_the_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}
        _post(monkeypatch, OK_ENVELOPE, captured)
        create_sdk_user(**VALID)
        assert captured["data"]["nonce"]
        assert captured["data"]["signature"]
        assert "client_secret" not in captured["data"]

    def test_measures_are_scaled_integers_not_floats(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # value * 10^unit. 75.4 kg is {value: 75400, unit: -3}, never {value: 75.4} — which
        # Withings accepts and then misreads. This is the same scaling trap as the data path.
        captured: dict[str, Any] = {}
        _post(monkeypatch, OK_ENVELOPE, captured)
        create_sdk_user(**VALID)
        measures = json.loads(captured["data"]["measures"])
        weight = next(m for m in measures if m["type"] == 1)
        height = next(m for m in measures if m["type"] == 4)
        assert weight == {"value": 75400, "unit": -3, "type": 1}
        assert height == {"value": 1820, "unit": -3, "type": 4}
        assert all(isinstance(m["value"], int) for m in measures)

    def test_omits_optional_fields_rather_than_sending_them_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}
        _post(monkeypatch, OK_ENVELOPE, captured)
        create_sdk_user(**VALID)
        for optional in ("firstname", "lastname", "phonenumber", "recovery_code"):
            assert optional not in captured["data"]

    def test_prefers_our_external_id_over_the_echo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A differing echo is a mismatch we must not store silently — the member is keyed off
        # the value WE sent.
        _post(monkeypatch, {"status": 0, "body": {"user": {"code": "c", "external_id": "somebody-else"}}})
        assert create_sdk_user(**VALID).external_id == "robin-user-1"


class TestCreateSdkUserFailure:
    def test_non_zero_status_raises_even_though_http_is_200(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The whole point: Withings signals failure inside a 200 body.
        _post(monkeypatch, {"status": 503, "error": "Invalid params"})
        with pytest.raises(WithingsSdkUserError) as e:
            create_sdk_user(**VALID)
        assert e.value.withings_status == 503

    def test_status_zero_with_no_code_is_a_contract_change_not_a_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _post(monkeypatch, {"status": 0, "body": {"user": {"external_id": "robin-user-1"}}})
        with pytest.raises(WithingsSdkUserError):
            create_sdk_user(**VALID)

    def test_http_error_is_wrapped_and_the_body_is_not_leaked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_post(url: str, **_: Any) -> NoReturn:
            request = httpx.Request("POST", url)
            raise httpx.HTTPStatusError(
                "boom",
                request=request,
                response=httpx.Response(500, text="signature=deadbeef", request=request),
            )

        monkeypatch.setattr(sdk_users.httpx, "post", fake_post)
        with pytest.raises(WithingsSdkUserError) as e:
            create_sdk_user(**VALID)
        assert "deadbeef" not in str(e.value)

    @pytest.mark.parametrize("bad", ["RO", "ROBI", "RO!", "", "ro b"])
    def test_rejects_a_shortname_the_device_screen_cannot_render(
        self, monkeypatch: pytest.MonkeyPatch, bad: str
    ) -> None:
        # Caught locally: the API answers an opaque status, and this string is shown ON the
        # hardware, so a wrong one is visible to the member.
        _post(monkeypatch, OK_ENVELOPE)
        with pytest.raises(ValueError, match="shortname"):
            create_sdk_user(**{**VALID, "shortname": bad})

    def test_accepts_a_valid_three_character_alphanumeric_shortname(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Guards the guard: a regex that rejected everything would pass every test above.
        _post(monkeypatch, OK_ENVELOPE)
        assert create_sdk_user(**{**VALID, "shortname": "a1B"}).code == "abc123"

    @pytest.mark.parametrize("bad_gender", [2, -1, 99])
    def test_rejects_an_out_of_range_gender(self, monkeypatch: pytest.MonkeyPatch, bad_gender: int) -> None:
        _post(monkeypatch, OK_ENVELOPE)
        with pytest.raises(ValueError, match="gender"):
            create_sdk_user(**{**VALID, "gender": bad_gender})

    @pytest.mark.parametrize("bad_pref", [2, -1])
    def test_rejects_an_out_of_range_mailingpref(self, monkeypatch: pytest.MonkeyPatch, bad_pref: int) -> None:
        _post(monkeypatch, OK_ENVELOPE)
        with pytest.raises(ValueError, match="mailingpref"):
            create_sdk_user(**{**VALID, "mailingpref": bad_pref})
