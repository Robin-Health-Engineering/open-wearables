"""The post-OAuth redirect target must be allowlisted.

`authorize` has no API-key dependency, so an unvalidated `redirect_uri` stored there and
replayed by the callback is an open redirect on the API's own origin.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.services.providers.factory import ProviderFactory
from app.utils.redirect_allowlist import is_allowed_redirect_uri
from tests.factories import UserFactory

PREFIXES = ["robin://", "https://dashboard.example.test"]


class TestIsAllowedRedirectUri:
    @pytest.mark.parametrize(
        "uri",
        [
            "robin://profile/health-connection",
            "robin://profile/health-connection?connected=withings",
            "https://dashboard.example.test/settings",
        ],
    )
    def test_allows_configured_prefixes_including_hyphenated_routes(self, uri: str) -> None:
        assert is_allowed_redirect_uri(uri, PREFIXES) is True

    @pytest.mark.parametrize(
        "uri",
        [
            # The boundary case: a prefix match without a delimiter admits any host that
            # merely STARTS with an allowed one. Written as a registrable domain because
            # that is what an attacker buys.
            "https://dashboard.example.test.attacker.com/steal",
            "https://dashboard.example.testevil.com/steal",
            "https://evil.test/steal",
            "http://dashboard.example.test/settings",  # scheme downgrade is a different origin
            "//evil.test",
            "javascript:alert(1)",
            "",
        ],
    )
    def test_rejects_everything_else(self, uri: str) -> None:
        assert is_allowed_redirect_uri(uri, PREFIXES) is False

    def test_absent_uri_is_allowed_and_means_the_internal_success_page(self) -> None:
        # Not a redirect off-origin at all; this is the pre-existing default.
        assert is_allowed_redirect_uri(None, PREFIXES) is True

    @pytest.mark.parametrize("ch", ["\n", "\r", "\x00", " ", "\t"])
    def test_rejects_control_characters_and_whitespace(self, ch: str) -> None:
        # Header/log injection once the value reaches a Location header.
        assert is_allowed_redirect_uri(f"robin://a{ch}b", PREFIXES) is False

    def test_a_bare_origin_with_query_or_fragment_is_still_allowed(self) -> None:
        # An origin followed by ? or # is the same origin; rejecting it would look like a bug.
        assert is_allowed_redirect_uri("https://dashboard.example.test?next=x", PREFIXES) is True
        assert is_allowed_redirect_uri("https://dashboard.example.test#frag", PREFIXES) is True
        assert is_allowed_redirect_uri("https://dashboard.example.test", PREFIXES) is True

    def test_custom_schemes_carry_their_own_boundary(self) -> None:
        # "robin://" already ends in "/", so a lookalike scheme cannot match.
        assert is_allowed_redirect_uri("robin://callback", PREFIXES) is True
        assert is_allowed_redirect_uri("robinevil://callback", PREFIXES) is False

    def test_an_empty_allowlist_permits_nothing(self) -> None:
        # Fail closed: a deployment that configures nothing must not redirect anywhere.
        assert is_allowed_redirect_uri("robin://x", []) is False
        assert is_allowed_redirect_uri("https://dashboard.example.test/", [""]) is False


def _withings_token_envelope() -> MagicMock:
    return MagicMock(
        status_code=200,
        raise_for_status=MagicMock(return_value=None),
        json=MagicMock(
            return_value={
                "status": 0,
                "body": {
                    "userid": "999",
                    "access_token": "at",
                    "refresh_token": "rt",
                    "expires_in": 10800,
                    "token_type": "Bearer",
                },
            }
        ),
    )


class TestCallbackHonoursTheAllowlist:
    """The callback re-checks the STORED redirect_uri before using it.

    Without these the re-check is dead code that could be deleted with CI still green — the
    branch was uncovered (91%, lines 173-175) even after the allowlist landed.
    """

    @patch("app.api.routes.v1.oauth.celery_app.send_task")
    @patch("app.integrations.celery.tasks.sync_vendor_data.delay")
    @patch("httpx.post")
    def test_redirects_to_an_allowlisted_stored_uri(
        self,
        mock_post: MagicMock,
        mock_sync: MagicMock,
        mock_send: MagicMock,
        client: TestClient,
        db: Session,
    ) -> None:
        user = UserFactory()
        allowed = f"{settings.frontend_url}/pair/success"
        strategy = ProviderFactory().get_provider("withings")
        assert strategy.oauth
        _, state = strategy.oauth.get_authorization_url(user.id, allowed)
        mock_post.return_value = _withings_token_envelope()

        response = client.get(
            "/api/v1/oauth/withings/callback",
            params={"code": "authorization_code", "state": state},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == allowed

    @patch("app.api.routes.v1.oauth.celery_app.send_task")
    @patch("app.integrations.celery.tasks.sync_vendor_data.delay")
    @patch("httpx.post")
    def test_falls_back_to_the_success_page_for_a_foreign_stored_uri(
        self,
        mock_post: MagicMock,
        mock_sync: MagicMock,
        mock_send: MagicMock,
        client: TestClient,
        db: Session,
    ) -> None:
        """A state minted before the authorize check existed must still not be replayable.

        get_authorization_url is called directly here precisely because the route now rejects
        this value — that is what a pre-existing Redis entry looks like.
        """
        user = UserFactory()
        hostile = "https://evil.test/steal"
        strategy = ProviderFactory().get_provider("withings")
        assert strategy.oauth
        _, state = strategy.oauth.get_authorization_url(user.id, hostile)
        mock_post.return_value = _withings_token_envelope()

        response = client.get(
            "/api/v1/oauth/withings/callback",
            params={"code": "authorization_code", "state": state},
            follow_redirects=False,
        )

        location = response.headers.get("location", "")
        assert "evil.test" not in location
        assert location.startswith("/api/v1/oauth/success")
