from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import User
from app.repositories.user_connection_repository import UserConnectionRepository
from app.repositories.user_repository import UserRepository
from app.schemas.auth import AuthenticationMethod, ConnectionStatus
from app.schemas.model_crud.credentials import OAuthTokenResponse
from app.services.providers.withings.oauth import WithingsOAuth, WithingsTokenError
from tests.factories import UserConnectionFactory, UserFactory


@pytest.fixture
def withings_oauth() -> WithingsOAuth:
    return WithingsOAuth(
        user_repo=UserRepository(User),
        connection_repo=UserConnectionRepository(),
        provider_name="withings",
        api_base_url="https://wbsapi.withings.net",
    )


def test_endpoints(withings_oauth: WithingsOAuth) -> None:
    e = withings_oauth.endpoints
    assert e.authorize_url == "https://account.withings.com/oauth2_user/authorize2"
    assert e.token_url == "https://wbsapi.withings.net/v2/oauth2"


def test_auth_method_is_body_and_no_pkce(withings_oauth: WithingsOAuth) -> None:
    assert withings_oauth.auth_method == AuthenticationMethod.BODY
    assert withings_oauth.use_pkce is False


def test_authorization_url_is_standard(withings_oauth: WithingsOAuth) -> None:
    url, state = withings_oauth.get_authorization_url(uuid4())
    assert url.startswith("https://account.withings.com/oauth2_user/authorize2?")
    assert "response_type=code" in url
    assert f"state={state}" in url


@patch("httpx.post")
def test_exchange_token_unwraps_envelope(mock_post: MagicMock, withings_oauth: WithingsOAuth) -> None:
    mock_post.return_value = MagicMock(
        status_code=200,
        raise_for_status=MagicMock(return_value=None),
        json=MagicMock(
            return_value={
                "status": 0,
                "body": {
                    "userid": "999",
                    "access_token": "at",
                    "refresh_token": "rt",
                    "scope": "user.info",
                    "expires_in": 10800,
                    "token_type": "Bearer",
                },
            }
        ),
    )
    resp = withings_oauth._exchange_token("the_code", None)
    assert isinstance(resp, OAuthTokenResponse)
    assert resp.access_token == "at"
    assert resp.refresh_token == "rt"
    assert resp.expires_in == 10800
    # action=requesttoken + grant_type sent in the BODY
    sent = mock_post.call_args.kwargs["data"]
    assert sent["action"] == "requesttoken"
    assert sent["grant_type"] == "authorization_code"
    assert sent["code"] == "the_code"


@patch("httpx.post")
def test_exchange_token_raises_on_nonzero_status(mock_post: MagicMock, withings_oauth: WithingsOAuth) -> None:
    # Invalid request input maps to HTTP 400.
    mock_post.return_value = MagicMock(
        status_code=200,
        raise_for_status=MagicMock(return_value=None),
        json=MagicMock(return_value={"status": 342, "body": {}}),
    )
    with pytest.raises(HTTPException) as exc_info:
        withings_oauth._exchange_token("bad", None)
    assert exc_info.value.status_code == 400

    # Unknown provider status maps to HTTP 500.
    mock_post.return_value = MagicMock(
        status_code=200,
        raise_for_status=MagicMock(return_value=None),
        json=MagicMock(return_value={"status": 123, "body": {}}),
    )
    with pytest.raises(HTTPException) as exc_info:
        withings_oauth._exchange_token("bad", None)
    assert exc_info.value.status_code == 500


@pytest.mark.parametrize("withings_status", [100, 101, 102, 200, 401])
@patch("httpx.post")
def test_exchange_authentication_failure_is_unauthorized_without_invalidating_refresh_grant(
    mock_post: MagicMock, withings_status: int, withings_oauth: WithingsOAuth
) -> None:
    mock_post.return_value = MagicMock(
        status_code=200,
        raise_for_status=MagicMock(return_value=None),
        json=MagicMock(return_value={"status": withings_status, "body": {}}),
    )

    with pytest.raises(WithingsTokenError) as exc_info:
        withings_oauth._exchange_token("expired", None)

    assert exc_info.value.status_code == 401
    assert not exc_info.value.invalid_grant


@patch("httpx.post")
def test_exchange_token_rate_limit_is_retryable(mock_post: MagicMock, withings_oauth: WithingsOAuth) -> None:
    mock_post.return_value = MagicMock(
        status_code=200,
        raise_for_status=MagicMock(return_value=None),
        json=MagicMock(return_value={"status": 601, "body": {}}),
    )

    with pytest.raises(WithingsTokenError) as exc_info:
        withings_oauth._exchange_token("code", None)

    assert exc_info.value.status_code == 429
    assert not exc_info.value.invalid_grant


@patch("app.services.providers.templates.base_oauth.on_connection_revoked")
@patch("httpx.post")
def test_refresh_invalid_grant_revokes_connection(
    mock_post: MagicMock, mock_revoked: MagicMock, withings_oauth: WithingsOAuth, db: Session
) -> None:
    user = UserFactory()
    connection = UserConnectionFactory(user=user, provider="withings", refresh_token="dead")
    mock_post.return_value = MagicMock(
        status_code=200,
        raise_for_status=MagicMock(return_value=None),
        json=MagicMock(return_value={"status": 200, "body": {}}),
    )

    with pytest.raises(WithingsTokenError) as exc_info:
        withings_oauth.refresh_access_token(db, user.id, "dead")

    db.refresh(connection)
    assert exc_info.value.status_code == 401
    assert exc_info.value.invalid_grant
    assert connection.status == ConnectionStatus.REVOKED
    mock_revoked.assert_called_once()


@patch("app.services.providers.templates.base_oauth.on_connection_revoked")
@patch("httpx.post")
def test_refresh_status_343_is_non_terminal(
    mock_post: MagicMock, mock_revoked: MagicMock, withings_oauth: WithingsOAuth, db: Session
) -> None:
    user = UserFactory()
    connection = UserConnectionFactory(user=user, provider="withings", refresh_token="still-valid")
    mock_post.return_value = MagicMock(
        status_code=200,
        raise_for_status=MagicMock(return_value=None),
        json=MagicMock(return_value={"status": 343, "body": {}}),
    )

    with pytest.raises(WithingsTokenError) as exc_info:
        withings_oauth.refresh_access_token(db, user.id, "still-valid")

    db.refresh(connection)
    assert exc_info.value.status_code == 500
    assert not exc_info.value.invalid_grant
    assert connection.status == ConnectionStatus.ACTIVE
    mock_revoked.assert_not_called()


def test_user_info_reads_userid_from_token_body(withings_oauth: WithingsOAuth) -> None:
    token = OAuthTokenResponse(
        access_token="at",
        refresh_token="rt",
        expires_in=10800,
        token_type="Bearer",
        userid="12345",
    )
    info = withings_oauth._get_provider_user_info(token, "internal-user")
    assert info["user_id"] == "12345"


@patch("httpx.post")
def test_refresh_persists_rotated_token(mock_post: MagicMock, withings_oauth: WithingsOAuth, db: Session) -> None:
    user = UserFactory()
    conn = UserConnectionFactory(user=user, provider="withings", refresh_token="old_rt")
    mock_post.return_value = MagicMock(
        status_code=200,
        raise_for_status=MagicMock(return_value=None),
        json=MagicMock(
            return_value={
                "status": 0,
                "body": {
                    "userid": "999",
                    "access_token": "new_at",
                    "refresh_token": "new_rt",
                    "expires_in": 10800,
                    "token_type": "Bearer",
                },
            }
        ),
    )
    resp = withings_oauth.refresh_access_token(db, user.id, "old_rt")
    assert resp.access_token == "new_at"
    assert resp.refresh_token == "new_rt"
    sent = mock_post.call_args.kwargs["data"]
    assert sent["action"] == "requesttoken"
    assert sent["grant_type"] == "refresh_token"

    # Verify the rotated tokens were persisted to the DB
    db.refresh(conn)
    assert conn.refresh_token == "new_rt"
    assert conn.access_token == "new_at"


def test_user_info_returns_none_when_userid_absent(withings_oauth: WithingsOAuth) -> None:
    token = OAuthTokenResponse(
        access_token="at",
        refresh_token="rt",
        expires_in=10800,
        token_type="Bearer",
    )
    info = withings_oauth._get_provider_user_info(token, "internal")
    assert info["user_id"] is None
