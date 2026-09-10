"""Ending a cellular programme: one call per device, and a failure nobody gets to swallow.

A cellular device sits on a plan Withings bills us for. Revoking a member's connection locally
stops us reading their data and does nothing to the hardware, which keeps transmitting and
keeps charging — so ``Devicev2-endpartnerprogram`` is not optional teardown, it is the part
with money attached.

The two properties these pin are in tension, and both matter: the call must never block a
member from disconnecting, and a failure must never be quietly dropped.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx

from app.services.providers.withings.end_program import end_partner_program

_POST = "app.services.providers.withings.end_program.httpx.post"
_SIGN = "app.services.providers.withings.end_program.sign_payload"
_SLOT = "app.services.providers.withings.end_program.acquire_request_slot"


def _ok(status: int = 0) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {"status": status, "body": {}}
    response.raise_for_status.return_value = None
    return response


def _run(macs: list[str], post: Any) -> list:
    with (
        patch(_SIGN, side_effect=lambda payload, *_a, **_k: {**payload, "signature": "sig", "nonce": "n"}),
        patch(_SLOT),
        patch(_POST, post),
    ):
        return end_partner_program(
            client_id="client-id",
            client_secret="client-secret",
            mac_addresses=macs,
            api_base_url="https://wbsapi.example.net",
        )


class TestEndPartnerProgram:
    def test_terminates_each_device_by_mac(self) -> None:
        post = MagicMock(return_value=_ok())

        results = _run(["AA:BB:CC:00:11:22", "AA:BB:CC:33:44:55"], post)

        assert [r.ok for r in results] == [True, True]
        assert post.call_count == 2
        sent = [call.kwargs["data"] for call in post.call_args_list]
        assert [d["mac_address"] for d in sent] == ["AA:BB:CC:00:11:22", "AA:BB:CC:33:44:55"]
        assert {d["status"] for d in sent} == {"TERMINATED"}
        assert {d["action"] for d in sent} == {"endpartnerprogram"}

    def test_an_empty_list_calls_nothing(self) -> None:
        # The member-linked case, and the ordinary one: a connection the member owns has no
        # device of ours on it, and asking Withings to end a programme that does not exist is
        # an error we would then have to explain.
        post = MagicMock(return_value=_ok())

        assert _run([], post) == []
        post.assert_not_called()

    def test_signs_every_request(self) -> None:
        # devicev2-* is the contract-gated surface: nonce + HMAC in the application's own name,
        # never an access token. An unsigned call is rejected with an opaque status.
        post = MagicMock(return_value=_ok())

        _run(["AA:BB:CC:00:11:22"], post)

        data = post.call_args.kwargs["data"]
        assert data["signature"] == "sig"
        assert data["nonce"] == "n"
        assert "client_secret" not in data

    def test_a_non_zero_withings_status_is_a_failure_not_an_exception(self) -> None:
        # Withings reports failure as a status inside an HTTP 200, which raise_for_status will
        # never see. Reported back to the caller rather than raised, because the disconnect
        # that called this has to finish.
        post = MagicMock(return_value=_ok(status=277))

        results = _run(["AA:BB:CC:00:11:22"], post)

        assert results[0].ok is False
        assert results[0].withings_status == 277

    def test_an_http_error_is_a_failure_not_an_exception(self) -> None:
        response = MagicMock(status_code=503, text="upstream unavailable")
        post = MagicMock(
            side_effect=httpx.HTTPStatusError("boom", request=MagicMock(), response=response),
        )

        results = _run(["AA:BB:CC:00:11:22"], post)

        assert results[0].ok is False
        assert results[0].detail == "HTTP 503"

    def test_a_transport_failure_is_a_failure_not_an_exception(self) -> None:
        # Withings being unreachable must not trap a member on a device they want rid of.
        post = MagicMock(side_effect=httpx.ConnectError("no route"))

        results = _run(["AA:BB:CC:00:11:22"], post)

        assert results[0].ok is False
        assert results[0].detail == "ConnectError"

    def test_one_failing_device_does_not_stop_the_others(self) -> None:
        # One order can ship several devices onto one account. A scale that fails must not
        # leave the monitor beside it on a plan we are still paying for.
        post = MagicMock(side_effect=[httpx.ConnectError("no route"), _ok()])

        results = _run(["AA:BB:CC:00:11:22", "AA:BB:CC:33:44:55"], post)

        assert [r.ok for r in results] == [False, True]
        assert [r.mac_address for r in results] == ["AA:BB:CC:00:11:22", "AA:BB:CC:33:44:55"]
