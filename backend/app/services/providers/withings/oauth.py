"""Handle Withings OAuth token RPC envelopes and provider user identity."""

import logging
from datetime import UTC, datetime
from uuid import UUID

import httpx
from fastapi import HTTPException
from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR

from app.config import settings
from app.database import DbSession
from app.schemas.auth import AuthenticationMethod
from app.schemas.enums import ProviderName
from app.schemas.model_crud.credentials import (
    OAuthTokenResponse,
    ProviderCredentials,
    ProviderEndpoints,
)
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.services.providers.withings._client import WITHINGS_API_BASE_URL
from app.services.providers.withings.refresh_lock import single_flight_refresh
from app.services.providers.withings.request_budget import acquire_request_slot
from app.services.providers.withings.signature import sign_payload
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

# Token-request statuses caused by invalid request inputs rather than provider faults.
_TOKEN_CLIENT_ERROR_STATUSES = {247, 250, 283, 286, 293, 303, 304, 342}
# Withings' "Authentication failed" family. On refresh these also mean the grant
# is spent; on exchange they usually mean an expired authorization code.
_AUTHENTICATION_FAILED_STATUSES = {100, 101, 102, 200, 401}
_RATE_LIMIT_STATUS = 601

# Preserve the short-lived authorization code by bounding request-budget wait time.
_EXCHANGE_MAX_WAIT_SECONDS = 5


class WithingsTokenError(HTTPException):
    """Typed token failure preserving provider status and grant finality."""

    def __init__(
        self,
        *,
        task: str,
        withings_status: int | None = None,
        http_status: int | None = None,
        detail: str | None = None,
    ) -> None:
        self.withings_status = withings_status
        self.http_status = http_status
        authentication_failed = withings_status in _AUTHENTICATION_FAILED_STATUSES or http_status in {400, 401}
        self.invalid_grant = task == "refresh_access_token" and authentication_failed
        if withings_status == _RATE_LIMIT_STATUS or http_status == 429:
            status_code = 429
        elif authentication_failed:
            status_code = 401
        elif withings_status in _TOKEN_CLIENT_ERROR_STATUSES or (http_status is not None and http_status < 500):
            status_code = 400
        else:
            status_code = 500
        super().__init__(
            status_code=status_code,
            detail=detail or f"Withings token error (status={withings_status})",
        )


class WithingsOAuth(BaseOAuthTemplate):
    """Withings OAuth 2.0 implementation."""

    use_pkce: bool = False
    auth_method: AuthenticationMethod = AuthenticationMethod.BODY

    @property
    def endpoints(self) -> ProviderEndpoints:
        return ProviderEndpoints(
            authorize_url="https://account.withings.com/oauth2_user/authorize2",
            token_url="https://wbsapi.withings.net/v2/oauth2",
        )

    @property
    def credentials(self) -> ProviderCredentials:
        return ProviderCredentials(
            client_id=settings.withings_client_id or "",
            client_secret=(
                settings.withings_client_secret.get_secret_value() if settings.withings_client_secret else ""
            ),
            redirect_uri=settings.oauth_redirect_uri(ProviderName.WITHINGS),
            default_scope=settings.withings_default_scope,
        )

    def _exchange_token(self, code: str, code_verifier: str | None) -> OAuthTokenResponse:
        payload = {
            "action": "requesttoken",
            "grant_type": "authorization_code",
            "client_id": self.credentials.client_id,
            "client_secret": self.credentials.client_secret,
            "code": code,
            "redirect_uri": self.credentials.redirect_uri,
        }
        return self._request_token(payload, task="exchange_token", max_wait_seconds=_EXCHANGE_MAX_WAIT_SECONDS)

    def _already_rotated(self, db: DbSession, user_id: UUID, refresh_token: str) -> OAuthTokenResponse | None:
        """Return another worker's freshly rotated token, if there is one.

        Withings invalidates ``refresh_token`` the moment a rotation happens, so a stored
        refresh token that differs from the one we were handed is proof that somebody
        else has already refreshed. Reusing their result is not an optimisation - issuing
        our own requesttoken with the dead ``refresh_token`` would fail, and issuing it
        with the live one would orphan their rotation.
        """
        db.expire_all()  # drop the identity-map copy; another worker wrote this row
        connection = self.connection_repo.get_by_user_and_provider(db, user_id, self.provider_name)
        if not connection or not connection.access_token or not connection.refresh_token:
            return None
        if connection.refresh_token == refresh_token:
            return None  # nothing rotated while we waited

        expires_at = connection.token_expires_at
        expires_in = int((expires_at - datetime.now(UTC)).total_seconds()) if expires_at else 0
        if expires_in <= 0:
            return None  # rotated, but already stale - refresh properly

        log_structured(
            logger,
            "info",
            "Withings token already refreshed by another worker",
            provider=self.provider_name,
            task="refresh_access_token",
            user_id=str(user_id),
        )
        return OAuthTokenResponse(
            access_token=connection.access_token,
            token_type="Bearer",
            refresh_token=connection.refresh_token,
            expires_in=expires_in,
        )

    def refresh_access_token(self, db: DbSession, user_id: UUID, refresh_token: str) -> OAuthTokenResponse:
        # Serialised per user: Withings rotates the refresh token on every call, so two
        # concurrent refreshes leave the connection holding an already-invalidated token.
        # See refresh_lock.py for the interleaving this prevents.
        with single_flight_refresh(user_id):
            reused = self._already_rotated(db, user_id, refresh_token)
            if reused:
                return reused
            return self._do_refresh(db, user_id, refresh_token)

    def _do_refresh(self, db: DbSession, user_id: UUID, refresh_token: str) -> OAuthTokenResponse:
        payload = {
            "action": "requesttoken",
            "grant_type": "refresh_token",
            "client_id": self.credentials.client_id,
            "client_secret": self.credentials.client_secret,
            "refresh_token": refresh_token,
        }
        try:
            token_response = self._request_token(payload, task="refresh_access_token")
        except WithingsTokenError as exc:
            if exc.invalid_grant:
                self._revoke_connection(db, user_id, reason="refresh_failed")
            raise

        connection = self.connection_repo.get_by_user_and_provider(db, user_id, self.provider_name)
        if connection:
            # Withings rotates the refresh token on refresh; keep the old one if omitted.
            self.connection_repo.update_tokens(
                db,
                connection,
                token_response.access_token,
                token_response.refresh_token or refresh_token,
                token_response.expires_in,
            )
        log_structured(
            logger,
            "info",
            "Withings token refreshed",
            provider=self.provider_name,
            task="refresh_access_token",
            user_id=str(user_id),
        )
        return token_response

    def _authenticate_payload(self, payload: dict[str, str]) -> dict[str, str]:
        """Apply the configured token-auth scheme.

        In "signature" mode the client secret never leaves our process: it keys an HMAC
        over a freshly fetched nonce. In "secret" mode the payload is passed through with
        client_secret in the body, which is what the upstream branch did.
        """
        if settings.withings_auth_mode != "signature":
            return payload
        client_id = self.credentials.client_id
        client_secret = self.credentials.client_secret
        if not client_id or not client_secret:
            raise HTTPException(
                status_code=HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Withings credentials are not configured",
            )
        return sign_payload(payload, client_id, client_secret, api_base_url=WITHINGS_API_BASE_URL)

    def _request_token(
        self, payload: dict[str, str], *, task: str, max_wait_seconds: float | None = None
    ) -> OAuthTokenResponse:
        """POST a token request and unwrap the Withings ``{status, body}`` envelope."""
        payload = self._authenticate_payload(payload)
        if max_wait_seconds is not None:
            acquire_request_slot(max_wait_seconds=max_wait_seconds)
        else:
            acquire_request_slot()
        try:
            response = httpx.post(
                self.endpoints.token_url,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30.0,
            )
            response.raise_for_status()
            envelope = response.json()
        except httpx.HTTPStatusError as e:
            log_structured(
                logger,
                "error",
                f"Withings token HTTP error: {e.response.text}",
                provider=self.provider_name,
                task=task,
                status_code=e.response.status_code,
            )
            raise WithingsTokenError(
                task=task,
                http_status=e.response.status_code,
                detail=f"Withings token request failed: {e.response.text}",
            ) from e
        except Exception as e:
            log_structured(
                logger,
                "error",
                f"Withings token request failed: {e}",
                provider=self.provider_name,
                task=task,
            )
            raise HTTPException(
                status_code=HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Withings token request failed: {e}",
            ) from e

        status = envelope.get("status")
        if status != 0:
            log_structured(
                logger,
                "error",
                "Withings token envelope status non-zero",
                provider=self.provider_name,
                task=task,
                withings_status=status,
            )
            raise WithingsTokenError(task=task, withings_status=status)

        return OAuthTokenResponse.model_validate(envelope.get("body", {}))

    def _get_provider_user_info(self, token_response: OAuthTokenResponse, user_id: str) -> dict[str, str | None]:
        """Return the Withings ``userid`` from the token body — the key for inbound notifications."""
        extra = token_response.model_extra or {}
        userid = extra.get("userid")
        return {"user_id": str(userid) if userid is not None else None, "username": None}

    def deregister_user(self, access_token: str, provider_user_id: str | None = None) -> None:
        """Leave Notify teardown to the webhook subscription lifecycle."""
