from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.services.providers.api_client import make_authenticated_request


@patch("app.services.providers.api_client._get_valid_token", return_value="access-token")
@patch("app.services.providers.api_client.time.sleep")
@patch("app.services.providers.api_client.httpx.Client")
def test_acquire_slot_runs_for_every_http_attempt(
    mock_client_type: MagicMock,
    mock_sleep: MagicMock,
    mock_token: MagicMock,
) -> None:
    rate_limited = MagicMock(status_code=429)
    success = MagicMock(status_code=200)
    success.raise_for_status.return_value = None
    success.json.return_value = {"status": 0, "body": {}}
    request = mock_client_type.return_value.__enter__.return_value.request
    request.side_effect = [rate_limited, success]
    gate = MagicMock()

    make_authenticated_request(
        db=MagicMock(),
        user_id=uuid4(),
        connection_repo=MagicMock(),
        oauth=MagicMock(),
        api_base_url="https://example.test",
        provider_name="withings",
        endpoint="/measure",
        acquire_slot=gate,
    )

    assert request.call_count == 2
    assert gate.call_count == 2
    mock_sleep.assert_called_once()


@patch("app.services.providers.api_client.httpx.Client")
def test_token_validation_precedes_request_gate(mock_client_type: MagicMock) -> None:
    events: list[str] = []
    response = MagicMock(status_code=200)
    response.raise_for_status.return_value = None
    response.json.return_value = {"status": 0, "body": {}}
    request = mock_client_type.return_value.__enter__.return_value.request
    request.side_effect = lambda **_kwargs: events.append("request") or response

    with patch(
        "app.services.providers.api_client._get_valid_token",
        side_effect=lambda *_args: events.append("token") or "access-token",
    ):
        make_authenticated_request(
            db=MagicMock(),
            user_id=uuid4(),
            connection_repo=MagicMock(),
            oauth=MagicMock(),
            api_base_url="https://example.test",
            provider_name="withings",
            endpoint="/measure",
            acquire_slot=lambda: events.append("gate"),
        )

    assert events == ["token", "gate", "request"]


@patch("app.services.providers.api_client._get_valid_token")
@patch("app.services.providers.api_client.httpx.Client")
def test_form_and_json_bodies_are_mutually_exclusive(
    mock_client_type: MagicMock,
    mock_token: MagicMock,
) -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        make_authenticated_request(
            db=MagicMock(),
            user_id=uuid4(),
            connection_repo=MagicMock(),
            oauth=MagicMock(),
            api_base_url="https://example.test",
            provider_name="withings",
            endpoint="/measure",
            form_data={"action": "getmeas"},
            json_data={"action": "getmeas"},
        )

    mock_token.assert_not_called()
    mock_client_type.assert_not_called()
