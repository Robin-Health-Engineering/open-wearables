"""End the Withings cellular programme for a device we shipped.

A cellular device is not merely a data source. It carries a SIM on a plan Withings bills the
partner for, and it stays on that plan until we say otherwise. Revoking the member's connection
locally would stop us reading the account and leave the hardware transmitting and charging.

``Devicev2-endpartnerprogram`` is what says otherwise: it ends cellular billing and reconfigures
the device for personal use with the Withings app over Wi-Fi or BLE — the documented path for
"participants exit your program and retain their devices". It is addressed by the device's MAC
address, takes ``status=TERMINATED``, and the device switches to consumer firmware within about
48 hours. There is a short reversal window in which ``status=STANDBY`` undoes it, before the
update begins; we do not use it, but an operator recovering from a mistaken call has minutes.

Two things about where this lives.

**The MAC is a parameter, not state.** Which devices we shipped, their MACs and their order
status live in robin-backend's DynamoDB, because that is commerce rather than health data. But
robin-backend cannot make this call: the request is signed with the Withings client secret,
which lives here and should live in exactly one place. So the system holding the MAC passes it
in and this module signs and sends. Nothing in Open Wearables reads a MAC otherwise — the
dissociation sweep keys on Withings' own ``deviceid``, and sync never sees one — so there is
nothing to store.

**It is the same signed surface as ``createuser``.** ``devicev2-*`` is contract-gated and
authenticated with nonce+HMAC in the application's own name, exactly like ``/v2/sdk``, so
``signature.sign_payload`` covers it unchanged and no new credential is introduced.

**Not reachable yet, and here is what activates it.** Its only caller is
``WithingsStrategy._end_cellular_program``, which returns early because ``_device_mac_addresses``
has nothing to return: MACs live in robin-backend's ``WithingsDeviceOrder`` and arrive through the
``owWithingsDisconnect`` proxy (robin-backend#166), which passes ``device_macs`` into the
disconnect route. Until that ships, this module is fully tested and never executed — including
the Sentry capture in ``_end_cellular_program``, so "a failure here is reported rather than
logged" is a property of the code and not yet an observed one.

Reference: Withings "End Program API" — developer-guide/v3/get-access/terminatecellular
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.services.providers.withings._body_logging import describe_body
from app.services.providers.withings._client import WITHINGS_API_BASE_URL
from app.services.providers.withings.request_budget import acquire_request_slot
from app.services.providers.withings.sdk_users import STATUS_OK
from app.services.providers.withings.signature import sign_payload
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

_DEVICE_PATH = "/v2/device"
_ACTION = "endpartnerprogram"
_TIMEOUT_SECONDS = 30.0

# The programme state we are asking for. STANDBY is the documented reversal, available for a few
# minutes before the device begins updating; deliberately not wired — an undo path for a window
# that short is more likely to be wrong than useful, and an operator can call it by hand.
_STATUS_TERMINATED = "TERMINATED"


@dataclass(frozen=True)
class EndProgramResult:
    """What happened for ONE device.

    Carries the outcome rather than raising, because a disconnect must not be blocked by
    Withings being unreachable — see ``end_partner_program``.
    """

    mac_address: str
    ok: bool
    withings_status: int | None = None
    detail: str | None = None


def _end_one(
    *,
    client_id: str,
    client_secret: str,
    mac_address: str,
    api_base_url: str,
) -> EndProgramResult:
    payload = {
        "action": _ACTION,
        "mac_address": mac_address,
        "status": _STATUS_TERMINATED,
    }
    # Adds client_id, a fresh single-use nonce and the HMAC signature, and drops any secret.
    signed = sign_payload(payload, client_id, client_secret, api_base_url=api_base_url)

    acquire_request_slot()
    try:
        response = httpx.post(
            f"{api_base_url}{_DEVICE_PATH}",
            data=signed,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        envelope = response.json()
    except httpx.HTTPStatusError as e:
        # NOT logged: the request carried a signature and a device MAC, and the response may echo
        # either. A MAC identifies one member's hardware and is what End of Program is addressed
        # by, so it is kept out of the log for the same reason the profile fields are.
        log_structured(
            logger,
            "error",
            f"Withings endpartnerprogram HTTP error ({describe_body(e.response.text)})",
            provider="withings",
            task=_ACTION,
            status_code=e.response.status_code,
        )
        return EndProgramResult(
            mac_address=mac_address,
            ok=False,
            detail=f"HTTP {e.response.status_code}",
        )
    except Exception as e:
        log_structured(
            logger,
            "error",
            f"Withings endpartnerprogram request failed: {type(e).__name__}",
            provider="withings",
            task=_ACTION,
        )
        return EndProgramResult(mac_address=mac_address, ok=False, detail=type(e).__name__)

    status = envelope.get("status")
    if status != STATUS_OK:
        # No body echo: it is the response to a signed request and may repeat our parameters.
        log_structured(
            logger,
            "error",
            "Withings endpartnerprogram returned a non-zero status",
            provider="withings",
            task=_ACTION,
            withings_status=status,
        )
        return EndProgramResult(mac_address=mac_address, ok=False, withings_status=status)

    log_structured(
        logger,
        "info",
        "Withings cellular programme ended for a device",
        provider="withings",
        task=_ACTION,
    )
    return EndProgramResult(mac_address=mac_address, ok=True, withings_status=status)


def end_partner_program(
    *,
    client_id: str,
    client_secret: str,
    mac_addresses: list[str],
    api_base_url: str = WITHINGS_API_BASE_URL,
) -> list[EndProgramResult]:
    """End the cellular programme for each device, and report per device.

    **Takes a list because a connection can carry several devices.** One order can ship a scale
    and a blood-pressure monitor onto the same provisioned account, and disconnecting that
    account ends the programme for both. An EMPTY list is a legitimate, common case — a
    member-linked connection has no device of ours — and calls nothing.

    **Never raises.** The caller is a disconnect, and a member must be able to disconnect while
    Withings is unreachable; a failure here is reported to the caller and to the log, not thrown.
    That is a deliberate difference from ``create_sdk_user``, whose failure means no account was
    created and there is nothing to carry on with.

    It is also a deliberate difference from the notify-subscription teardown that runs beside it,
    which is best-effort and merely logged: notify failing is cosmetic, this failing leaves a real
    cellular plan billing us. Callers are expected to report a non-``ok`` result, not swallow it.
    """
    return [
        _end_one(
            client_id=client_id,
            client_secret=client_secret,
            mac_address=mac,
            api_base_url=api_base_url,
        )
        for mac in mac_addresses
    ]
