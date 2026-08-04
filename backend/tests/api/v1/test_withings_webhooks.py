from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.config import settings

CALLBACK_TOKEN = "withings-test-token"
ENDPOINT = "/api/v1/providers/withings/webhooks"


class TestWithingsWebhookHeadProbe:
    def test_head_returns_200(self, client: TestClient, db: Session) -> None:
        with patch.object(settings, "withings_webhook_token", SecretStr(CALLBACK_TOKEN)):
            response = client.head(ENDPOINT, params={"token": CALLBACK_TOKEN})
        assert response.status_code == 200, (
            f"Expected 200 for HEAD probe, got {response.status_code}. "
            "Withings subscribe handshake will fail if this is not 200."
        )

    def test_get_challenge_is_not_implemented(self, client: TestClient, db: Session) -> None:
        with patch.object(settings, "withings_webhook_token", SecretStr(CALLBACK_TOKEN)):
            response = client.get(ENDPOINT, params={"token": CALLBACK_TOKEN})
        assert response.status_code == 501

    def test_head_returns_empty_body(self, client: TestClient, db: Session) -> None:
        with patch.object(settings, "withings_webhook_token", SecretStr(CALLBACK_TOKEN)):
            response = client.head(ENDPOINT, params={"token": CALLBACK_TOKEN})
        assert response.content == b""

    def test_head_rejects_invalid_token(self, client: TestClient, db: Session) -> None:
        with patch.object(settings, "withings_webhook_token", SecretStr(CALLBACK_TOKEN)):
            response = client.head(ENDPOINT, params={"token": "wrong-token"})
        assert response.status_code == 401


class TestWithingsWebhookNotification:
    def test_post_rejects_missing_token(self, client: TestClient, db: Session) -> None:
        with patch.object(settings, "withings_webhook_token", SecretStr(CALLBACK_TOKEN)):
            response = client.post(ENDPOINT, content=b"userid=123&appli=1&startdate=1&enddate=2")
        assert response.status_code == 401
