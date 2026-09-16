"""Withings' `error` string is the only thing that says WHICH parameter they rejected.

A cellular order came back `status=503` on staging and the failure was undiagnosable: the log had
the number and nothing else, because the whole envelope was withheld under the PII rule. The rule
is right about the BODY — it echoes a signed payload containing the member's address — but the
envelope's own `error` field is Withings describing their own validation, and a real 503 from the
token endpoint proved the shape:

    {"body": {}, "error": "Invalid Params: invalid refresh_token", "status": 503}

A parameter name. So it is surfaced, bounded, with the bound justified by the fact that the
"names parameters only" guarantee is Withings' to keep rather than ours.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from app.models import User
from app.repositories.user_connection_repository import UserConnectionRepository
from app.repositories.user_repository import UserRepository
from app.services.providers.withings._body_logging import upstream_reason
from app.services.providers.withings.dropshipment import WithingsDropshipmentError
from app.services.providers.withings.oauth import WithingsOAuth, WithingsTokenError
from tests.providers.withings.test_withings_dropshipment import _call

_OAUTH = "app.services.providers.withings.oauth"


class TestUpstreamReason:
    def test_returns_withings_own_words(self) -> None:
        envelope = {"body": {}, "error": "Invalid Params: invalid refresh_token", "status": 503}

        assert upstream_reason(envelope) == "Invalid Params: invalid refresh_token"

    def test_is_bounded(self) -> None:
        # The guarantee that this only ever names parameters belongs to Withings. If they ever echo
        # a submitted value, the cap decides how much of it reaches a log line.
        assert len(upstream_reason({"error": "x" * 5000}) or "") == 200

    def test_collapses_newlines(self) -> None:
        # A multi-line upstream string would otherwise break one structured log record into
        # several, which is how a log line stops being greppable.
        assert upstream_reason({"error": "Invalid\nParams:\r\n  bad ean"}) == "Invalid Params: bad ean"

    def test_absent_or_unusable_gives_none(self) -> None:
        # None rather than a placeholder. `log_structured` spreads **attributes into the record
        # without dropping None, so the field is emitted as `"withings_error": null` rather than
        # omitted — which is the property that matters: null says "we asked and they were silent",
        # where a placeholder would say "we lost it" and an absent key would say "this log line
        # predates the field". (ross, #14.)
        for envelope in ({}, {"error": ""}, {"error": "   "}, {"error": 42}, {"status": 503}, None, "nope", []):
            assert upstream_reason(envelope) is None, envelope


class TestSpentRefreshToken:
    """A dead refresh token must revoke the connection, and Withings do not say so with a status.

    They answer HTTP 200 with `{"status": 503, "error": "Invalid Params: invalid refresh_token"}`.
    503 is their catch-all Invalid Params — the same code a dropshipment order returns for a
    missing unit_pref key — so the status alone cannot mean "spent grant" and the reason string is
    the discriminator.

    Before this, the classifier saw HTTP 200 and an unrecognised status, called it a 500, and left
    `invalid_grant` False. `_revoke_connection` never ran: the connection stayed ACTIVE with a dead
    token, sync retried it every cycle, and the member was never prompted to reconnect — 19 events
    over 5 days on staging with their data silently not syncing (OW-BACKEND-4).
    """

    def test_a_503_naming_the_refresh_token_is_a_spent_grant(self) -> None:
        err = WithingsTokenError(
            task="refresh_access_token",
            withings_status=503,
            upstream_reason="Invalid Params: invalid refresh_token",
        )

        assert err.invalid_grant is True
        # 401, not 500: the status code has to say "reconnect", not "we broke".
        assert err.status_code == 401

    def test_a_503_about_anything_else_is_not_a_spent_grant(self) -> None:
        # The whole reason this is keyed on the reason and not the status. Revoking a member's
        # connection over an unrelated validation error would disconnect them for someone else's
        # bug — and this exact string came back from a dropshipment order the same day.
        err = WithingsTokenError(
            task="refresh_access_token",
            withings_status=503,
            upstream_reason="Invalid Params: [unit] Missing value for: distance",
        )

        assert err.invalid_grant is False

    def test_a_503_with_no_reason_at_all_is_not_a_spent_grant(self) -> None:
        # Fails safe: an unexplained 503 leaves the connection alone rather than revoking on a guess.
        assert WithingsTokenError(task="refresh_access_token", withings_status=503).invalid_grant is False

    def test_it_is_only_a_spent_grant_on_refresh(self) -> None:
        # The same sentence during a code exchange says nothing about a stored grant, because there
        # is no stored grant yet.
        err = WithingsTokenError(
            task="exchange_code",
            withings_status=503,
            upstream_reason="Invalid Params: invalid refresh_token",
        )

        assert err.invalid_grant is False
        # And the HTTP status agrees with that verdict rather than contradicting it.
        assert err.status_code != 401

    def test_the_documented_auth_statuses_still_classify(self) -> None:
        # The positive control for the change: widening the rule must not have replaced the
        # existing one.
        for status in (100, 101, 102, 200, 401):
            err = WithingsTokenError(task="refresh_access_token", withings_status=status)
            assert err.invalid_grant is True, status


class TestTheReasonSurvivesTheEnvelopeHop:
    """Pins the WIRING rather than the rule, at both call sites.

    Everything above this class passes with `upstream_reason=reason` deleted from both `raise`
    statements: the classifier tests construct the exception directly, and the pure-function tests
    never touch a call site. So the feature was deletable in silence — measured by Lucas on #14,
    33 passed before and after removing both halves.

    That matters most on the token path, where the deletion is not a lost log line but a restored
    incident: `upstream_reason` None → `spent_refresh_token` False → `invalid_grant` False →
    `_revoke_connection` never runs → the connection stays ACTIVE with a dead token (OW-BACKEND-4).

    Both read stdout rather than `caplog`, for the reason `test_withings_partner_body_logging.py`
    documents: `log_structured` writes to stdout and never touches the Logger.
    """

    @staticmethod
    def _entry(captured: str, message_fragment: str) -> dict:
        return next(
            json.loads(line)
            for line in captured.splitlines()
            if line.strip().startswith("{") and message_fragment in line
        )

    def test_dropshipment_carries_it_to_the_log_and_the_exception(self, capsys: pytest.CaptureFixture[str]) -> None:
        response = MagicMock()
        response.json.return_value = {"status": 503, "error": "Invalid Params: invalid ean", "body": {}}
        response.raise_for_status.return_value = None

        with pytest.raises(WithingsDropshipmentError) as raised:
            _call(MagicMock(return_value=response))

        assert raised.value.upstream_reason == "Invalid Params: invalid ean"
        entry = self._entry(capsys.readouterr().out, "non-zero status")
        assert entry["withings_error"] == "Invalid Params: invalid ean"

    def test_the_token_endpoint_carries_it_all_the_way_to_the_revoke_decision(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        response = MagicMock()
        response.json.return_value = {"status": 503, "error": "Invalid Params: invalid refresh_token"}
        response.raise_for_status.return_value = None

        oauth = WithingsOAuth(
            user_repo=UserRepository(User),
            connection_repo=UserConnectionRepository(),
            provider_name="withings",
            api_base_url="https://wbsapi.withings.net",
        )

        with (
            patch(f"{_OAUTH}.httpx.post", MagicMock(return_value=response)),
            patch(f"{_OAUTH}.acquire_request_slot"),
            pytest.raises(WithingsTokenError) as raised,
        ):
            oauth._request_token({"action": "requesttoken"}, task="refresh_access_token")

        assert raised.value.upstream_reason == "Invalid Params: invalid refresh_token"
        # The three assertions that make this the OW-BACKEND-4 guard and not a log-field test.
        assert raised.value.invalid_grant is True
        assert raised.value.status_code == 401
        entry = self._entry(capsys.readouterr().out, "envelope status non-zero")
        assert entry["withings_error"] == "Invalid Params: invalid refresh_token"
