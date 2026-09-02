"""HMAC request signing for Withings service-level calls.

Withings does not authenticate these calls with the client secret in the request body.
The secret is the **HMAC key** and is never transmitted: you fetch a single-use ``nonce``,
concatenate an agreed set of parameter *values* sorted by key name, join them with commas,
and send the HMAC-SHA256 hex digest as ``signature``.

Two different key sets are in play, which is the easy thing to get wrong:

* ``getnonce`` has no nonce yet, so it signs ``action, client_id, timestamp``.
* every other signed call signs ``action, client_id, nonce``.

Only those keys are signed even when the request carries more fields - ``requesttoken``
also sends ``grant_type``, ``code``/``refresh_token`` and ``redirect_uri``, none of which
enter the digest.

This module is deliberately free of any dependency on ``BaseOAuthTemplate``: phase 2 of the
Withings integration needs the exact same primitive for ``POST /v2/sdk action=createuser``
(partner-provisioned accounts), and that call has nothing to do with OAuth.

Verified against the live API on 2026-09-02: a ``getnonce`` signed this way returns
``status: 0`` with our production credentials.
"""

import hashlib
import hmac
import logging
import time

import httpx

from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

# Signed for every call made *with* a nonce.
REQUEST_SIGNED_KEYS = ("action", "client_id", "nonce")
# Signed for the nonce request itself, which cannot reference a nonce.
NONCE_SIGNED_KEYS = ("action", "client_id", "timestamp")

_SIGNATURE_PATH = "/v2/signature"
_TIMEOUT_SECONDS = 30.0


class WithingsSignatureError(RuntimeError):
    """Raised when a nonce could not be obtained."""

    def __init__(self, *, withings_status: int | None = None, detail: str | None = None) -> None:
        self.withings_status = withings_status
        super().__init__(detail or f"Withings getnonce failed (status={withings_status})")


def sign(params: dict[str, str], client_secret: str, keys: tuple[str, ...] = REQUEST_SIGNED_KEYS) -> str:
    """Return the hex HMAC-SHA256 of the signed subset of ``params``.

    ``keys`` names which parameters take part; their VALUES are ordered by key name and
    comma-joined. A missing key is a programming error, not a runtime condition, so it
    raises rather than silently signing a shorter message that the API would reject with
    an opaque status.
    """
    missing = [k for k in keys if k not in params]
    if missing:
        raise KeyError(f"cannot sign Withings request, missing parameters: {missing}")
    message = ",".join(params[k] for k in sorted(keys))
    return hmac.new(client_secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def get_nonce(client_id: str, client_secret: str, *, api_base_url: str) -> str:
    """Fetch a single-use nonce.

    A fresh nonce is required per signed call - reusing one is what the mechanism exists
    to prevent - so callers must not cache the result.
    """
    params = {
        "action": "getnonce",
        "client_id": client_id,
        "timestamp": str(int(time.time())),
    }
    params["signature"] = sign(params, client_secret, NONCE_SIGNED_KEYS)

    response = httpx.post(
        f"{api_base_url}{_SIGNATURE_PATH}",
        data=params,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    envelope = response.json()

    status = envelope.get("status")
    if status != 0:
        log_structured(
            logger,
            "error",
            "Withings getnonce returned non-zero status",
            provider="withings",
            task="getnonce",
            withings_status=status,
        )
        raise WithingsSignatureError(withings_status=status)

    nonce = envelope.get("body", {}).get("nonce")
    if not nonce:
        raise WithingsSignatureError(detail="Withings getnonce returned no nonce")
    return str(nonce)


def sign_payload(payload: dict[str, str], client_id: str, client_secret: str, *, api_base_url: str) -> dict[str, str]:
    """Return ``payload`` with a fresh nonce and signature, and no client secret.

    The secret is dropped rather than left alongside the signature: sending both invites a
    future reader to assume the body-secret form still works, which is the flow we are
    moving off.
    """
    signed = {k: v for k, v in payload.items() if k != "client_secret"}
    signed["client_id"] = client_id
    signed["nonce"] = get_nonce(client_id, client_secret, api_base_url=api_base_url)
    signed["signature"] = sign(signed, client_secret, REQUEST_SIGNED_KEYS)
    return signed
