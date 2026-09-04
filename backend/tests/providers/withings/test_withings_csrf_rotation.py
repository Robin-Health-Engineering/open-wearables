"""The csrf_token must survive a token refresh.

Withings reissues csrf_token with every token response. The connector's refresh persists the
access and refresh tokens and, before this, dropped the csrf_token — which would leave the
stored copy stale and fail at WebView-open time, far from the refresh that caused it. That is
a silent failure with a long fuse, so the branches get pinned here.

These are control-flow tests with a mock session on purpose: the two branches that matter are
"there is nothing to persist" and "there is nowhere to persist it", and both must leave the
session untouched rather than raise. A refresh is not worth failing over an SDK detail that
does not apply to the connection being refreshed.

The write branch asserts ``commit``, not ``flush``, and that distinction is the bug this file
exists to keep out: ``update_tokens`` has already committed by the time this runs, so a flushed
csrf write sits in a new uncommitted transaction, and the read-only session endpoint that needs
it never commits — the request session is closed without one and the write is discarded.

Deliberately NOT also covered by an integration test, because one would not discriminate. The
``db`` fixture (tests/conftest.py) wraps each test in a savepoint that is restarted after every
commit and rolled back at the end, and the route under test shares that same session — so a
flush and a commit are equally visible to it. A test that passes either way is worse than no
test, because it reads like the invariant is pinned.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

from app.services.providers.withings.oauth import WithingsOAuth


def _strategy() -> WithingsOAuth:
    """A strategy instance without running __init__, which wants live credentials."""
    return WithingsOAuth.__new__(WithingsOAuth)


class TestPersistRotatedCsrfToken:
    def test_does_nothing_when_the_response_carried_no_csrf_token(self) -> None:
        strategy = _strategy()
        strategy._last_token_body = {"access_token": "at"}
        db = MagicMock()

        strategy._persist_rotated_csrf_token(db, uuid4())

        db.query.assert_not_called()
        db.flush.assert_not_called()
        db.commit.assert_not_called()

    def test_does_nothing_when_no_token_request_has_been_made(self) -> None:
        # _last_token_body is only set by _request_token. A refresh that failed before that
        # must not explode here on a missing attribute.
        strategy = _strategy()
        db = MagicMock()

        strategy._persist_rotated_csrf_token(db, uuid4())

        db.query.assert_not_called()

    def test_does_nothing_for_a_connection_with_no_sdk_account(self) -> None:
        # The normal case: phase-1 consumer OAuth connections have no SDK account row, and
        # every one of their refreshes reaches this method.
        strategy = _strategy()
        strategy._last_token_body = {"csrf_token": "fresh"}
        db = MagicMock()
        db.query.return_value.filter.return_value.one_or_none.return_value = None

        strategy._persist_rotated_csrf_token(db, uuid4())

        db.flush.assert_not_called()
        db.commit.assert_not_called()

    def test_writes_the_new_csrf_token_and_stamps_updated_at(self) -> None:
        strategy = _strategy()
        strategy._last_token_body = {"csrf_token": "fresh"}
        account = MagicMock()
        account.csrf_token = "stale"
        db = MagicMock()
        db.query.return_value.filter.return_value.one_or_none.return_value = account

        strategy._persist_rotated_csrf_token(db, uuid4())

        assert account.csrf_token == "fresh"
        assert account.updated_at is not None
        # The rotation has to be DURABLE, not merely flushed — see the module docstring.
        db.commit.assert_called_once()
