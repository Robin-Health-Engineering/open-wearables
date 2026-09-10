"""A refresh must write to the connection it refreshed, and lock the one it wrote.

With one connection per member per provider, "the member's Withings connection" was an
unambiguous phrase and the token path leaned on it in three places: resolving the connection,
keying the Redis refresh lock, and re-resolving inside that lock. A member can now hold several
— their own linked Withings account, plus one per cellular device we shipped them — and each of
those three is a way to write account B's tokens onto account A's row.

Mocks rather than the session fixture here, deliberately: what is being pinned is which
arguments the token path passes and which key it locks, not what a row looks like afterwards.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services.providers.api_client import _get_valid_token

_REDIS = "app.services.providers.api_client.get_redis_client"


class _Recorder:
    """A Redis stand-in that records the key every lock was taken on."""

    def __init__(self) -> None:
        self.keys: list[str] = []

    def lock(self, key: str, **_: Any) -> Any:
        self.keys.append(key)

        @contextmanager
        def _noop() -> Any:
            yield

        return _noop()


def _connection(*, expired: bool, refresh_token: str | None = "refresh") -> MagicMock:
    connection = MagicMock()
    connection.id = uuid4()
    connection.access_token = "access"
    connection.refresh_token = refresh_token
    connection.token_expires_at = datetime.now(timezone.utc) + (
        timedelta(minutes=-1) if expired else timedelta(hours=2)
    )
    return connection


def _repo(*, by_id: MagicMock, primary: MagicMock) -> MagicMock:
    repo = MagicMock()
    repo.get.return_value = by_id
    repo.get_by_user_and_provider.return_value = primary
    return repo


class TestWhichConnectionIsUsed:
    def test_a_named_connection_is_fetched_by_id_not_by_provider(self) -> None:
        named = _connection(expired=False)
        primary = _connection(expired=False)
        primary.access_token = "primary-access"
        repo = _repo(by_id=named, primary=primary)
        user_id = uuid4()

        token = _get_valid_token(MagicMock(), user_id, "withings", repo, MagicMock(), named.id)

        assert token == "access"
        repo.get.assert_called_once_with(repo.get.call_args[0][0], named.id)
        repo.get_by_user_and_provider.assert_not_called()

    def test_no_named_connection_falls_back_to_the_primary(self) -> None:
        # The twelve providers that can only ever have one connection, and every existing
        # caller: behaviour must be exactly what it was.
        primary = _connection(expired=False)
        repo = _repo(by_id=MagicMock(), primary=primary)

        _get_valid_token(MagicMock(), uuid4(), "garmin", repo, MagicMock())

        repo.get_by_user_and_provider.assert_called_once()
        repo.get.assert_not_called()

    def test_an_unknown_connection_id_is_unauthorized_rather_than_the_primary(self) -> None:
        # Falling back here would silently authenticate as a DIFFERENT Withings account than
        # the caller named, which is the whole failure this parameter exists to prevent.
        repo = _repo(by_id=None, primary=_connection(expired=False))

        with pytest.raises(HTTPException) as exc:
            _get_valid_token(MagicMock(), uuid4(), "withings", repo, MagicMock(), uuid4())

        assert exc.value.status_code == 401


class TestRefreshingAnExpiredToken:
    def test_refreshes_the_named_connection_and_says_which(self) -> None:
        named = _connection(expired=True)
        repo = _repo(by_id=named, primary=_connection(expired=True))
        oauth = MagicMock()
        oauth.refresh_access_token.return_value = MagicMock(access_token="fresh")
        user_id = uuid4()

        with patch(_REDIS, return_value=_Recorder()):
            token = _get_valid_token(MagicMock(), user_id, "withings", repo, oauth, named.id)

        assert token == "fresh"
        assert oauth.refresh_access_token.call_args.kwargs["connection_id"] == named.id

    def test_re_reads_the_same_connection_inside_the_lock(self) -> None:
        # The subtle half. The re-read inside the lock used to call get_by_user_and_provider,
        # so with two connections the lock guarded one row while the refresh wrote to another.
        named = _connection(expired=True)
        repo = _repo(by_id=named, primary=_connection(expired=True))
        oauth = MagicMock()
        oauth.refresh_access_token.return_value = MagicMock(access_token="fresh")

        with patch(_REDIS, return_value=_Recorder()):
            _get_valid_token(MagicMock(), uuid4(), "withings", repo, oauth, named.id)

        assert repo.get_by_user_and_provider.call_count == 0
        assert all(call.args[1] == named.id for call in repo.get.call_args_list)

    def test_the_lock_key_names_the_connection(self) -> None:
        # Two connections of one member must not serialise on one lock, and — the real defect —
        # a lock that does not name the connection cannot protect the row being written.
        named = _connection(expired=True)
        repo = _repo(by_id=named, primary=named)
        oauth = MagicMock()
        oauth.refresh_access_token.return_value = MagicMock(access_token="fresh")
        recorder = _Recorder()
        user_id = uuid4()

        with patch(_REDIS, return_value=recorder):
            _get_valid_token(MagicMock(), user_id, "withings", repo, oauth, named.id)

        assert recorder.keys == [f"token_refresh_lock:withings:{user_id}:{named.id}"]

    def test_two_connections_of_one_member_lock_different_keys(self) -> None:
        user_id = uuid4()
        recorder = _Recorder()
        oauth = MagicMock()
        oauth.refresh_access_token.return_value = MagicMock(access_token="fresh")

        for _ in range(2):
            connection = _connection(expired=True)
            repo = _repo(by_id=connection, primary=connection)
            with patch(_REDIS, return_value=recorder):
                _get_valid_token(MagicMock(), user_id, "withings", repo, oauth, connection.id)

        assert len(set(recorder.keys)) == 2, "both refreshes contended on one lock"
