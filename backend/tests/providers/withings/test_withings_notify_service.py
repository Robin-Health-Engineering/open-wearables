from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.schemas.auth import LiveSyncMode
from app.services.providers.withings.applis import SUBSCRIBED_APPLIS
from app.services.providers.withings.callback import (
    MANAGED_COMMENT,
    WithingsCallbackUrlInvalidError,
    WithingsWebhookTokenUnconfiguredError,
)
from app.services.providers.withings.notify_service import WithingsNotifyService
from app.services.providers.withings.oauth import WithingsTokenError
from app.services.providers.withings.request_budget import WithingsRequestBudgetExceeded

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
