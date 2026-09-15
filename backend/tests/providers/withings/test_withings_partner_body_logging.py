"""The signed partner surface must not log an upstream body, because the body is the member.

`/v2/sdk`, `/v2/dropshipment`, `/v2/order` and `devicev2-*` all answer a request WE signed, and an
error body from any of them may echo what we sent: a member's email, birth date and weight, and on
an order their home address as well.

**These pin the LOG, not the exception.** Every one of these modules already keeps the body out of
`detail` and is tested for it — and that test passes while the log leaks, which is exactly what
happened. `createuserorder` carried a comment claiming `redact_body` covered "a signature AND a
postal address"; driving the real function with a 400 body of `{"address": "Via Roma 1, Milano"}`
printed the address into the structured log verbatim. `redact_body` masks credential-shaped KEYS,
and an address is not one. The comment had been true of the intent and never of the code.

So the assertion here is deliberately crude and deliberately on the emitted line: whatever the
body was, none of it appears. A cleverer redaction is the thing that failed.

**Captured from stdout, not with `caplog`.** `log_structured` writes JSON directly to stdout and
never goes through a handler, so `caplog` records nothing from these modules — a leak test built
on it passes while the address is on the console. Found while writing this file, by capturing
nothing and noticing.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from typing import Any, Callable
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.models import User
from app.repositories.user_connection_repository import UserConnectionRepository
from app.repositories.user_repository import UserRepository
from app.services.providers.withings.dropshipment import WithingsDropshipmentError, create_user_order
from app.services.providers.withings.end_program import end_partner_program
from app.services.providers.withings.oauth import WithingsOAuth, WithingsTokenError
from app.services.providers.withings.order_detail import WithingsOrderDetailError, get_order_detail
from app.services.providers.withings.sdk_users import WithingsSdkUserError, create_sdk_user
from tests.providers.withings.test_withings_dropshipment import _PROFILE, _order

# A body carrying one token of each kind of thing these calls send, so a leak of any of them is
# caught by one assertion. None is credential-shaped, which is the point.
_MEMBER_TOKENS = ["Via Roma 1", "Milano", "member@example.com", "643248000", "75.4", "aa:bb:cc:dd:ee:ff"]
_ECHOED_BODY = json.dumps(
    {
        "error": "bad request",
        "echo": {
            "address1": "Via Roma 1",
            "city": "Milano",
            "email": "member@example.com",
            "birthdate": "643248000",
            "weight": "75.4",
            "mac_address": "aa:bb:cc:dd:ee:ff",
        },
    }
)


def _http_400() -> MagicMock:
    response = MagicMock(status_code=400, text=_ECHOED_BODY)
    response.raise_for_status.side_effect = httpx.HTTPStatusError("bad", request=MagicMock(), response=response)
    return response


def _drive(module: str, call: Callable[[], Any], expected: type[Exception] | None) -> None:
    """Run `call` against a 400 whose body echoes the member, with only httpx and signing stubbed."""
    with (
        patch(f"app.services.providers.withings.{module}.httpx.post", MagicMock(return_value=_http_400())),
        patch(f"app.services.providers.withings.{module}.sign_payload", side_effect=lambda p, *a, **k: p),
        patch(f"app.services.providers.withings.{module}.acquire_request_slot"),
    ):
        if expected is None:
            call()
        else:
            with pytest.raises(expected):
                call()


_CREDS = {"client_id": "cid", "client_secret": "secret"}

_CASES = [
    pytest.param(
        "dropshipment",
        lambda: create_user_order(**{**_PROFILE, "orders": [_order()]}),
        WithingsDropshipmentError,
        id="createuserorder",
    ),
    pytest.param(
        "order_detail",
        lambda: get_order_detail(**_CREDS, customer_ref_ids=["order-1"]),
        WithingsOrderDetailError,
        id="getdetail",
    ),
    pytest.param(
        "sdk_users",
        lambda: create_sdk_user(**_PROFILE),
        WithingsSdkUserError,
        id="createuser",
    ),
    pytest.param(
        "end_program",
        # Returns a result rather than raising: ending a programme reports failure in its value.
        lambda: end_partner_program(**_CREDS, mac_addresses=["aa:bb:cc:dd:ee:ff"]),
        None,
        id="endpartnerprogram",
    ),
]


def _emitted(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    """Every structured line this call wrote, parsed. Non-JSON lines are kept as a raw message."""
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    parsed: list[dict[str, Any]] = []
    for line in lines:
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            parsed.append({"message": line})
    return parsed


@pytest.mark.parametrize(("module", "call", "expected"), _CASES)
def test_no_member_data_reaches_the_log(
    module: str,
    call: Callable[[], Any],
    expected: type[Exception] | None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _drive(module, call, expected)

    records = _emitted(capsys)
    assert records, "the failure must still be logged — silence is not the fix"
    emitted = json.dumps(records)
    for token in _MEMBER_TOKENS:
        assert token not in emitted, f"{module} logged {token!r} from the upstream body"


@pytest.mark.parametrize(("module", "call", "expected"), _CASES)
def test_the_status_code_survives(
    module: str,
    call: Callable[[], Any],
    expected: type[Exception] | None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The positive control. Dropping the body is only correct if what a reader actually needs is
    # still there — otherwise the fix for a leak is a blind spot, which is the usual way this
    # class of change goes wrong.
    _drive(module, call, expected)

    assert any(record.get("status_code") == 400 for record in _emitted(capsys)), (
        f"{module} dropped the HTTP status along with the body"
    )


# ---------------------------------------------------------------------------
# The other failure branch: HTTP 200 with a non-zero envelope status.
#
# Everything above drives `_http_400()`, so all four cases take the HTTP-error branch. Withings'
# own validation failures do not arrive that way — they arrive as HTTP 200 carrying
# `{"status": 503, "error": "…", "body": {…}}`, which is a different code path in every one of
# these modules and was covered by none of them. Lucas, #14: the guard "covers four modules and
# not the one field on this surface that forwards an upstream string verbatim".
#
# That field is `withings_error`, and it is the one deliberate exception in this file. It carries
# Withings' `error` string, bounded to 200 characters — see `_body_logging.upstream_reason` for
# why. So this branch is pinned in two halves that have to be read together:
#
#   * the ENVELOPE BODY is withheld exactly as the HTTP-error body is, at every site;
#   * the `error` string is forwarded verbatim, and only it.
#
# Written to PERMIT the forwarding rather than to forbid it, because that is the decision taken.
# What it buys is the thing a silent guard cannot: if Withings ever start echoing a submitted
# value inside `error`, the second test is where it becomes visible, as a failing assertion
# naming the field — instead of as a member's address in a log six weeks later.
# ---------------------------------------------------------------------------

_ENVELOPE_ERROR = "Invalid Params: [unit] Missing value for: distance"
_ENVELOPE_503 = {
    "status": 503,
    "error": _ENVELOPE_ERROR,
    # The same member tokens as `_ECHOED_BODY`. Withings really do echo the submitted order back
    # in `body` on some failures, so this is the shape, not a worst case invented for the test.
    "body": {
        "address1": "Via Roma 1",
        "city": "Milano",
        "email": "member@example.com",
        "birthdate": "643248000",
        "weight": "75.4",
        "mac_address": "aa:bb:cc:dd:ee:ff",
    },
}


def _envelope_200() -> MagicMock:
    response = MagicMock(status_code=200, text=json.dumps(_ENVELOPE_503))
    response.json.return_value = _ENVELOPE_503
    response.raise_for_status.return_value = None
    return response


def _drive_envelope(module: str, call: Callable[[], Any], expected: type[Exception] | None) -> None:
    """As `_drive`, but the upstream answers 200 with a non-zero envelope status."""
    base = f"app.services.providers.withings.{module}"
    patches = [
        patch(f"{base}.httpx.post", MagicMock(return_value=_envelope_200())),
        patch(f"{base}.acquire_request_slot"),
    ]
    # `oauth` signs nothing — it is the one token-endpoint case here, and patching a name it does
    # not import would fail for a reason that has nothing to do with what is being tested.
    if module != "oauth":
        patches.append(patch(f"{base}.sign_payload", side_effect=lambda p, *a, **k: p))

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        if expected is None:
            call()
        else:
            with pytest.raises(expected):
                call()


def _withings_oauth() -> WithingsOAuth:
    return WithingsOAuth(
        user_repo=UserRepository(User),
        connection_repo=UserConnectionRepository(),
        provider_name="withings",
        api_base_url="https://wbsapi.withings.net",
    )


_ENVELOPE_CASES = [
    pytest.param(
        "dropshipment",
        lambda: create_user_order(**{**_PROFILE, "orders": [_order()]}),
        WithingsDropshipmentError,
        True,
        id="createuserorder",
    ),
    pytest.param(
        "oauth",
        lambda: _withings_oauth()._request_token({"action": "requesttoken"}, task="refresh_access_token"),
        WithingsTokenError,
        True,
        id="requesttoken",
    ),
    # The remaining three log `withings_status` and nothing else on this branch — no body and no
    # reason. Included so the no-leak half covers every site, and so that adding `upstream_reason`
    # to one of them later (a real diagnostic gap, deliberately not this PR's) arrives with its
    # guard already written.
    pytest.param(
        "order_detail",
        lambda: get_order_detail(**_CREDS, customer_ref_ids=["order-1"]),
        WithingsOrderDetailError,
        False,
        id="getdetail",
    ),
    pytest.param(
        "sdk_users",
        lambda: create_sdk_user(**_PROFILE),
        WithingsSdkUserError,
        False,
        id="createuser",
    ),
    pytest.param(
        "end_program",
        lambda: end_partner_program(**_CREDS, mac_addresses=["aa:bb:cc:dd:ee:ff"]),
        None,
        False,
        id="endpartnerprogram",
    ),
]


@pytest.mark.parametrize(("module", "call", "expected", "forwards_reason"), _ENVELOPE_CASES)
def test_no_member_data_reaches_the_log_on_a_non_zero_envelope_status(
    module: str,
    call: Callable[[], Any],
    expected: type[Exception] | None,
    forwards_reason: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _drive_envelope(module, call, expected)

    records = _emitted(capsys)
    assert records, "the failure must still be logged — silence is not the fix"
    emitted = json.dumps(records)
    for token in _MEMBER_TOKENS:
        assert token not in emitted, f"{module} logged {token!r} from the envelope body"


@pytest.mark.parametrize(("module", "call", "expected", "forwards_reason"), _ENVELOPE_CASES)
def test_the_envelope_status_survives_and_only_the_reason_is_forwarded(
    module: str,
    call: Callable[[], Any],
    expected: type[Exception] | None,
    forwards_reason: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The positive control, and the statement of the exception in one place. Withholding the
    # envelope is only correct if a reader is still told which parameter Withings refused.
    _drive_envelope(module, call, expected)

    records = _emitted(capsys)
    assert any(record.get("withings_status") == 503 for record in records), (
        f"{module} dropped the envelope status along with the body"
    )

    reasons = [record["withings_error"] for record in records if "withings_error" in record]
    if forwards_reason:
        assert reasons == [_ENVELOPE_ERROR], f"{module} did not forward Withings' own reason verbatim"
    else:
        assert reasons == [], f"{module} grew a withings_error field without a test saying so"
