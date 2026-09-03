"""The post-OAuth redirect target must be allowlisted.

`authorize` has no API-key dependency, so an unvalidated `redirect_uri` stored there and
replayed by the callback is an open redirect on the API's own origin.
"""

import pytest

from app.utils.redirect_allowlist import is_allowed_redirect_uri

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

    def test_an_empty_allowlist_permits_nothing(self) -> None:
        # Fail closed: a deployment that configures nothing must not redirect anywhere.
        assert is_allowed_redirect_uri("robin://x", []) is False
        assert is_allowed_redirect_uri("https://dashboard.example.test/", [""]) is False
