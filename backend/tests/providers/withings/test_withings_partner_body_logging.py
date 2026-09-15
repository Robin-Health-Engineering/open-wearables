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
from typing import Any, Callable
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.services.providers.withings.dropshipment import WithingsDropshipmentError, create_user_order
from app.services.providers.withings.end_program import end_partner_program
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
