from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.schemas.providers.withings import WithingsMeasure
from app.services.providers.withings import _client


def test_scale_measure_applies_power_of_ten() -> None:
    assert _client.scale_measure(WithingsMeasure(value=7500, type=1, unit=-2)) == Decimal("75.00")
    assert _client.scale_measure(WithingsMeasure(value=180, type=4, unit=-2)) == Decimal("1.80")
    assert _client.scale_measure(WithingsMeasure(value=65, type=11, unit=0)) == Decimal("65")


@patch("app.services.providers.withings._client.make_authenticated_request")
def test_withings_request_unwraps_body(mock_req: MagicMock) -> None:
    mock_req.return_value = {"status": 0, "body": {"measuregrps": [1, 2]}}
    body = _client.withings_request(
        db=MagicMock(),
        user_id=uuid4(),
        connection_repo=MagicMock(),
        oauth=MagicMock(),
        service_path="/measure",
        action="getmeas",
        params={"meastypes": "1"},
    )
    assert body == {"measuregrps": [1, 2]}
    kwargs = mock_req.call_args.kwargs
    assert kwargs["method"] == "POST"
    assert kwargs["form_data"]["action"] == "getmeas"
    assert kwargs["form_data"]["meastypes"] == "1"
    assert kwargs["acquire_slot"] is _client.acquire_request_slot


@patch("app.services.providers.withings._client.make_authenticated_request")
def test_withings_request_status_100_is_not_silently_treated_as_no_data(mock_req: MagicMock) -> None:
    mock_req.return_value = {"status": 100, "body": {}}
    with pytest.raises(_client.WithingsAPIError) as exc_info:
        _client.withings_request(
            db=MagicMock(),
            user_id=uuid4(),
            connection_repo=MagicMock(),
            oauth=MagicMock(),
            service_path="/measure",
            action="getmeas",
            params={},
        )
    assert exc_info.value.withings_status == 100
    assert exc_info.value.status_code == 401


@patch("app.services.providers.withings._client.make_authenticated_request")
def test_withings_request_raises_on_error_status(mock_req: MagicMock) -> None:
    mock_req.return_value = {"status": 503, "body": {}}
    with pytest.raises(_client.WithingsAPIError) as exc_info:
        _client.withings_request(
            db=MagicMock(),
            user_id=uuid4(),
            connection_repo=MagicMock(),
            oauth=MagicMock(),
            service_path="/measure",
            action="getmeas",
            params={},
        )
    assert exc_info.value.withings_status == 503
    assert exc_info.value.action == "getmeas"
    assert exc_info.value.status_code == 502


@patch("app.services.providers.withings._client.make_authenticated_request")
def test_withings_request_raises_typed_error_on_rate_limit_status(mock_req: MagicMock) -> None:
    mock_req.return_value = {"status": 601, "body": {}}
    with pytest.raises(_client.WithingsAPIError) as exc_info:
        _client.withings_request(
            db=MagicMock(),
            user_id=uuid4(),
            connection_repo=MagicMock(),
            oauth=MagicMock(),
            service_path="/measure",
            action="getmeas",
            params={},
        )
    assert exc_info.value.withings_status == 601
    assert exc_info.value.status_code == 429


@patch("app.services.providers.withings._client.make_authenticated_request")
def test_withings_request_does_not_treat_343_as_authentication_failure(mock_req: MagicMock) -> None:
    mock_req.return_value = {"status": 343, "body": {}}
    oauth = MagicMock()

    with pytest.raises(_client.WithingsAPIError) as exc_info:
        _client.withings_request(
            db=MagicMock(),
            user_id=uuid4(),
            connection_repo=MagicMock(),
            oauth=oauth,
            service_path="/measure",
            action="getmeas",
            params={},
        )

    assert exc_info.value.withings_status == 343
    assert exc_info.value.status_code == 502
    oauth.refresh_access_token.assert_not_called()
    assert mock_req.call_count == 1


@patch("app.services.providers.withings._client.make_authenticated_request")
def test_paginate_follows_more_offset(mock_req: MagicMock) -> None:
    mock_req.side_effect = [
        {"status": 0, "body": {"rows": [1, 2], "more": 1, "offset": 2}},
        {"status": 0, "body": {"rows": [3], "more": 0, "offset": 0}},
    ]
    page = _client.paginate(
        db=MagicMock(),
        user_id=uuid4(),
        connection_repo=MagicMock(),
        oauth=MagicMock(),
        service_path="/v2/measure",
        action="getactivity",
        params={},
        list_key="rows",
    )
    assert page.rows == [1, 2, 3]
    assert mock_req.call_count == 2


@patch("app.services.providers.withings._client.make_authenticated_request")
def test_paginate_keeps_the_first_pages_envelope(mock_req: MagicMock) -> None:
    mock_req.side_effect = [
        {"status": 0, "body": {"rows": [1], "timezone": "Europe/Berlin", "more": 1, "offset": 1}},
        {"status": 0, "body": {"rows": [2], "more": 0, "offset": 0}},
    ]
    page = _client.paginate(
        db=MagicMock(),
        user_id=uuid4(),
        connection_repo=MagicMock(),
        oauth=MagicMock(),
        service_path="/measure",
        action="getmeas",
        params={},
        list_key="rows",
    )
    assert page.rows == [1, 2]
    assert page.envelope["timezone"] == "Europe/Berlin"
    # The rows travel in .rows; duplicating them in the envelope would retain them twice.
    assert "rows" not in page.envelope


@patch("app.services.providers.withings._client.make_authenticated_request")
def test_paginate_raises_when_offset_does_not_advance(mock_req: MagicMock) -> None:
    mock_req.return_value = {"status": 0, "body": {"rows": [1], "more": 1, "offset": 0}}
    with pytest.raises(_client.WithingsPaginationError, match="offset did not advance"):
        _client.paginate(
            db=MagicMock(),
            user_id=uuid4(),
            connection_repo=MagicMock(),
            oauth=MagicMock(),
            service_path="/v2/measure",
            action="getactivity",
            params={},
            list_key="rows",
        )
    assert mock_req.call_count == 1


@patch("app.services.providers.withings._client.make_authenticated_request")
def test_paginate_stops_at_page_cap(mock_req: MagicMock) -> None:
    def _page(**kwargs: Any) -> dict[str, Any]:
        offset = kwargs["form_data"].get("offset", 0)
        return {"status": 0, "body": {"rows": [offset], "more": 1, "offset": offset + 1}}

    mock_req.side_effect = _page
    with pytest.raises(_client.WithingsPaginationError, match="exceeded"):
        _client.paginate(
            db=MagicMock(),
            user_id=uuid4(),
            connection_repo=MagicMock(),
            oauth=MagicMock(),
            service_path="/v2/measure",
            action="getactivity",
            params={},
            list_key="rows",
        )
    assert mock_req.call_count == _client._MAX_PAGES
