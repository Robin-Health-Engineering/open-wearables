from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.integrations.celery.task_names import SYNC_PROVIDER_USER_SUBSCRIPTION_TASK
from app.models import User, UserConnection
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.auth import ConnectionStatus, LiveSyncMode
from app.services.providers.withings.applis import SUBSCRIBED_APPLIS
from app.services.providers.withings.callback import (
    MANAGED_COMMENT,
    WithingsCallbackUrlInvalidError,
    WithingsWebhookTokenUnconfiguredError,
)
from app.services.providers.withings.notify_service import WithingsNotifyService
from app.services.providers.withings.oauth import WithingsTokenError
from app.services.providers.withings.request_budget import WithingsRequestBudgetExceeded
from tests.factories import UserConnectionFactory, UserFactory
from tests.providers.withings.conftest import ProvisionedConnectionMaker

_OUR_CALLBACK = "https://api.example.com/api/v1/providers/withings/webhooks?token=current"
_OUR_CALLBACK_STALE_TOKEN = "https://api.example.com/api/v1/providers/withings/webhooks?token=old"
_FOREIGN_CALLBACK = "https://staging.example.com/api/v1/providers/withings/webhooks?token=current"


def _service() -> WithingsNotifyService:
    return WithingsNotifyService(connection_repo=MagicMock(), oauth=MagicMock())


def _profiles(*entries: tuple[int, str]) -> dict:
    return {"profiles": [{"appli": appli, "callbackurl": url} for appli, url in entries]}


@patch("app.services.providers.withings.notify_service.withings_callback_url")
@patch("app.services.providers.withings.notify_service.withings_request")
def test_sync_user_skips_unconfigured_callback_token(mock_req: MagicMock, mock_url: MagicMock) -> None:
    mock_url.side_effect = WithingsWebhookTokenUnconfiguredError

    results = _service().sync_user(MagicMock(), uuid4(), LiveSyncMode.WEBHOOK)

    assert results == [{"status": "skipped", "reason": "webhook_token_unconfigured"}]
    mock_req.assert_not_called()


@patch("app.services.providers.withings.notify_service.withings_callback_url", return_value=_OUR_CALLBACK)
@patch("app.services.providers.withings.notify_service.withings_request")
def test_sync_user_is_a_noop_when_already_fully_subscribed(mock_req: MagicMock, mock_url: MagicMock) -> None:
    mock_req.return_value = _profiles(*[(appli, _OUR_CALLBACK) for appli in SUBSCRIBED_APPLIS])
    service = _service()

    results = service.sync_user(MagicMock(), uuid4(), LiveSyncMode.WEBHOOK)

    assert mock_req.call_count == 1
    assert {r["status"] for r in results} == {"unchanged"}
    assert {r["appli"] for r in results} == set(SUBSCRIBED_APPLIS)


@patch("app.services.providers.withings.notify_service.withings_callback_url", return_value=_OUR_CALLBACK)
@patch("app.services.providers.withings.notify_service.withings_request")
def test_sync_user_subscribes_only_the_missing_applis(mock_req: MagicMock, mock_url: MagicMock) -> None:
    already_subscribed = SUBSCRIBED_APPLIS[0]

    def side_effect(*, action: str, params: dict, **_kwargs: object) -> dict:
        if action == "list":
            return _profiles((already_subscribed, _OUR_CALLBACK))
        return {}

    mock_req.side_effect = side_effect
    service = _service()

    results = service.sync_user(MagicMock(), uuid4(), LiveSyncMode.WEBHOOK)

    subscribe_calls = [c for c in mock_req.call_args_list if c.kwargs["action"] == "subscribe"]
    assert {c.kwargs["params"]["appli"] for c in subscribe_calls} == set(SUBSCRIBED_APPLIS) - {already_subscribed}
    for call in subscribe_calls:
        assert call.kwargs["params"]["comment"] == MANAGED_COMMENT
    statuses = {r["appli"]: r["status"] for r in results}
    assert statuses[already_subscribed] == "unchanged"
    assert all(statuses[appli] == "subscribed" for appli in SUBSCRIBED_APPLIS if appli != already_subscribed)


@patch("app.services.providers.withings.notify_service.withings_callback_url", return_value=_OUR_CALLBACK)
@patch("app.services.providers.withings.notify_service.withings_request")
def test_sync_user_revokes_own_host_subscriptions_switching_to_pull(mock_req: MagicMock, mock_url: MagicMock) -> None:
    def side_effect(*, action: str, params: dict, **_kwargs: object) -> dict:
        if action == "list":
            return _profiles(*[(appli, _OUR_CALLBACK) for appli in SUBSCRIBED_APPLIS])
        return {}

    mock_req.side_effect = side_effect
    service = _service()

    results = service.sync_user(MagicMock(), uuid4(), LiveSyncMode.PULL)

    revoke_calls = [c for c in mock_req.call_args_list if c.kwargs["action"] == "revoke"]
    assert {c.kwargs["params"]["appli"] for c in revoke_calls} == set(SUBSCRIBED_APPLIS)
    assert {r["status"] for r in results} == {"revoked"}


@patch("app.services.providers.withings.notify_service.withings_callback_url", return_value=_OUR_CALLBACK)
@patch("app.services.providers.withings.notify_service.withings_request")
def test_sync_user_replaces_a_stale_token_at_the_same_endpoint(mock_req: MagicMock, mock_url: MagicMock) -> None:
    appli = SUBSCRIBED_APPLIS[0]

    def side_effect(*, action: str, params: dict, **_kwargs: object) -> dict:
        if action == "list":
            return _profiles((appli, _OUR_CALLBACK_STALE_TOKEN))
        return {}

    mock_req.side_effect = side_effect
    service = _service()

    service.sync_user(MagicMock(), uuid4(), LiveSyncMode.WEBHOOK)

    subscribe_calls = [c for c in mock_req.call_args_list if c.kwargs["action"] == "subscribe"]
    revoke_calls = [c for c in mock_req.call_args_list if c.kwargs["action"] == "revoke"]
    assert {c.kwargs["params"]["appli"] for c in subscribe_calls} == set(SUBSCRIBED_APPLIS)
    assert [c.kwargs["params"]["callbackurl"] for c in revoke_calls] == [_OUR_CALLBACK_STALE_TOKEN]
    assert revoke_calls[0].kwargs["params"]["appli"] == appli


@patch("app.services.providers.withings.notify_service.withings_callback_url", return_value=_OUR_CALLBACK)
@patch("app.services.providers.withings.notify_service.withings_request")
def test_sync_user_leaves_foreign_host_subscriptions_untouched(mock_req: MagicMock, mock_url: MagicMock) -> None:
    appli = SUBSCRIBED_APPLIS[0]
    mock_req.return_value = _profiles((appli, _FOREIGN_CALLBACK))
    service = _service()

    results = service.sync_user(MagicMock(), uuid4(), LiveSyncMode.PULL)

    assert mock_req.call_count == 1
    assert results == []


@patch("app.services.providers.withings.notify_service.withings_callback_url", return_value=_OUR_CALLBACK)
@patch("app.services.providers.withings.notify_service.withings_request")
def test_sync_user_reports_an_error_when_listing_fails(mock_req: MagicMock, mock_url: MagicMock) -> None:
    mock_req.side_effect = RuntimeError("boom")
    service = _service()

    results = service.sync_user(MagicMock(), uuid4(), LiveSyncMode.WEBHOOK)

    assert mock_req.call_count == 1
    assert results == [{"status": "error", "error": "boom"}]


@patch("app.services.providers.withings.notify_service.withings_callback_url", return_value=_OUR_CALLBACK)
@patch("app.services.providers.withings.notify_service.withings_request")
def test_sync_user_skips_without_retrying_on_invalid_grant(mock_req: MagicMock, mock_url: MagicMock) -> None:
    mock_req.side_effect = WithingsTokenError(task="refresh_access_token", withings_status=200)
    service = _service()

    results = service.sync_user(MagicMock(), uuid4(), LiveSyncMode.WEBHOOK)

    assert mock_req.call_count == 1
    assert results == [{"status": "skipped", "reason": "invalid_grant"}]


@patch("app.services.providers.withings.notify_service.withings_callback_url", return_value=_OUR_CALLBACK)
@patch("app.services.providers.withings.notify_service.withings_request")
def test_sync_user_skips_on_a_spent_refresh_token_too(mock_req: MagicMock, mock_url: MagicMock) -> None:
    """The second consumer of the widened `invalid_grant`, pinned as a deliberate outcome.

    Withings answer a spent refresh token with a generic 503 naming the token, so this exception
    did not report `invalid_grant` before — it fell through to `log_and_capture_error` and reached
    Sentry. It now takes the skip branch. That is intended: the condition is terminal until the
    member reconnects, and the revoke that fires alongside it is what asks them to. Asserted so
    the change stays a decision rather than a quiet drop in event volume (Lucas, #14).
    """
    mock_req.side_effect = WithingsTokenError(
        task="refresh_access_token",
        withings_status=503,
        upstream_reason="Invalid Params: invalid refresh_token",
    )
    service = _service()

    results = service.sync_user(MagicMock(), uuid4(), LiveSyncMode.WEBHOOK)

    assert mock_req.call_count == 1
    assert results == [{"status": "skipped", "reason": "invalid_grant"}]


@patch("app.services.providers.withings.notify_service.withings_callback_url", return_value=_OUR_CALLBACK)
@patch("app.services.providers.withings.notify_service.withings_request")
def test_sync_user_still_reports_an_unrelated_503(mock_req: MagicMock, mock_url: MagicMock) -> None:
    # The control for the case above: a 503 that does not name the refresh token is an ordinary
    # error and must keep reaching Sentry. Without this, the widening above is indistinguishable
    # from "every 503 now goes quiet".
    mock_req.side_effect = WithingsTokenError(
        task="refresh_access_token",
        withings_status=503,
        upstream_reason="Invalid Params: [unit] Missing value for: distance",
    )
    service = _service()

    results = service.sync_user(MagicMock(), uuid4(), LiveSyncMode.WEBHOOK)

    assert results != [{"status": "skipped", "reason": "invalid_grant"}]
    assert results[0]["status"] == "error"


@patch("app.services.providers.withings.notify_service.withings_callback_url", return_value=_OUR_CALLBACK)
@patch("app.services.providers.withings.notify_service.withings_request")
def test_sync_user_retries_token_rate_limit(mock_req: MagicMock, mock_url: MagicMock) -> None:
    mock_req.side_effect = WithingsTokenError(task="refresh_access_token", withings_status=601)
    service = _service()

    results = service.sync_user(MagicMock(), uuid4(), LiveSyncMode.WEBHOOK)

    assert mock_req.call_count == 1
    assert results[0]["status"] == "error"


@patch("app.services.providers.withings.notify_service.withings_callback_url", return_value=_OUR_CALLBACK)
@patch("app.services.providers.withings.notify_service.withings_request")
def test_sync_user_defers_when_the_request_budget_is_exhausted(mock_req: MagicMock, mock_url: MagicMock) -> None:
    """Budget exhaustion is backpressure, not a fault: it carries its own wait, and
    reporting it as a generic error throws that away and retries on a blind schedule."""
    mock_req.side_effect = WithingsRequestBudgetExceeded(7)
    service = _service()

    results = service.sync_user(MagicMock(), uuid4(), LiveSyncMode.WEBHOOK)

    assert results == [{"status": "deferred", "reason": "rate_limited", "retry_after": 7}]


@patch("app.services.providers.withings.notify_service.withings_callback_url", return_value=_OUR_CALLBACK)
@patch("app.services.providers.withings.notify_service.withings_request")
def test_remove_user_revokes_own_host_subscriptions(mock_req: MagicMock, mock_url: MagicMock) -> None:
    def side_effect(*, action: str, params: dict, **_kwargs: object) -> dict:
        if action == "list":
            return _profiles(*[(appli, _OUR_CALLBACK) for appli in SUBSCRIBED_APPLIS])
        return {}

    mock_req.side_effect = side_effect
    service = _service()

    results = service.remove_user(MagicMock(), uuid4())

    revoke_calls = [c for c in mock_req.call_args_list if c.kwargs["action"] == "revoke"]
    assert {c.kwargs["params"]["appli"] for c in revoke_calls} == set(SUBSCRIBED_APPLIS)
    assert {r["status"] for r in results} == {"revoked"}


@patch("app.services.providers.withings.notify_service.withings_callback_url", return_value=_OUR_CALLBACK)
@patch("app.services.providers.withings.notify_service.withings_request")
def test_sync_user_reports_per_appli_errors_on_subscribe_failure(mock_req: MagicMock, mock_url: MagicMock) -> None:
    def side_effect(*, action: str, params: dict, **_kwargs: object) -> dict:
        if action == "list":
            return _profiles()
        raise RuntimeError("rate limited")

    mock_req.side_effect = side_effect
    service = _service()

    results = service.sync_user(MagicMock(), uuid4(), LiveSyncMode.WEBHOOK)

    assert {r["status"] for r in results} == {"error"}
    assert {r["appli"] for r in results} == set(SUBSCRIBED_APPLIS)


@patch("app.services.providers.withings.notify_service.withings_callback_url", return_value=_OUR_CALLBACK)
@patch("app.services.providers.withings.notify_service.withings_request")
def test_sync_user_keeps_stale_profile_when_replacement_fails(mock_req: MagicMock, mock_url: MagicMock) -> None:
    appli = SUBSCRIBED_APPLIS[0]

    def side_effect(*, action: str, params: dict, **_kwargs: object) -> dict:
        if action == "list":
            return _profiles((appli, _OUR_CALLBACK_STALE_TOKEN))
        if action == "subscribe":
            raise RuntimeError("temporary failure")
        return {}

    mock_req.side_effect = side_effect
    service = _service()

    results = service.sync_user(MagicMock(), uuid4(), LiveSyncMode.WEBHOOK)

    assert any(result.get("appli") == appli and result["status"] == "error" for result in results)
    assert not any(call.kwargs["action"] == "revoke" for call in mock_req.call_args_list)


@patch("app.services.providers.withings.notify_service.log_structured")
@patch("app.services.providers.withings.notify_service.withings_request")
def test_list_subscriptions_skips_malformed_profiles(mock_req: MagicMock, mock_log: MagicMock) -> None:
    secret = "SECRET_NOTIFY_TOKEN_123"
    malformed_callback = f"https://api.example.com/api/v1/providers/withings/webhooks?token={secret}"
    mock_req.return_value = {
        "profiles": [
            {"appli": 1, "callbackurl": _OUR_CALLBACK},
            {"callbackurl": malformed_callback},
            {"appli": 2, "callbackurl": {"token": secret}},
        ]
    }
    service = _service()

    profiles = service._list_subscriptions(MagicMock(), uuid4())

    assert [(profile.appli, profile.callbackurl) for profile in profiles] == [(1, _OUR_CALLBACK)]
    assert mock_log.call_count == 2
    assert secret not in repr(mock_log.call_args_list)
    url_log, non_string_log = mock_log.call_args_list
    assert url_log.kwargs["action"] == "notify_profile_validation_failed"
    assert url_log.kwargs["callback_url"] == ("https://api.example.com/api/v1/providers/withings/webhooks?redacted")
    assert url_log.kwargs["error"][0]["loc"] == ("appli",)
    assert url_log.kwargs["error"][0]["msg"] == "Field required"
    assert non_string_log.kwargs["callback_url"] is None


@patch("app.services.providers.withings.notify_service.withings_callback_url")
def test_sync_user_skips_when_the_callback_url_is_not_registrable(mock_url: MagicMock) -> None:
    mock_url.side_effect = WithingsCallbackUrlInvalidError("Withings callback URL must use HTTPS")
    service = WithingsNotifyService(connection_repo=MagicMock(), oauth=MagicMock())

    results = service.sync_user(MagicMock(), uuid4(), LiveSyncMode.WEBHOOK)

    assert results == [{"status": "skipped", "reason": "callback_url_invalid"}]


# --------------------------- fan-out and teardown ---------------------------


_SYNC_USER_TASK = SYNC_PROVIDER_USER_SUBSCRIPTION_TASK


def _connection(user_id: object, *, provider_user_id: str | None, created_at: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        user_id=user_id,
        provider_user_id=provider_user_id,
        created_at=created_at,
    )


@patch("app.services.providers.withings.notify_service.celery_app")
@patch("app.services.providers.withings.notify_service.SessionLocal")
async def test_register_subscriptions_dispatches_one_task_per_active_user(
    session_local: MagicMock, celery: MagicMock
) -> None:
    user_ids = [uuid4(), uuid4()]
    service = _service()
    service.connection_repo.get_all_active_by_provider.return_value = [
        _connection(user_id, provider_user_id=f"external-{user_id}", created_at=datetime.now(timezone.utc))
        for user_id in user_ids
    ]
    session_local.return_value.__enter__ = MagicMock(return_value=MagicMock())
    session_local.return_value.__exit__ = MagicMock(return_value=False)

    results = await service.register_subscriptions("https://api.example.com/api/v1/providers/withings/webhooks")

    assert {result["status"] for result in results} == {"dispatched"}
    assert celery.send_task.call_count == 2
    assert {call.kwargs["args"][1] for call in celery.send_task.call_args_list} == {str(u) for u in user_ids}
    assert all(call.args[0] == _SYNC_USER_TASK for call in celery.send_task.call_args_list)


@patch("app.services.providers.withings.notify_service.celery_app")
@patch("app.services.providers.withings.notify_service.SessionLocal")
async def test_register_subscriptions_dispatches_every_account_a_member_owns(
    session_local: MagicMock, celery: MagicMock
) -> None:
    """A member holding a personal AND a provisioned account owns two subscription sets. The
    fan-out used to dispatch [provider, user_id], which the worker resolved to the member's
    primary (personal) connection only — the provisioned scale was never subscribed."""
    user_id = uuid4()
    created_at = datetime.now(timezone.utc)
    personal = _connection(user_id, provider_user_id="personal", created_at=created_at - timedelta(days=30))
    provisioned = _connection(user_id, provider_user_id="provisioned", created_at=created_at)
    service = _service()
    service.connection_repo.get_all_active_by_provider.return_value = [provisioned, personal]
    session_local.return_value.__enter__ = MagicMock(return_value=MagicMock())
    session_local.return_value.__exit__ = MagicMock(return_value=False)

    results = await service.register_subscriptions("https://api.example.com/api/v1/providers/withings/webhooks")

    dispatched_args = [call.kwargs["args"] for call in celery.send_task.call_args_list]
    assert sorted(dispatched_args) == sorted(
        [
            ["withings", str(user_id), str(personal.id)],
            ["withings", str(user_id), str(provisioned.id)],
        ]
    )
    assert {result["connection_id"] for result in results} == {str(personal.id), str(provisioned.id)}


@patch("app.services.providers.withings.notify_service.celery_app")
@patch("app.services.providers.withings.notify_service.SessionLocal")
async def test_register_subscriptions_dispatches_once_per_provider_account(
    session_local: MagicMock, celery: MagicMock
) -> None:
    """Subscriptions belong to the Withings account, so linked profiles share one."""
    oldest_user_id, newer_user_id = uuid4(), uuid4()
    created_at = datetime.now(timezone.utc)
    service = _service()
    service.connection_repo.get_all_active_by_provider.return_value = [
        _connection(newer_user_id, provider_user_id="shared-account", created_at=created_at),
        _connection(oldest_user_id, provider_user_id="shared-account", created_at=created_at - timedelta(seconds=1)),
    ]
    session_local.return_value.__enter__ = MagicMock(return_value=MagicMock())
    session_local.return_value.__exit__ = MagicMock(return_value=False)

    results = await service.register_subscriptions("https://api.example.com/api/v1/providers/withings/webhooks")

    oldest = service.connection_repo.get_all_active_by_provider.return_value[1]
    assert results == [{"status": "dispatched", "user_id": str(oldest_user_id), "connection_id": str(oldest.id)}]
    celery.send_task.assert_called_once_with(
        _SYNC_USER_TASK,
        args=["withings", str(oldest_user_id), str(oldest.id)],
        queue="webhook_sync",
    )


@pytest.mark.parametrize(
    ("configured", "expected_mode"),
    [(LiveSyncMode.WEBHOOK, LiveSyncMode.WEBHOOK), (None, LiveSyncMode.PULL)],
)
def test_reconcile_user_subscriptions_reads_the_current_mode(
    configured: LiveSyncMode | None, expected_mode: LiveSyncMode
) -> None:
    service = _service()
    service.provider_settings_repo = MagicMock()
    service.provider_settings_repo.get_live_sync_mode.return_value = configured
    db, user_id = MagicMock(), uuid4()
    connection = _connection(user_id, provider_user_id="account", created_at=datetime.now(timezone.utc))
    connection.provider = "withings"
    service.connection_repo.get_all_active_by_user.return_value = [connection]
    service.connection_repo.get_all_by_provider_user_id.return_value = [connection]

    with patch.object(service, "sync_user", return_value=[]) as sync_user:
        service.reconcile_user_subscriptions(db, user_id)

    sync_user.assert_called_once_with(db, user_id, expected_mode, connection_id=connection.id)


def test_reconcile_user_subscriptions_skips_without_a_live_sync_mode() -> None:
    service = WithingsNotifyService(connection_repo=MagicMock(), oauth=MagicMock(), default_live_sync_mode=None)
    service.provider_settings_repo = MagicMock()
    service.provider_settings_repo.get_live_sync_mode.return_value = None

    with patch.object(service, "sync_user") as sync_user:
        results = service.reconcile_user_subscriptions(MagicMock(), uuid4())

    assert results == [{"status": "skipped", "reason": "no_live_sync_mode"}]
    sync_user.assert_not_called()


def test_remove_user_revokes_everything_for_the_last_active_link() -> None:
    service = _service()
    service.connection_repo.get_by_user_and_provider.return_value = SimpleNamespace(provider_user_id="account")
    db, user_id = MagicMock(), uuid4()
    service.connection_repo.get_all_by_provider_user_id.return_value = [SimpleNamespace(user_id=user_id)]

    with patch.object(service, "sync_user", return_value=[]) as sync_user:
        service.remove_user(db, user_id)

    sync_user.assert_called_once_with(db, user_id, LiveSyncMode.PULL, connection_id=None)


def test_remove_user_keeps_subscriptions_a_sibling_profile_still_wants() -> None:
    service = _service()
    user_id = uuid4()
    service.connection_repo.get_by_user_and_provider.return_value = SimpleNamespace(provider_user_id="account")
    service.connection_repo.get_all_by_provider_user_id.return_value = [
        SimpleNamespace(user_id=user_id),
        SimpleNamespace(user_id=uuid4()),
    ]

    with patch.object(service, "sync_user") as sync_user:
        results = service.remove_user(MagicMock(), user_id)

    assert results == [{"status": "skipped", "reason": "provider_account_still_linked"}]
    sync_user.assert_not_called()


# ---------------------------------------------------------------------------
# Every connection of a member — against the real repository, stubbed only at
# the Withings HTTP edge (``withings_request``).
# ---------------------------------------------------------------------------


class _RecordingWithings:
    """Stands in for the Withings API: an empty subscription list per account, and a record of
    which connection each call authenticated as."""

    def __init__(self, deferred_connection_id: object = None) -> None:
        self.calls: list[tuple[str, object]] = []
        self.deferred_connection_id = deferred_connection_id

    def __call__(self, *, action: str, connection_id: object = None, **_: object) -> dict:
        self.calls.append((action, connection_id))
        if action == "list" and connection_id == self.deferred_connection_id:
            raise WithingsRequestBudgetExceeded(retry_after_seconds=12)
        return {"profiles": []} if action == "list" else {}

    def connections_for(self, action: str) -> list[object]:
        return [connection_id for call_action, connection_id in self.calls if call_action == action]


def _db_service(mode: LiveSyncMode | None = LiveSyncMode.WEBHOOK) -> WithingsNotifyService:
    service = WithingsNotifyService(connection_repo=UserConnectionRepository(), oauth=MagicMock())
    service.provider_settings_repo = MagicMock()
    service.provider_settings_repo.get_live_sync_mode.return_value = mode
    return service


def _personal_and_provisioned(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> tuple[User, UserConnection, UserConnection]:
    """The staging shape: the member's own account linked first, then the one we provisioned."""
    user = UserFactory()
    personal = UserConnectionFactory(
        user=user,
        provider="withings",
        provider_user_id="personal-account",
        created_at=datetime.now(timezone.utc) - timedelta(days=90),
    )
    _, provisioned = make_provisioned_connection(user=user)
    return user, personal, provisioned


@patch("app.services.providers.withings.notify_service.withings_callback_url", return_value=_OUR_CALLBACK)
@patch("app.services.providers.withings.notify_service.withings_request")
def test_reconcile_subscribes_the_provisioned_account_too(
    mock_req: MagicMock,
    mock_url: MagicMock,
    db: Session,
    make_provisioned_connection: ProvisionedConnectionMaker,
) -> None:
    """Regression (staging, 2026-09-30): the personal account held all 7 subscriptions, the
    provisioned one 0, so a Body Pro 2 weigh-in was never notified."""
    user, personal, provisioned = _personal_and_provisioned(db, make_provisioned_connection)
    withings = _RecordingWithings()
    mock_req.side_effect = withings

    results = _db_service().reconcile_user_subscriptions(db, user.id)

    assert sorted(map(str, withings.connections_for("list"))) == sorted([str(personal.id), str(provisioned.id)])
    assert withings.connections_for("subscribe").count(provisioned.id) == len(SUBSCRIBED_APPLIS)
    assert withings.connections_for("subscribe").count(personal.id) == len(SUBSCRIBED_APPLIS)
    assert None not in withings.connections_for("subscribe")
    for connection in (personal, provisioned):
        entries = [r for r in results if r["connection_id"] == str(connection.id)]
        assert {r["status"] for r in entries} == {"subscribed"}
        assert len(entries) == len(SUBSCRIBED_APPLIS)


@patch("app.services.providers.withings.notify_service.withings_callback_url", return_value=_OUR_CALLBACK)
@patch("app.services.providers.withings.notify_service.withings_request")
def test_reconcile_skips_an_account_an_older_profile_owns(
    mock_req: MagicMock,
    mock_url: MagicMock,
    db: Session,
    make_provisioned_connection: ProvisionedConnectionMaker,
) -> None:
    """Subscriptions belong to the Withings account and the oldest active link owns them, so a
    sibling profile linked later must leave that account alone rather than fight over it."""
    now = datetime.now(timezone.utc)
    sibling_owner = UserFactory()
    UserConnectionFactory(
        user=sibling_owner, provider="withings", provider_user_id="shared", created_at=now - timedelta(days=10)
    )
    user = UserFactory()
    shared = UserConnectionFactory(user=user, provider="withings", provider_user_id="shared", created_at=now)
    _, provisioned = make_provisioned_connection(user=user)
    withings = _RecordingWithings()
    mock_req.side_effect = withings

    results = _db_service().reconcile_user_subscriptions(db, user.id)

    assert withings.connections_for("list") == [provisioned.id]
    assert [r for r in results if r["connection_id"] == str(shared.id)] == [
        {
            "status": "skipped",
            "reason": "provider_account_owned_by_another_user",
            "connection_id": str(shared.id),
        }
    ]


@patch("app.services.providers.withings.notify_service.withings_callback_url", return_value=_OUR_CALLBACK)
@patch("app.services.providers.withings.notify_service.withings_request")
def test_reconcile_can_target_one_connection(
    mock_req: MagicMock,
    mock_url: MagicMock,
    db: Session,
    make_provisioned_connection: ProvisionedConnectionMaker,
) -> None:
    user, _personal, provisioned = _personal_and_provisioned(db, make_provisioned_connection)
    withings = _RecordingWithings()
    mock_req.side_effect = withings

    results = _db_service().reconcile_user_subscriptions(db, user.id, connection_id=provisioned.id)

    assert set(withings.connections_for("list") + withings.connections_for("subscribe")) == {provisioned.id}
    assert {r["connection_id"] for r in results} == {str(provisioned.id)}


@patch("app.services.providers.withings.notify_service.withings_callback_url", return_value=_OUR_CALLBACK)
@patch("app.services.providers.withings.notify_service.withings_request")
def test_reconcile_refuses_a_connection_that_is_not_this_members(
    mock_req: MagicMock,
    mock_url: MagicMock,
    db: Session,
    make_provisioned_connection: ProvisionedConnectionMaker,
) -> None:
    _, someone_elses = make_provisioned_connection()
    user = UserFactory()

    results = _db_service().reconcile_user_subscriptions(db, user.id, connection_id=someone_elses.id)

    assert results == [{"status": "skipped", "reason": "connection_not_active", "connection_id": str(someone_elses.id)}]
    mock_req.assert_not_called()


@patch("app.services.providers.withings.notify_service.withings_callback_url", return_value=_OUR_CALLBACK)
@patch("app.services.providers.withings.notify_service.withings_request")
def test_reconcile_skips_revoked_and_foreign_provider_connections(
    mock_req: MagicMock, mock_url: MagicMock, db: Session
) -> None:
    user = UserFactory()
    UserConnectionFactory(user=user, provider="withings", status=ConnectionStatus.REVOKED)
    UserConnectionFactory(user=user, provider="garmin")

    results = _db_service().reconcile_user_subscriptions(db, user.id)

    assert results == [{"status": "skipped", "reason": "no_active_connection"}]
    mock_req.assert_not_called()


@patch("app.services.providers.withings.notify_service.withings_callback_url", return_value=_OUR_CALLBACK)
@patch("app.services.providers.withings.notify_service.withings_request")
def test_reconcile_keeps_a_deferral_from_one_account_alongside_the_others(
    mock_req: MagicMock,
    mock_url: MagicMock,
    db: Session,
    make_provisioned_connection: ProvisionedConnectionMaker,
) -> None:
    """The task retries on any deferred entry, so one account's backpressure must survive
    aggregation instead of being masked by the other account's success."""
    user, personal, provisioned = _personal_and_provisioned(db, make_provisioned_connection)
    mock_req.side_effect = _RecordingWithings(deferred_connection_id=provisioned.id)

    results = _db_service().reconcile_user_subscriptions(db, user.id)

    assert [r for r in results if r["connection_id"] == str(provisioned.id)] == [
        {"status": "deferred", "reason": "rate_limited", "retry_after": 12, "connection_id": str(provisioned.id)}
    ]
    assert {r["status"] for r in results if r["connection_id"] == str(personal.id)} == {"subscribed"}


@patch("app.services.providers.withings.notify_service.withings_callback_url", return_value=_OUR_CALLBACK)
@patch("app.services.providers.withings.notify_service.withings_request")
def test_reconcile_a_connection_with_no_known_account_is_its_own_owner(
    mock_req: MagicMock,
    mock_url: MagicMock,
    db: Session,
) -> None:
    """No provider_user_id means no account to share: the connection owns its own subscriptions."""
    user = UserFactory()
    orphan = UserConnectionFactory(user=user, provider="withings", provider_user_id=None)
    withings = _RecordingWithings()
    mock_req.side_effect = withings

    results = _db_service().reconcile_user_subscriptions(db, user.id)

    assert withings.connections_for("list") == [orphan.id]
    assert {r["status"] for r in results} == {"subscribed"}
