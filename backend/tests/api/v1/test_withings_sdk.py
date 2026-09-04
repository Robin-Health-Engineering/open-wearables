"""Route-level tests for the Withings Mobile SDK endpoints.

The service layer is covered elsewhere; what is only true at the route is covered here — and
one of those things is load-bearing enough that the docstring says so in capitals.

``GET /providers/withings/sdk/session`` resolves the access token FIRST and reads the SDK
account SECOND, because resolving the token may refresh it and a refresh ROTATES the
csrf_token. Read the account first and the response carries the pre-rotation value: valid
looking, and rejected by Withings at WebView-open time, far from the call that caused it.
Nothing about that ordering is visible in a diff, which is exactly why it needs a test that
fails when someone tidies it.

The ordering test asserts the ORDER OF THE CALLS rather than the value in the response.
Mutating the account row would not discriminate: the route builds its response from the ORM
object at the end either way, and SQLAlchemy's identity map hands back the same instance, so a
reordered route would still read the rotated value and the test would pass while the invariant
was broken.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.config import settings
from app.models.user import User
from app.models.withings_sdk_account import WithingsSdkAccount
from app.services.providers.withings.sdk_users import WithingsSdkUserError
from tests.factories import UserConnectionFactory, UserFactory

_TOKEN = "app.api.routes.v1.withings_sdk._get_valid_token"
_PROVISION = "app.api.routes.v1.withings_sdk.provision_sdk_account"

_ACCOUNTS_URL = "/api/v1/providers/withings/sdk/accounts"
_SESSION_URL = "/api/v1/providers/withings/sdk/session"


@pytest.fixture
def withings_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provisioning answers 503 unless the deployment has partner credentials."""
    monkeypatch.setattr(settings, "withings_client_id", "test-client-id")
    monkeypatch.setattr(settings, "withings_client_secret", SecretStr("test-client-secret"))


def _valid_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "user_id": str(uuid4()),
        "external_id": "robin-user-1",
        "email": "member@example.com",
        "shortname": "FRA",
        "birthdate": 643248000,
        "gender": 0,
        "weight_kg": 75.4,
        "height_m": 1.78,
        "preflang": "it_IT",
        "timezone": "Europe/Rome",
        "mailingpref": 0,
    }
    payload.update(overrides)
    return payload


def _connected_member(db: Session, *, csrf_token: str | None, with_account: bool = True) -> User:
    """A member with a Withings connection, optionally SDK-provisioned."""
    user = UserFactory()
    connection = UserConnectionFactory(user=user, provider="withings", provider_user_id="withings-1")
    if with_account:
        account = WithingsSdkAccount(
            id=uuid4(),
            user_connection_id=connection.id,
            external_id=f"robin-{uuid4().hex[:8]}",
            csrf_token=csrf_token,
            updated_at=connection.updated_at,
        )
        db.add(account)
        db.commit()
    return user


class TestSessionRoute:
    def test_returns_both_halves_of_the_pair(
        self, client: TestClient, db: Session, api_key_header: dict[str, str]
    ) -> None:
        user = _connected_member(db, csrf_token="csrf-current")

        with patch(_TOKEN, return_value="access-current"):
            response = client.get(_SESSION_URL, params={"user_id": str(user.id)}, headers=api_key_header)

        assert response.status_code == 200
        assert response.json() == {"access_token": "access-current", "csrf_token": "csrf-current"}

    def test_resolves_the_token_before_reading_the_account(
        self, client: TestClient, db: Session, api_key_header: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # THE ordering invariant — see the module docstring for why this asserts call order
        # rather than the returned value.
        user = _connected_member(db, csrf_token="csrf-current")
        calls: list[str] = []

        original_query = db.query

        def spy_query(*args: Any, **kwargs: Any) -> Any:
            if args and args[0] is WithingsSdkAccount:
                calls.append("read_account")
            return original_query(*args, **kwargs)

        monkeypatch.setattr(db, "query", spy_query)

        def fake_token(*_args: Any, **_kwargs: Any) -> str:
            calls.append("resolve_token")
            return "access-current"

        with patch(_TOKEN, side_effect=fake_token):
            response = client.get(_SESSION_URL, params={"user_id": str(user.id)}, headers=api_key_header)

        assert response.status_code == 200
        assert calls == ["resolve_token", "read_account"], (
            "the token must be resolved first: resolving it may refresh, and a refresh rotates csrf_token"
        )

    def test_404_when_the_member_was_never_sdk_provisioned(
        self, client: TestClient, db: Session, api_key_header: dict[str, str]
    ) -> None:
        # Connected via phase-1 consumer OAuth. A distinct condition from "not connected".
        user = _connected_member(db, csrf_token=None, with_account=False)

        with patch(_TOKEN, return_value="access-current"):
            response = client.get(_SESSION_URL, params={"user_id": str(user.id)}, headers=api_key_header)

        assert response.status_code == 404
        assert "no Withings SDK account" in response.json()["detail"]

    def test_409_when_the_account_has_no_csrf_token(
        self, client: TestClient, db: Session, api_key_header: dict[str, str]
    ) -> None:
        user = _connected_member(db, csrf_token=None)

        with patch(_TOKEN, return_value="access-current"):
            response = client.get(_SESSION_URL, params={"user_id": str(user.id)}, headers=api_key_header)

        assert response.status_code == 409
        assert "re-provision" in response.json()["detail"]

    def test_requires_authentication(self, client: TestClient, db: Session) -> None:
        user = _connected_member(db, csrf_token="csrf-current")

        response = client.get(_SESSION_URL, params={"user_id": str(user.id)})

        assert response.status_code == 401


class TestProvisioningRoute:
    def test_returns_what_provisioning_stored(
        self, client: TestClient, db: Session, api_key_header: dict[str, str], withings_configured: None
    ) -> None:
        account = WithingsSdkAccount(
            id=uuid4(), user_connection_id=uuid4(), external_id="robin-user-1", csrf_token="csrf-new"
        )

        with patch(_PROVISION, return_value=account):
            response = client.post(_ACCOUNTS_URL, json=_valid_payload(), headers=api_key_header)

        assert response.status_code == 201
        assert response.json() == {"external_id": "robin-user-1", "csrf_token": "csrf-new"}

    def test_503_when_the_deployment_has_no_withings_credentials(
        self, client: TestClient, api_key_header: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An operator condition, not a bad request — and the default state of a deployment
        # that has not been given partner credentials.
        monkeypatch.setattr(settings, "withings_client_id", None)

        response = client.post(_ACCOUNTS_URL, json=_valid_payload(), headers=api_key_header)

        assert response.status_code == 503

    def test_400_when_provisioning_rejects_the_input_locally(
        self, client: TestClient, api_key_header: dict[str, str], withings_configured: None
    ) -> None:
        with patch(_PROVISION, side_effect=ValueError("shortname must be exactly 3 alphanumerics")):
            response = client.post(_ACCOUNTS_URL, json=_valid_payload(), headers=api_key_header)

        assert response.status_code == 400

    def test_502_without_echoing_the_upstream_detail(
        self, client: TestClient, api_key_header: dict[str, str], withings_configured: None
    ) -> None:
        # The upstream body answers a SIGNED request and may repeat our parameters back, which
        # carry the member's email, birth date and weight.
        error = WithingsSdkUserError(withings_status=503, detail="createuser failed for member@example.com")

        with patch(_PROVISION, side_effect=error):
            response = client.post(_ACCOUNTS_URL, json=_valid_payload(), headers=api_key_header)

        assert response.status_code == 502
        assert "member@example.com" not in response.json()["detail"]
        assert "status=503" in response.json()["detail"]

    def test_502_when_withings_returned_no_csrf_token(
        self, client: TestClient, api_key_header: dict[str, str], withings_configured: None
    ) -> None:
        # The column is nullable, so the invariant is asserted rather than assumed: handing
        # back a null the client cannot use would fail later and further away.
        account = WithingsSdkAccount(
            id=uuid4(), user_connection_id=uuid4(), external_id="robin-user-1", csrf_token=None
        )

        with patch(_PROVISION, return_value=account):
            response = client.post(_ACCOUNTS_URL, json=_valid_payload(), headers=api_key_header)

        assert response.status_code == 502

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("email", "not-an-email"),
            ("shortname", "TOOLONG"),
            ("shortname", "AB"),
            ("gender", 2),
            ("mailingpref", 7),
            ("weight_kg", 0),
            ("height_m", -1),
        ],
    )
    def test_400_on_input_withings_would_reject_opaquely(
        self,
        client: TestClient,
        api_key_header: dict[str, str],
        withings_configured: None,
        field: str,
        value: Any,
    ) -> None:
        # Every one of these comes back from Withings as a non-zero status with no field name
        # attached, surfacing as a 502 the caller can do nothing with. Caught here instead.
        #
        # 400 and not FastAPI's default 422: this app registers its own RequestValidationError
        # handler (main.py:91, utils/exceptions.py:88-95) which remaps it. Asserting 422 here
        # would be asserting the framework's contract instead of this deployment's, and the
        # robin-backend proxy reads the status.
        with patch(_PROVISION) as provision:
            response = client.post(_ACCOUNTS_URL, json=_valid_payload(**{field: value}), headers=api_key_header)

        assert response.status_code == 400
        # A rejected request must not reach Withings.
        provision.assert_not_called()
