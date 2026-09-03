"""Partner-hosted User Creation for the Withings Mobile SDK.

Phase 2 of the Withings integration provisions a Withings account on the member's behalf,
rather than linking one they already own (which is phase 1's consumer OAuth flow). The two
coexist by design: a member can link their own account AND buy a device from us, and will
then have two ``provider_user_id``s. ``external_id`` is what ties the provisioned one back
to our member.

This lives in Open Wearables and not in robin-backend, deliberately. ``createuser`` must be
signed with ``client_secret``, and the ``code`` it returns becomes tokens that OW then owns
and refreshes. Putting it in robin-backend would mean a second copy of the secret and a
second refresher against a token Withings rotates on every use — which is the documented
way to end up with a dead connection.

Reference: https://developer.withings.com/sdk/v2/tree/sdk-webviews/required-web-services/
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.services.providers.withings._client import WITHINGS_API_BASE_URL
from app.services.providers.withings.oauth import redact_body
from app.services.providers.withings.request_budget import acquire_request_slot
from app.services.providers.withings.signature import sign_payload
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

_SDK_PATH = "/v2/sdk"
_TIMEOUT_SECONDS = 30.0

# Withings' own constraint, quoted from the docs: /^[a-zA-Z0-9]{3}$/. Enforced here rather
# than left to the API because the failure comes back as an opaque non-zero status, and this
# value is rendered on the device screen — a wrong one is visible on the hardware.
_SHORTNAME_RE = re.compile(r"^[a-zA-Z0-9]{3}$")

# Withings encodes success as status 0 inside an HTTP 200 body. Every other value is a
# failure that `raise_for_status` will never catch.
_STATUS_OK = 0


class WithingsSdkUserError(RuntimeError):
    """Raised when Withings declines to create the SDK user."""

    def __init__(self, *, withings_status: int | None = None, detail: str | None = None) -> None:
        self.withings_status = withings_status
        super().__init__(detail or f"Withings createuser failed (status={withings_status})")


@dataclass(frozen=True)
class SdkUser:
    """What ``createuser`` returns: a short-lived code, and our own id echoed back."""

    code: str
    external_id: str


def _measures_payload(weight_kg: float, height_m: float) -> str:
    """Withings takes measures as JSON, with a value/unit pair per measure.

    ``unit`` is a power of ten: value * 10^unit is the real quantity. At the milli precision
    used here, 75.4 kg is {value: 75400, unit: -3}, not {value: 75.4}. Sending a float is
    accepted and then silently misread, which is the worst of both.
    """
    return json.dumps(
        [
            {"value": int(round(weight_kg * 1000)), "unit": -3, "type": 1},
            {"value": int(round(height_m * 1000)), "unit": -3, "type": 4},
        ]
    )


def create_sdk_user(
    *,
    client_id: str,
    client_secret: str,
    external_id: str,
    email: str,
    shortname: str,
    birthdate: int,
    gender: int,
    weight_kg: float,
    height_m: float,
    preflang: str,
    timezone: str,
    mailingpref: int,
    unit_pref: dict[str, Any] | None = None,
    firstname: str | None = None,
    lastname: str | None = None,
    phonenumber: str | None = None,
    recovery_code: str | None = None,
    api_base_url: str = WITHINGS_API_BASE_URL,
) -> SdkUser:
    """Provision a Withings account for one member and return its authorization code.

    The returned ``code`` is short-lived and must be exchanged for tokens. That exchange is a
    follow-up rather than an unknown: Withings documents it as ``requesttoken`` with
    ``grant_type=authorization_code``, ``redirect_uri`` REQUIRED even though this code never
    came from a redirect, and nonce+signature rather than client_secret-in-body — so
    ``signature.py`` covers it too. The response carries ``csrf_token`` alongside the token
    pair, which is why ``withings_sdk_account`` exists to hold it.
    """
    if not _SHORTNAME_RE.match(shortname):
        raise ValueError(f"shortname must match {_SHORTNAME_RE.pattern} (Withings renders it on the device screen)")
    if gender not in (0, 1):
        raise ValueError("gender must be 0 (male) or 1 (female) per the Withings API")
    if mailingpref not in (0, 1):
        raise ValueError("mailingpref must be 0 (refused) or 1 (accepted)")

    payload: dict[str, str] = {
        "action": "createuser",
        "birthdate": str(birthdate),
        "email": email,
        "external_id": external_id,
        "gender": str(gender),
        "mailingpref": str(mailingpref),
        "measures": _measures_payload(weight_kg, height_m),
        "preflang": preflang,
        "shortname": shortname,
        "timezone": timezone,
        "unit_pref": json.dumps(unit_pref if unit_pref is not None else {}),
    }
    for key, value in (
        ("firstname", firstname),
        ("lastname", lastname),
        ("phonenumber", phonenumber),
        ("recovery_code", recovery_code),
    ):
        if value:
            payload[key] = value

    # Adds client_id, a fresh single-use nonce and the HMAC signature, and drops any secret.
    signed = sign_payload(payload, client_id, client_secret, api_base_url=api_base_url)

    acquire_request_slot()
    try:
        response = httpx.post(
            f"{api_base_url}{_SDK_PATH}",
            data=signed,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        envelope = response.json()
    except httpx.HTTPStatusError as e:
        # Redacted: the request body carried a signature, and the response may echo it back.
        log_structured(
            logger,
            "error",
            f"Withings createuser HTTP error: {redact_body(e.response.text)}",
            provider="withings",
            task="createuser",
            status_code=e.response.status_code,
        )
        raise WithingsSdkUserError(detail=f"Withings createuser failed (HTTP {e.response.status_code})") from e
    except Exception as e:
        log_structured(
            logger,
            "error",
            f"Withings createuser request failed: {type(e).__name__}",
            provider="withings",
            task="createuser",
        )
        raise WithingsSdkUserError(detail="Withings createuser request failed") from e

    status = envelope.get("status")
    if status != _STATUS_OK:
        # No body echo: it is the response to a signed request and may repeat our parameters.
        log_structured(
            logger,
            "error",
            "Withings createuser returned a non-zero status",
            provider="withings",
            task="createuser",
            withings_status=status,
        )
        raise WithingsSdkUserError(withings_status=status)

    user = (envelope.get("body") or {}).get("user") or {}
    code = user.get("code")
    if not code:
        # status 0 with no code is a contract change, not a user-facing condition.
        raise WithingsSdkUserError(detail="Withings createuser returned no code")

    log_structured(
        logger,
        "info",
        "Withings SDK user created",
        provider="withings",
        task="createuser",
        external_id=external_id,
    )
    # Trust our own external_id over the echo: the caller keys the member off it, and an
    # echoed value that differs is a mismatch we would otherwise store silently.
    return SdkUser(code=code, external_id=user.get("external_id") or external_id)
