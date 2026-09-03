from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import User
from app.repositories.user_connection_repository import UserConnectionRepository
from app.repositories.user_repository import UserRepository
from app.schemas.auth import AuthenticationMethod, ConnectionStatus
from app.schemas.model_crud.credentials import OAuthTokenResponse
from app.services.providers.withings.oauth import WithingsOAuth, WithingsTokenError, redact_body
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


@patch("app.services.providers.withings.oauth.acquire_request_slot")
@patch("httpx.post")
def test_exchange_token_raises_on_nonzero_status(
    mock_post: MagicMock, mock_acquire: MagicMock, withings_oauth: WithingsOAuth
) -> None:
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
@patch("app.services.providers.withings.oauth.acquire_request_slot")
@patch("httpx.post")
def test_exchange_authentication_failure_is_unauthorized_without_invalidating_refresh_grant(
    mock_post: MagicMock, mock_acquire: MagicMock, withings_status: int, withings_oauth: WithingsOAuth
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


@patch("app.services.providers.withings.oauth.acquire_request_slot")
@patch("httpx.post")
def test_exchange_token_bounds_its_budget_wait_by_the_authorization_code_lifetime(
    mock_post: MagicMock, mock_acquire: MagicMock, withings_oauth: WithingsOAuth
) -> None:
    mock_post.return_value = MagicMock(
        status_code=200,
        raise_for_status=MagicMock(return_value=None),
        json=MagicMock(
            return_value={
                "status": 0,
                "body": {
                    "access_token": "at",
                    "refresh_token": "rt",
                    "expires_in": 10800,
                    "token_type": "Bearer",
                },
            }
        ),
    )
    withings_oauth._exchange_token("the_code", None)
    mock_acquire.assert_called_once_with(max_wait_seconds=5)


@patch("app.services.providers.withings.oauth.acquire_request_slot")
@patch("httpx.post")
def test_refresh_access_token_uses_the_default_budget_wait(
    mock_post: MagicMock, mock_acquire: MagicMock, withings_oauth: WithingsOAuth, db: Session
) -> None:
    mock_post.return_value = MagicMock(
        status_code=200,
        raise_for_status=MagicMock(return_value=None),
        json=MagicMock(
            return_value={
                "status": 0,
                "body": {
                    "access_token": "at",
                    "refresh_token": "rt2",
                    "expires_in": 10800,
                    "token_type": "Bearer",
                },
            }
        ),
    )
    withings_oauth.refresh_access_token(db, uuid4(), "old-refresh-token")
    mock_acquire.assert_called_once_with()


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


@patch("httpx.post")
def test_deregister_user_does_not_duplicate_notify_teardown(
    mock_post: MagicMock, withings_oauth: WithingsOAuth
) -> None:
    withings_oauth.deregister_user("the_token", provider_user_id="withings-user")
    mock_post.assert_not_called()


@patch("httpx.post")
def test_upstream_error_body_never_reaches_the_api_caller(mock_post: MagicMock, withings_oauth: WithingsOAuth) -> None:
    """WithingsTokenError is an HTTPException, so `detail` is serialised to OUR caller.

    The body it would carry is the response to a requesttoken whose payload held client_secret
    and refresh_token, so it must be logged and not returned.
    """
    secret_body = "error: invalid client_secret=abc123 for refresh_token=rt_xyz"
    response = MagicMock(status_code=400, text=secret_body)
    mock_post.return_value = MagicMock(
        raise_for_status=MagicMock(side_effect=httpx.HTTPStatusError("400", request=MagicMock(), response=response))
    )

    with pytest.raises(HTTPException) as exc:
        withings_oauth._exchange_token("the_code", None)

    assert "abc123" not in str(exc.value.detail)
    assert "rt_xyz" not in str(exc.value.detail)
    assert "400" in str(exc.value.detail)


@patch("httpx.post")
def test_generic_exception_branch_also_leaks_nothing(mock_post: MagicMock, withings_oauth: WithingsOAuth) -> None:
    """The second leak path, which nothing pinned.

    A non-JSON 200 raises inside res.json(), and a SyntaxError's str() quotes a fragment of
    the body — the more likely of the two to regress, because that message is produced by the
    parser rather than written by us.
    """
    mock_post.return_value = MagicMock(
        raise_for_status=MagicMock(),
        json=MagicMock(side_effect=ValueError("Expecting value: client_secret=abc123 rt_xyz")),
    )

    with pytest.raises(HTTPException) as exc:
        withings_oauth._exchange_token("the_code", None)

    assert "abc123" not in str(exc.value.detail)
    assert "rt_xyz" not in str(exc.value.detail)


@pytest.mark.parametrize(
    "body",
    [
        # form encoding
        "error: invalid client_secret=abc123 for refresh_token=rt_xyz",
        "client_secret=abc123&refresh_token=rt_xyz&grant_type=refresh_token",
        # JSON — the form this API actually replies in, and the one the first version of
        # this redaction missed entirely because the key's closing quote broke the match.
        '{"error":"invalid_client","client_secret":"abc123"}',
        '{ "client_secret" : "abc123", "refresh_token" : "rt_xyz" }',
        '{"status":401,"error":"invalid_grant","refresh_token":"rt_xyz"}',
        '{"body":{"access_token":"at_live_9f2","expires_in":10800}}',
        # a dict repr, which is what str(e) can carry
        "{'client_secret': 'abc123'}",
    ],
)
def test_redact_body_masks_credentials_in_every_wire_form(body: str) -> None:
    """The log half of the contract, across the shapes this body actually arrives in.

    The first version of this test pinned only the form-encoded case, which is the one the
    regex happened to handle — so it passed while JSON leaked in the clear.
    """
    out = redact_body(body)
    for secret in ("abc123", "rt_xyz", "at_live_9f2"):
        assert secret not in out, f"{secret!r} leaked from {body!r} -> {out!r}"


@pytest.mark.parametrize(
    "body",
    [
        "status_code=401 error_code: 503",
        '{"status_code":401,"error_code":"503"}',
    ],
)
def test_redact_body_keeps_diagnostics(body: str) -> None:
    """The mask must not eat the values the body is logged FOR.

    Without a word boundary the bare `code` alternative also matches inside `status_code`
    and `error_code`, so redaction destroyed exactly the two numbers a reader needs.
    """
    out = redact_body(body)
    assert "401" in out
    assert "503" in out


@pytest.mark.parametrize("body", ["code=auth_code_9f2", '{"code": "auth_code_9f2"}'])
def test_redact_body_still_masks_a_standalone_code(body: str) -> None:
    # The boundary must not cost us the real thing: an OAuth authorization code.
    assert "auth_code_9f2" not in redact_body(body)


def test_redact_body_bounds_length() -> None:
    # An HTML error page is not a useful log line either.
    assert len(redact_body("x" * 5000)) < 600


def test_redact_body_redacts_before_truncating() -> None:
    # Truncating first could cut a secret in half and leave the prefix in the log.
    padded = "y" * 480 + ' {"client_secret":"abc123"}'
    assert "abc123" not in redact_body(padded)
