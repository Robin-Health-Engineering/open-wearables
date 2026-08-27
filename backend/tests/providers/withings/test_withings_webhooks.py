from collections.abc import Iterator
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from app.config import settings
from app.repositories.data_point_series_repository import WriteCounts
from app.schemas.auth import LiveSyncMode
from app.services.providers.withings.webhook_handler import WithingsWebhookHandler

_CALLBACK_TOKEN = "withings-test-token"


def _request(token: str | None = _CALLBACK_TOKEN) -> SimpleNamespace:
    return SimpleNamespace(query_params={"token": token} if token is not None else {})


def _handler(live_sync_mode: LiveSyncMode | None = LiveSyncMode.WEBHOOK) -> WithingsWebhookHandler:
    h = WithingsWebhookHandler(data_247=MagicMock(), workouts=MagicMock())
    h.connection_repo = MagicMock()
    h.connection_repo.get_by_provider_user_id.return_value = MagicMock(user_id=uuid4())
    h.connection_repo.get_all_by_provider_user_id.return_value = [MagicMock(user_id=uuid4())]
    h.provider_settings_repo = MagicMock()
    h.provider_settings_repo.get_live_sync_mode.return_value = live_sync_mode
    return h


@pytest.fixture(autouse=True)
def mock_webhook_delivered() -> Iterator[MagicMock]:
    with patch("app.services.providers.withings.webhook_handler.sync_status_service.webhook_delivered") as mock:
        yield mock


def test_supported_event_types_include_profile_changes() -> None:
    assert _handler().supported_event_types() == ["1", "2", "4", "16", "44", "46", "58"]


def test_parse_payload_reads_form_fields() -> None:
    h = _handler()
    body = b"userid=123&appli=1&startdate=1728000000&enddate=1728001000"
    payload = h.parse_payload(body)
    assert payload["userid"] == "123"
    assert payload["appli"] == "1"
    assert payload["startdate"] == "1728000000"


def test_extract_user_id_reads_the_userid_form_field() -> None:
    h = _handler()
    payload = h.parse_payload(b"userid=123&appli=1&startdate=1728000000&enddate=1728001000")

    assert WithingsWebhookHandler.user_id_field == "userid"
    assert h.extract_user_id(payload) == "123"
    assert h.extract_user_id({"appli": "1"}) is None


def test_verify_signature_accepts_wellformed_notification() -> None:
    h = _handler()
    with patch.object(settings, "withings_webhook_token", SecretStr(_CALLBACK_TOKEN)):
        assert h.verify_signature(_request(), b"userid=123&appli=1&startdate=1&enddate=2") is True


def test_verify_signature_accepts_unknown_user_wellformed() -> None:
    # Unknown/disconnected users are acked 200 and ignored in the worker — never 401.
    h = _handler()
    with patch.object(settings, "withings_webhook_token", SecretStr(_CALLBACK_TOKEN)):
        assert h.verify_signature(_request(), b"userid=999&appli=1&startdate=1&enddate=2") is True


def test_verify_signature_rejects_missing_userid() -> None:
    h = _handler()
    with patch.object(settings, "withings_webhook_token", SecretStr(_CALLBACK_TOKEN)):
        assert h.verify_signature(_request(), b"appli=1") is False


# "tokén": compare_digest raises TypeError on non-ASCII str, and the token is
# caller-supplied — a bare 500 on every POST and HEAD would be one request away.
@pytest.mark.parametrize("token", [None, "", "wrong-token", "tokén"])
def test_verify_signature_rejects_invalid_callback_token(token: str | None) -> None:
    with patch.object(settings, "withings_webhook_token", SecretStr(_CALLBACK_TOKEN)):
        assert _handler().verify_signature(_request(token), b"userid=123&appli=1&startdate=1&enddate=2") is False


def test_handle_probe_accepts_an_authenticated_head_probe() -> None:
    # Withings fires a HEAD probe during subscribe; the handler must return 200.
    with patch.object(settings, "withings_webhook_token", SecretStr(_CALLBACK_TOKEN)):
        assert _handler().handle_probe(_request()) is None


def test_handle_probe_rejects_invalid_callback_token() -> None:
    with (
        patch.object(settings, "withings_webhook_token", SecretStr(_CALLBACK_TOKEN)),
        pytest.raises(HTTPException) as exc_info,
    ):
        _handler().handle_probe(_request("wrong-token"))
    assert exc_info.value.status_code == 401


@patch("app.services.providers.withings.webhook_handler.celery_app")
def test_dispatch_acknowledges_and_enqueues(mock_celery: MagicMock) -> None:
    h = _handler()
    payload = {"userid": "123", "appli": "1", "startdate": "1728000000", "enddate": "1728001000"}
    result = h.dispatch(MagicMock(), payload)
    assert result["status"] == "accepted"
    assert result["appli"] == 1
    mock_celery.send_task.assert_called_once()
    args, kwargs = mock_celery.send_task.call_args
    assert args[0].endswith("process_webhook_push")
    assert kwargs["args"][0] == "withings"
    assert kwargs["queue"] == "webhook_sync"
    h.data_247.save_measures.assert_not_called()


@patch("app.services.providers.withings.webhook_handler.celery_app")
def test_dispatch_ignores_invalid_payload_fields(mock_celery: MagicMock) -> None:
    h = _handler()
    payload = {"userid": "123", "appli": "x", "startdate": "not-a-number", "enddate": "1"}
    result = h.dispatch(MagicMock(), payload)
    assert result["status"] == "ignored"
    assert result["reason"] == "invalid_payload_fields"
    mock_celery.send_task.assert_not_called()


@patch("app.services.providers.withings.webhook_handler.celery_app")
def test_dispatch_ignores_profile_change_update(mock_celery: MagicMock) -> None:
    h = _handler()
    result = h.dispatch(MagicMock(), {"userid": "123", "appli": "46", "action": "update"})
    assert result["status"] == "ignored"
    assert result["reason"] == "profile_change"
    assert result["action"] == "update"
    mock_celery.send_task.assert_not_called()


@pytest.mark.parametrize("action", ["delete", "unlink"])
@patch("app.services.providers.withings.webhook_handler.celery_app")
def test_dispatch_enqueues_profile_change_delete_or_unlink(mock_celery: MagicMock, action: str) -> None:
    h = _handler()
    result = h.dispatch(MagicMock(), {"userid": "123", "appli": "46", "action": action})
    assert result["status"] == "accepted"
    assert result["appli"] == 46
    mock_celery.send_task.assert_called_once()


@patch("app.services.providers.withings.webhook_handler.celery_app")
def test_dispatch_ignores_profile_change_unknown_user(mock_celery: MagicMock) -> None:
    h = _handler()
    h.connection_repo.get_all_by_provider_user_id.return_value = []
    result = h.dispatch(MagicMock(), {"userid": "999", "appli": "46", "action": "unlink"})
    assert result["status"] == "ignored"
    assert result["reason"] == "user_not_found"
    assert result["withings_user_id"] == "999"
    mock_celery.send_task.assert_not_called()


@patch("app.services.providers.withings.webhook_handler.celery_app")
def test_dispatch_ignores_unhandled_appli(mock_celery: MagicMock) -> None:
    h = _handler()
    result = h.dispatch(MagicMock(), {"userid": "123", "appli": "99", "startdate": "1", "enddate": "2"})
    assert result["status"] == "ignored"
    assert "unhandled_appli" in result["reason"]
    mock_celery.send_task.assert_not_called()


@patch("app.services.providers.withings.webhook_handler.celery_app")
def test_dispatch_ignores_data_notification_without_date_range(mock_celery: MagicMock) -> None:
    h = _handler()
    result = h.dispatch(MagicMock(), {"userid": "123", "appli": "1"})
    assert result["status"] == "ignored"
    assert result["reason"] == "missing_date_range"
    mock_celery.send_task.assert_not_called()


@patch("app.services.providers.withings.webhook_handler.celery_app")
def test_dispatch_ignores_when_live_mode_is_pull(mock_celery: MagicMock) -> None:
    h = _handler(LiveSyncMode.PULL)
    result = h.dispatch(MagicMock(), {"userid": "123", "appli": "1", "startdate": "1", "enddate": "2"})
    assert result["status"] == "ignored"
    assert result["reason"] == "live_sync_mode_not_webhook"
    mock_celery.send_task.assert_not_called()


@patch("app.services.providers.withings.webhook_handler.celery_app")
def test_dispatch_defaults_to_pull_when_setting_missing(mock_celery: MagicMock) -> None:
    h = _handler(None)
    result = h.dispatch(MagicMock(), {"userid": "123", "appli": "1", "startdate": "1", "enddate": "2"})
    assert result["status"] == "ignored"
    assert result["reason"] == "live_sync_mode_not_webhook"
    mock_celery.send_task.assert_not_called()


@patch("app.services.providers.withings.webhook_handler.celery_app")
def test_dispatch_unknown_user_is_acked_without_enqueue(mock_celery: MagicMock) -> None:
    h = _handler()
    h.connection_repo.get_by_provider_user_id.return_value = None
    result = h.dispatch(MagicMock(), {"userid": "999", "appli": "1", "startdate": "1", "enddate": "2"})
    assert result["status"] == "ignored"
    assert result["reason"] == "user_not_found"
    mock_celery.send_task.assert_not_called()


@patch("app.services.providers.withings.webhook_handler.celery_app")
def test_dispatch_rejects_unbounded_window(mock_celery: MagicMock) -> None:
    h = _handler()
    result = h.dispatch(MagicMock(), {"userid": "123", "appli": "1", "startdate": "1", "enddate": "31536000"})
    assert result["status"] == "ignored"
    assert result["reason"] == "date_range_too_large"
    mock_celery.send_task.assert_not_called()


@patch("app.services.providers.withings.webhook_handler.celery_app")
def test_dispatch_accepts_activity_with_single_date(mock_celery: MagicMock) -> None:
    h = _handler()
    result = h.dispatch(MagicMock(), {"userid": "123", "appli": "16", "date": "2018-07-02"})
    assert result["status"] == "accepted"
    assert result["appli"] == 16
    mock_celery.send_task.assert_called_once()


@patch("app.services.providers.withings.webhook_handler.celery_app")
def test_dispatch_accepts_new_measure_categories(mock_celery: MagicMock) -> None:
    h = _handler()
    for appli in ("2", "58"):  # temperature, glucose
        mock_celery.reset_mock()
        result = h.dispatch(MagicMock(), {"userid": "1", "appli": appli, "startdate": "1", "enddate": "2"})
        assert result["status"] == "accepted"
        mock_celery.send_task.assert_called_once()


@patch("app.services.providers.withings.webhook_handler.store_raw_payload")
@patch("app.services.providers.withings.webhook_handler.celery_app")
def test_dispatch_stores_raw_payload(mock_celery: MagicMock, mock_store: MagicMock) -> None:
    h = _handler()
    h.dispatch(MagicMock(), {"userid": "1", "appli": "1", "startdate": "1", "enddate": "2"})
    mock_store.assert_called_once()


@patch("app.services.providers.withings.webhook_handler.store_raw_payload")
@patch("app.services.providers.withings.webhook_handler.celery_app")
def test_dispatch_stores_raw_payload_even_when_ignored(mock_celery: MagicMock, mock_store: MagicMock) -> None:
    h = _handler()
    h.connection_repo.get_by_provider_user_id.return_value = None
    result = h.dispatch(MagicMock(), {"userid": "1", "appli": "1", "startdate": "1", "enddate": "2"})
    assert result["status"] == "ignored"
    mock_store.assert_called_once()  # captured for audit before the user-known check rejects it


@patch("app.services.providers.withings.webhook_handler.celery_app")
def test_dispatch_passes_a_fresh_trace_id_not_the_user_id(mock_celery: MagicMock) -> None:
    h = _handler()
    h.dispatch(MagicMock(), {"userid": "distinctive-user-id", "appli": "1", "startdate": "1", "enddate": "2"})
    _, kwargs = mock_celery.send_task.call_args
    trace_id = kwargs["args"][2]
    assert trace_id != "distinctive-user-id"
    assert len(trace_id) == 8


def _process(h: WithingsWebhookHandler, appli: str) -> dict:
    h.connection_repo.get_all_by_provider_user_id.return_value = [MagicMock(user_id=uuid4())]
    # A distinct account per call: fetches are deduplicated per account and window,
    # so a shared id would have each test suppressing the next one's fetch.
    payload = {"userid": uuid4().hex, "appli": appli, "startdate": "1728000000", "enddate": "1728001000"}
    return h.process_payload(MagicMock(), payload, "trace-1")


def _burst(appli: str, userid: str) -> dict:
    return {"userid": userid, "appli": appli, "startdate": "1728000000", "enddate": "1728001000"}


def test_process_payload_fetches_once_for_a_burst_of_duplicate_notifications() -> None:
    h = _handler()
    h.data_247.save_measures.return_value = 1
    userid = uuid4().hex
    db = MagicMock()

    first = h.process_payload(db, _burst("1", userid), "trace-1")
    second = h.process_payload(db, _burst("1", userid), "trace-2")
    third = h.process_payload(db, _burst("4", userid), "trace-3")

    assert first["status"] == "processed"
    assert [r["status"] for r in (second, third)] == ["ignored", "ignored"]
    assert {r["reason"] for r in (second, third)} == {"duplicate_notification"}
    h.data_247.save_measures.assert_called_once()


def test_process_payload_lets_a_duplicate_retry_a_failed_fetch() -> None:
    h = _handler()
    h.data_247.save_measures.side_effect = [RuntimeError("getmeas failed"), 1]
    userid = uuid4().hex
    db = MagicMock()

    with pytest.raises(RuntimeError):
        h.process_payload(db, _burst("1", userid), "trace-1")
    retried = h.process_payload(db, _burst("4", userid), "trace-2")

    assert retried["status"] == "processed"
    assert h.data_247.save_measures.call_count == 2


def test_process_payload_refetches_a_notification_redelivered_after_a_lost_worker() -> None:
    h = _handler()
    h.data_247.save_measures.return_value = 1
    userid = uuid4().hex
    db = MagicMock()

    # SystemExit escapes the ``except Exception`` release, exactly as a SIGKILL would.
    with (
        patch.object(h.data_247, "save_measures", side_effect=SystemExit("worker killed")),
        pytest.raises(SystemExit),
    ):
        h.process_payload(db, _burst("1", userid), "trace-1")

    redelivered = h.process_payload(db, _burst("1", userid), "trace-1")

    assert redelivered["status"] == "processed"
    h.data_247.save_measures.assert_called_once()


def test_process_payload_appli_1_goes_to_measures() -> None:
    h = _handler()
    h.data_247.save_measures.return_value = 1
    result = _process(h, "1")
    assert result["status"] == "processed"
    assert result["domain"] == "measures"
    h.data_247.save_measures.assert_called_once()
    h.data_247.save_sleep.assert_not_called()


def test_process_payload_appli_4_blood_pressure_goes_to_measures() -> None:
    h = _handler()
    h.data_247.save_measures.return_value = 2
    result = _process(h, "4")
    assert result["status"] == "processed"
    h.data_247.save_measures.assert_called_once()


def test_process_payload_appli_44_goes_to_sleep() -> None:
    h = _handler()
    h.data_247.save_sleep.return_value = 1
    result = _process(h, "44")
    assert result["status"] == "processed"
    h.data_247.save_sleep.assert_called_once()
    h.data_247.save_measures.assert_not_called()


def test_process_payload_appli_16_fetches_activity_and_workouts() -> None:
    h = _handler()
    h.data_247.save_activity.return_value = 5
    h.workouts.load_data.return_value = 3
    result = _process(h, "16")
    assert result["status"] == "processed"
    assert result["records_saved"] == 8  # 5 activity + 3 workouts
    h.data_247.save_activity.assert_called_once()
    h.workouts.load_data.assert_called_once()


def test_process_payload_unknown_user_is_reported() -> None:
    h = _handler()
    h.connection_repo.get_all_by_provider_user_id.return_value = []
    payload = {"userid": "999", "appli": "1", "startdate": "1", "enddate": "2"}
    result = h.process_payload(MagicMock(), payload, "trace-1")
    assert result["status"] == "user_not_found"
    assert result["withings_user_id"] == "999"
    h.data_247.save_measures.assert_not_called()


def test_process_payload_ignores_when_live_mode_switched_to_pull() -> None:
    h = _handler(LiveSyncMode.PULL)
    h.connection_repo.get_by_provider_user_id.return_value = MagicMock(user_id=uuid4())
    payload = {"userid": "123", "appli": "1", "startdate": "1", "enddate": "2"}
    result = h.process_payload(MagicMock(), payload, "trace-1")
    assert result["status"] == "ignored"
    assert result["reason"] == "live_sync_mode_not_webhook"
    h.data_247.save_measures.assert_not_called()


@patch("app.services.providers.withings.webhook_handler.store_raw_payload")
def test_process_payload_does_not_store_raw_again(mock_store: MagicMock) -> None:
    h = _handler()
    h.data_247.save_measures.return_value = 0
    _process(h, "1")
    mock_store.assert_not_called()  # captured once at dispatch; the worker must not duplicate it


def test_process_payload_appli_16_single_date_fetches_activity_and_workouts() -> None:
    h = _handler()
    h.connection_repo.get_all_by_provider_user_id.return_value = [MagicMock(user_id=uuid4())]
    h.data_247.save_activity.return_value = 5
    h.workouts.load_data.return_value = 3
    result = h.process_payload(MagicMock(), {"userid": "1", "appli": "16", "date": "2018-07-02"}, "t")
    assert result["records_saved"] == 8
    h.data_247.save_activity.assert_called_once()
    h.workouts.load_data.assert_called_once()


def test_process_payload_fans_out_data_to_every_linked_profile(mock_webhook_delivered: MagicMock) -> None:
    h = _handler()
    user_ids = [uuid4(), uuid4()]
    h.connection_repo.get_all_by_provider_user_id.return_value = [MagicMock(user_id=user_id) for user_id in user_ids]
    h.data_247.save_measures.side_effect = [
        WriteCounts(2, 0),
        WriteCounts(1, 2, failed=1),
    ]

    result = h.process_payload(
        MagicMock(),
        {"userid": "123", "appli": "1", "startdate": "1728000000", "enddate": "1728001000"},
        "trace-1",
    )

    assert result["records_saved"] == 5
    assert result["items_processed"] == 5
    assert set(result["user_ids"]) == {str(user_id) for user_id in user_ids}
    assert [user_result["status"] for user_result in result["user_results"]] == ["success", "partial"]
    assert [user_result["items_processed"] for user_result in result["user_results"]] == [2, 3]
    assert result["user_results"][1]["components"]["measures"]["updated"] == 2
    assert [call.args[1] for call in h.data_247.save_measures.call_args_list] == user_ids
    assert [call.args[0] for call in mock_webhook_delivered.call_args_list] == [str(user_id) for user_id in user_ids]
    assert [call.kwargs["items_processed"] for call in mock_webhook_delivered.call_args_list] == [2, 3]


def test_process_payload_preserves_activity_workout_components_per_user(mock_webhook_delivered: MagicMock) -> None:
    h = _handler()
    user_ids = [uuid4(), uuid4()]
    h.connection_repo.get_all_by_provider_user_id.return_value = [MagicMock(user_id=user_id) for user_id in user_ids]
    h.data_247.save_activity.side_effect = [
        WriteCounts(1, 1),
        WriteCounts(0, 0),
    ]
    h.workouts.load_data.side_effect = [WriteCounts.unsplit(1, skipped=1), WriteCounts.unsplit(0, failed=2)]

    result = h.process_payload(
        MagicMock(),
        {"userid": "123", "appli": "16", "startdate": "1728000000", "enddate": "1728001000"},
        "trace-1",
    )

    assert result["records_saved"] == 3
    assert result["user_results"][0]["status"] == "success"
    assert result["user_results"][0]["skipped"] == 1
    assert result["user_results"][0]["components"]["activity"]["updated"] == 1
    assert result["user_results"][0]["components"]["workouts"]["items_processed"] == 1
    assert result["user_results"][1]["status"] == "failed"
    assert result["user_results"][1]["failed"] == 2
    assert [call.kwargs["status"].value for call in mock_webhook_delivered.call_args_list] == ["success", "failed"]


def test_process_payload_later_user_exception_emits_no_status(mock_webhook_delivered: MagicMock) -> None:
    h = _handler()
    user_ids = [uuid4(), uuid4()]
    h.connection_repo.get_all_by_provider_user_id.return_value = [MagicMock(user_id=user_id) for user_id in user_ids]
    h.data_247.save_measures.side_effect = [WriteCounts.unsplit(2), RuntimeError("retry me")]

    with pytest.raises(RuntimeError, match="retry me"):
        h.process_payload(
            MagicMock(),
            {"userid": "123", "appli": "1", "startdate": "1728000000", "enddate": "1728001000"},
            "trace-1",
        )

    mock_webhook_delivered.assert_not_called()


def test_process_payload_new_categories_go_to_measures() -> None:
    for appli in ("2", "58"):
        h = _handler()
        h.data_247.save_measures.return_value = 1
        result = _process(h, appli)
        assert result["domain"] == "measures"
        h.data_247.save_measures.assert_called_once()


@pytest.mark.parametrize("action", ["delete", "unlink"])
@patch("app.services.providers.withings.webhook_handler.on_connection_revoked")
def test_process_payload_revokes_connections_on_delete_or_unlink(mock_revoked: MagicMock, action: str) -> None:
    h = _handler()
    user_id = uuid4()
    connection = MagicMock(user_id=user_id, updated_at=datetime.now(timezone.utc))
    h.connection_repo.get_all_by_provider_user_id.return_value = [connection]
    h.connection_repo.disconnect.return_value = 1
    payload = {"userid": "123", "appli": "46", "action": action}
    result = h.process_payload(MagicMock(), payload, "trace-1")
    assert result["status"] == "revoked"
    assert result["action"] == action
    assert result["withings_user_id"] == "123"
    assert result["user_ids"] == [str(user_id)]
    h.connection_repo.disconnect.assert_called_once_with(ANY, user_id, "withings")
    mock_revoked.assert_called_once()
    assert mock_revoked.call_args.kwargs["reason"] == f"provider_{action}"


def test_process_payload_revokes_every_connection_for_multi_account_fanout() -> None:
    h = _handler()
    user_ids = [uuid4(), uuid4()]
    h.connection_repo.get_all_by_provider_user_id.return_value = [
        MagicMock(user_id=uid, updated_at=datetime.now(timezone.utc)) for uid in user_ids
    ]
    h.connection_repo.disconnect.return_value = 1
    payload = {"userid": "123", "appli": "46", "action": "unlink"}
    result = h.process_payload(MagicMock(), payload, "trace-1")
    assert result["status"] == "revoked"
    assert set(result["user_ids"]) == {str(uid) for uid in user_ids}
    assert h.connection_repo.disconnect.call_count == 2


def test_process_payload_profile_change_unknown_user_is_reported() -> None:
    h = _handler()
    h.connection_repo.get_all_by_provider_user_id.return_value = []
    payload = {"userid": "999", "appli": "46", "action": "delete"}
    result = h.process_payload(MagicMock(), payload, "trace-1")
    assert result["status"] == "user_not_found"
    assert result["withings_user_id"] == "999"
    h.connection_repo.disconnect.assert_not_called()


def test_process_payload_ignores_profile_change_update() -> None:
    h = _handler()
    payload = {"userid": "123", "appli": "46", "action": "update"}
    result = h.process_payload(MagicMock(), payload, "trace-1")
    assert result["status"] == "ignored"
    assert result["reason"] == "profile_change"
    h.connection_repo.disconnect.assert_not_called()
