"""Which HTTP status the recovery route gives each failure.

Its own file, and no database: the route function is called directly with the service stubbed,
because what is under test is the MAPPING and nothing else. Going through the app would need a
session, a member and a connection to reach the same four lines.

The mapping was wrong in the first version of this route, in a way that reads as correct: it
branched on ``withings_status is None`` to mean "not found". Ten of the raise sites behind it
leave the status unset — an HTTP error or timeout talking to Withings, a response with no code,
a failed token exchange, a store that refuses — and only two of them mean "no such account". So
a Withings outage answered 404 "this member has no provisioned Withings account": a wrong
diagnosis rather than a wrong number, and one that hides an upstream failure from whoever is
watching the 5xx rate.
"""

from __future__ import annotations

from typing import Any, NoReturn
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.routes.v1 import withings_sdk as route_mod
from app.api.routes.v1.withings_sdk import SdkAccountRecoveryRequest, recover_withings_sdk_account
from app.services.providers.withings.sdk_users import WithingsSdkUserError


class _FakeSecret:
    @staticmethod
    def get_secret_value() -> str:
        return "csecret"


class _FakeSettings:
    """Stands in for the whole settings object, rather than patching fields on it.

    `Settings` is a pydantic model: `oauth_redirect_uri` is a METHOD and not a field, so
    `monkeypatch.setattr` on the real instance is refused outright. Swapping the name the module
    looked up is both simpler and honest about what these tests need — three values and nothing
    else from configuration.
    """

    withings_client_id = "cid"
    withings_client_secret = _FakeSecret()

    @staticmethod
    def oauth_redirect_uri(_provider: Any) -> str:
        return "https://example.test/cb"


@pytest.fixture(autouse=True)
def _configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Credentials present, so the 503 guard never fires and each test reaches the mapping."""
    monkeypatch.setattr(route_mod, "settings", _FakeSettings())


def _raising(error: Exception) -> Any:
    def _fake(*_a: Any, **_k: Any) -> NoReturn:
        raise error

    return _fake


def _call() -> Any:
    return recover_withings_sdk_account(
        payload=SdkAccountRecoveryRequest(user_id=uuid4()),
        db=None,  # type: ignore[arg-type]
        _caller=None,  # type: ignore[arg-type]
    )


def _status_for(monkeypatch: pytest.MonkeyPatch, error: Exception) -> int:
    monkeypatch.setattr(route_mod, "recover_sdk_account", _raising(error))
    with pytest.raises(HTTPException) as e:
        _call()
    return e.value.status_code


class TestRecoveryStatusMapping:
    def test_no_provisioned_account_is_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        err = WithingsSdkUserError(detail="this member has no provisioned Withings account to recover", not_found=True)
        assert _status_for(monkeypatch, err) == 404

    def test_withings_refusal_is_502(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _status_for(monkeypatch, WithingsSdkUserError(withings_status=601)) == 502

    def test_upstream_http_error_is_502_not_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The regression this file exists for. No `withings_status`, and emphatically not a
        # missing account: Withings answered 500.
        err = WithingsSdkUserError(detail="Withings recoverauthorizationcode failed (HTTP 500)")
        assert _status_for(monkeypatch, err) == 502

    def test_network_failure_is_502_not_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        err = WithingsSdkUserError(detail="Withings recoverauthorizationcode request failed")
        assert _status_for(monkeypatch, err) == 502

    def test_missing_code_is_502_not_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        err = WithingsSdkUserError(detail="Withings recoverauthorizationcode returned no code")
        assert _status_for(monkeypatch, err) == 502

    def test_store_refusal_is_409(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `_store_provisioned_account` refusing — the account is the member's own, or held under a
        # different external_id. The caller's state, like the provisioning route's 409.
        err = WithingsSdkUserError(detail="already provisioned under a different external_id", already_exists=True)
        assert _status_for(monkeypatch, err) == 409

    def test_not_found_wins_over_already_exists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Not reachable today; pinned so the branch order is a decision rather than an accident.
        err = WithingsSdkUserError(detail="both", not_found=True, already_exists=True)
        assert _status_for(monkeypatch, err) == 404

    def test_the_404_carries_the_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            route_mod,
            "recover_sdk_account",
            _raising(WithingsSdkUserError(detail="no provisioned account", not_found=True)),
        )
        with pytest.raises(HTTPException) as e:
            _call()
        assert e.value.detail == "no provisioned account"


class TestRecoveryUnconfigured:
    def test_missing_credentials_is_503(self, monkeypatch: pytest.MonkeyPatch) -> None:
        unconfigured = _FakeSettings()
        unconfigured.withings_client_id = ""  # type: ignore[misc]
        monkeypatch.setattr(route_mod, "settings", unconfigured)
        with pytest.raises(HTTPException) as e:
            _call()
        assert e.value.status_code == 503
