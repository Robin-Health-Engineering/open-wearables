"""`recover_sdk_account` against states the SCHEMA allows, not just the ones we expect.

No database: the session is a stub whose query chain returns what the test is about. What is
under test is which failures the function converts into a `WithingsSdkUserError` — anything it
lets escape becomes a 500 with a traceback, outside the four status codes the route declares.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.exc import MultipleResultsFound

from app.services.providers.withings import sdk_provisioning
from app.services.providers.withings.sdk_provisioning import recover_sdk_account
from app.services.providers.withings.sdk_users import WithingsSdkUserError


class _Chain:
    """Every query builder call returns self; the terminal `one_or_none` does the work."""

    def __init__(self, outcome: Any) -> None:
        self._outcome = outcome

    def join(self, *_a: Any, **_k: Any) -> _Chain:
        return self

    def filter(self, *_a: Any, **_k: Any) -> _Chain:
        return self

    def one_or_none(self) -> Any:
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class _Db:
    def __init__(self, outcome: Any) -> None:
        self._outcome = outcome

    def query(self, *_a: Any, **_k: Any) -> _Chain:
        return _Chain(self._outcome)


def _recover(db: Any) -> Any:
    return recover_sdk_account(
        db,
        user_id=uuid4(),
        client_id="cid",
        client_secret="csecret",
        redirect_uri="https://example.test/cb",
    )


class TestRecoverPreconditions:
    def test_two_provisioned_accounts_is_a_conflict_not_a_crash(self) -> None:
        # The schema permits it: `ix_user_connection_user_provider` is unique on
        # (user_id, provider, provider_user_id), so two provisioned connections with different
        # provider_user_ids both satisfy it, and nothing in the provisioning route refuses a
        # second. Before this, `.one_or_none()` raised MultipleResultsFound straight through the
        # route as a 500 — the one outcome its `responses` cannot name, reaching the member who is
        # already worst off.
        with pytest.raises(WithingsSdkUserError) as e:
            _recover(_Db(MultipleResultsFound()))
        assert e.value.already_exists is True
        assert e.value.not_found is False

    def test_no_provisioned_account_is_not_found(self) -> None:
        with pytest.raises(WithingsSdkUserError) as e:
            _recover(_Db(None))
        assert e.value.not_found is True
        assert e.value.already_exists is False

    def test_a_connection_with_no_provider_user_id_is_not_found(self) -> None:
        class _Conn:
            provider_user_id = None

        class _Acct:
            external_id = "profile-1"

        with pytest.raises(WithingsSdkUserError) as e:
            _recover(_Db((_Conn(), _Acct())))
        assert e.value.not_found is True

    def test_the_happy_path_recovers_against_the_sdk_connection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Pins WHICH userid is sent to Withings: the provisioned connection's, never the member's
        # personal one. That choice is the reason this function exists rather than reusing the
        # primary-connection lookup.
        class _Conn:
            provider_user_id = "49550146"

        class _Acct:
            external_id = "profile-1"

        seen: dict[str, Any] = {}

        def fake_recover(*, client_id: str, client_secret: str, userid: str, **_k: Any) -> str:
            seen["userid"] = userid
            return "fresh-code"

        def fake_exchange(*, code: str, **_k: Any) -> str:
            seen["code"] = code
            return "tokens"  # opaque: _store_provisioned_account is stubbed too

        def fake_store(_db: Any, *, user_id: Any, external_id: str, tokens: Any) -> str:
            seen["external_id"] = external_id
            return "stored"

        monkeypatch.setattr(sdk_provisioning, "recover_authorization_code", fake_recover)
        monkeypatch.setattr(sdk_provisioning, "exchange_sdk_code", fake_exchange)
        monkeypatch.setattr(sdk_provisioning, "_store_provisioned_account", fake_store)

        assert _recover(_Db((_Conn(), _Acct()))) == "stored"
        assert seen == {"userid": "49550146", "code": "fresh-code", "external_id": "profile-1"}
