"""Handle Withings OAuth token RPC envelopes and provider user identity."""

import logging
import re
from datetime import datetime, timezone
from uuid import UUID

import httpx
from fastapi import HTTPException
from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR

from app.config import settings
from app.database import DbSession
from app.models.withings_sdk_account import WithingsSdkAccount
from app.schemas.auth import AuthenticationMethod
from app.schemas.enums import ProviderName
from app.schemas.model_crud.credentials import (
    OAuthTokenResponse,
    ProviderCredentials,
    ProviderEndpoints,
)
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.services.providers.withings.request_budget import acquire_request_slot
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

# A token-endpoint response body is the reply to a request whose payload carried
# client_secret and refresh_token, and providers do echo submitted values back in error text.
# The caller-facing leak is closed by never putting the body in `detail`; this is the other
# half — the log. Bounded, because an HTML error page is not a useful log line either.
# Matches a credential-shaped key/value in every form this body arrives in: form encoding
# (client_secret=x), JSON ("client_secret": "x") — which is what this API actually returns —
# and a Python dict repr ('client_secret': 'x'), which is what str(e) can carry. The earlier
# version required the separator to follow the key name immediately, so any quoted key missed
# and JSON passed through in the clear.
#
# The lookbehind matters in the other direction: without it the bare `code` alternative also
# matches inside `status_code=401` and `error_code: 503` — the two values an error body is
# logged FOR — so the mask ate the diagnostics it exists to preserve.
_SECRET_IN_BODY = re.compile(
    r"""(?<![\w-])(["']?(?:client_secret|refresh_token|access_token|csrf_token|code)["']?\s*[=:]\s*)(["']?)([^\s,&"'}\]]+)""",
    re.IGNORECASE,
)
_MAX_LOGGED_BODY = 500


def redact_body(text: str) -> str:
    """Mask credential-shaped values in an upstream body before it reaches the log."""
    redacted = _SECRET_IN_BODY.sub(r"\1\2<redacted>", text or "")
    if len(redacted) > _MAX_LOGGED_BODY:
        return redacted[:_MAX_LOGGED_BODY] + "…[truncated]"
    return redacted


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

    def refresh_access_token(self, db: DbSession, user_id: UUID, refresh_token: str) -> OAuthTokenResponse:
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
            # csrf_token rotates with the token pair. Only SDK-provisioned connections have
            # somewhere to put it, and for everyone else this is a no-op — but for those that
            # do, skipping it leaves a stale token that fails at WebView-open time, far from
            # the refresh that caused it.
            self._persist_rotated_csrf_token(db, connection.id)
        log_structured(
            logger,
            "info",
            "Withings token refreshed",
            provider=self.provider_name,
            task="refresh_access_token",
            user_id=str(user_id),
        )
        return token_response

    def _persist_rotated_csrf_token(self, db: DbSession, connection_id: UUID) -> None:
        """Update the SDK account's csrf_token from the last token response, if there is one.

        Deliberately tolerant: a connection with no ``withings_sdk_account`` row is the normal
        case (phase-1 consumer OAuth), and a token response without ``csrf_token`` is not an
        error either. Neither is worth failing a refresh over.
        """
        csrf_token = (getattr(self, "_last_token_body", None) or {}).get("csrf_token")
        if not csrf_token:
            return

        account = (
            db.query(WithingsSdkAccount).filter(WithingsSdkAccount.user_connection_id == connection_id).one_or_none()
        )
        if account is None:
            return

        account.csrf_token = csrf_token
        account.updated_at = datetime.now(timezone.utc)
        # COMMIT, not flush. ``update_tokens`` above has already committed, so this write opens a
        # fresh unit of work of its own — and the caller that needs it most, the read-only
        # ``GET /providers/withings/sdk/session``, never commits. The request-scoped session is
        # closed without one (``_get_db_dependency``: ``finally: db.close()``), which discards it.
        # The result would be precisely the failure this method exists to prevent: Withings has
        # rotated the csrf_token, our copy is the old one, and the WebView refuses to open.
        db.commit()

    def _request_token(
        self, payload: dict[str, str], *, task: str, max_wait_seconds: float | None = None
    ) -> OAuthTokenResponse:
        """POST a token request and unwrap the Withings ``{status, body}`` envelope."""
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
                f"Withings token HTTP error: {redact_body(e.response.text)}",
                provider=self.provider_name,
                task=task,
                status_code=e.response.status_code,
            )
            # The body is logged above REDACTED, and never returned: WithingsTokenError subclasses
            # HTTPException, so `detail` is serialised to our own API caller — and this is the
            # response to a requesttoken whose payload carried client_secret and refresh_token.
            raise WithingsTokenError(
                task=task,
                http_status=e.response.status_code,
                detail=f"Withings token request failed (HTTP {e.response.status_code})",
            ) from e
        except Exception as e:
            log_structured(
                logger,
                "error",
                f"Withings token request failed: {redact_body(str(e))}",
                provider=self.provider_name,
                task=task,
            )
            # Same reasoning: an arbitrary exception's str() can embed a response fragment
            # (httpx errors quote the request URL, and a JSON parse error quotes the body).
            raise HTTPException(
                status_code=HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Withings token request failed",
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

        body = envelope.get("body", {})
        # Stash the raw body for callers that need a field OAuthTokenResponse does not model.
        # Withings returns csrf_token on every token response, and the SDK WebViews cannot
        # open without a CURRENT one — dropping it here is what would leave the stored copy
        # stale after any refresh.
        self._last_token_body = body
        return OAuthTokenResponse.model_validate(body)

    def _get_provider_user_info(self, token_response: OAuthTokenResponse, user_id: str) -> dict[str, str | None]:
        """Return the Withings ``userid`` from the token body — the key for inbound notifications."""
        extra = token_response.model_extra or {}
        userid = extra.get("userid")
        return {"user_id": str(userid) if userid is not None else None, "username": None}

    def deregister_user(self, access_token: str, provider_user_id: str | None = None) -> None:
        """Leave Notify teardown to the webhook subscription lifecycle."""
