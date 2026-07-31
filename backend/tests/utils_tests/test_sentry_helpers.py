import logging
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.utils.sentry_helpers import log_and_capture_error


@patch("app.utils.sentry_helpers.sentry_sdk")
def test_4xx_http_exception_is_logged_but_not_captured(mock_sentry: MagicMock) -> None:
    logger = MagicMock(spec=logging.Logger)
    exc = HTTPException(status_code=429, detail="rate limited")

    log_and_capture_error(exc, logger, "sync failed")

    logger.warning.assert_called_once()
    mock_sentry.capture_exception.assert_not_called()


@patch("app.utils.sentry_helpers.sentry_sdk")
def test_5xx_http_exception_is_captured(mock_sentry: MagicMock) -> None:
    exc = HTTPException(status_code=502, detail="upstream error")
    logger = MagicMock(spec=logging.Logger)

    log_and_capture_error(exc, logger, "sync failed")

    mock_sentry.capture_exception.assert_called_once_with(exc)


@patch("app.utils.sentry_helpers.sentry_sdk")
def test_plain_exception_is_captured(mock_sentry: MagicMock) -> None:
    exc = Exception("boom")
    logger = MagicMock(spec=logging.Logger)

    log_and_capture_error(exc, logger, "sync failed")

    mock_sentry.capture_exception.assert_called_once_with(exc)
