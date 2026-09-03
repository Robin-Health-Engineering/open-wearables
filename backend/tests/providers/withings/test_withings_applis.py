from unittest.mock import patch

import pytest
from pydantic import SecretStr

from app.config import settings
from app.services.providers.withings.applis import (
    APPLI_DOMAIN,
    PROFILE_CHANGE_APPLI,
    SUBSCRIBED_APPLIS,
)
from app.services.providers.withings.callback import (
    callback_endpoints_match,
    callback_urls_match,
    redact_callback_url,
    withings_callback_url,
)


def test_subscribed_applis_are_derived_from_routing_plus_profile_change() -> None:
    assert sorted({*APPLI_DOMAIN, PROFILE_CHANGE_APPLI}) == SUBSCRIBED_APPLIS
    assert SUBSCRIBED_APPLIS == [1, 2, 4, 16, 44, 46, 58]


def test_appli_domains() -> None:
    assert APPLI_DOMAIN[2] == "measures"  # temperature
    assert APPLI_DOMAIN[58] == "measures"  # glucose
    assert APPLI_DOMAIN[16] == "activity_workouts"
    assert APPLI_DOMAIN[44] == "sleep"
    assert PROFILE_CHANGE_APPLI == 46
    assert 62 not in APPLI_DOMAIN
    # ECG has no canonical model mapping.
    assert 54 not in APPLI_DOMAIN


def test_callback_url_uses_encoded_token_and_webhooks_path() -> None:
    with (
        patch.object(settings, "api_base_url", "https://api.example.com"),
        patch.object(settings, "withings_webhook_token", SecretStr("a token/+")),
    ):
        url = withings_callback_url()
    assert url.endswith("/api/v1/providers/withings/webhooks?token=a+token%2F%2B")


def test_callback_url_requires_webhook_token() -> None:
    with (
        patch.object(settings, "withings_webhook_token", None),
        pytest.raises(ValueError, match="WITHINGS_WEBHOOK_TOKEN"),
    ):
        withings_callback_url()


@pytest.mark.parametrize(
    "api_base_url",
    ["https://api.example.com", "https://api.example.com:443", "https://api.example.com:80"],
)
def test_callback_url_accepts_documented_https_ports(api_base_url: str) -> None:
    with (
        patch.object(settings, "api_base_url", api_base_url),
        patch.object(settings, "withings_webhook_token", SecretStr("token")),
    ):
        assert withings_callback_url().startswith(api_base_url)


@pytest.mark.parametrize(
    "api_base_url",
    [
        "http://api.example.com",
        "http://api.example.com:80",
        "https://localhost",
        "https://LOCALHOST.",
        "https://service.localhost",
        "https://127.0.0.1",
        "https://[::1]",
        "https://api.example.com:8443",
    ],
)
def test_callback_url_rejects_restricted_endpoints(api_base_url: str) -> None:
    with (
        patch.object(settings, "api_base_url", api_base_url),
        patch.object(settings, "withings_webhook_token", SecretStr("token")),
        pytest.raises(ValueError, match="Withings callback URL"),
    ):
        withings_callback_url()


@pytest.mark.parametrize(("length", "valid"), [(255, True), (256, False)])
def test_callback_url_enforces_final_encoded_length(length: int, valid: bool) -> None:
    api_base_url = "https://api.example.com"
    prefix = f"{api_base_url}{settings.api_v1}/providers/withings/webhooks?token="
    token = "a" * (length - len(prefix))
    context = patch.object(settings, "withings_webhook_token", SecretStr(token))

    with patch.object(settings, "api_base_url", api_base_url), context:
        if valid:
            assert len(withings_callback_url()) == length
        else:
            with pytest.raises(ValueError, match="255"):
                withings_callback_url()


def test_callback_identity_normalizes_query_order_but_not_secret() -> None:
    assert callback_urls_match(
        "https://API.example/hook?token=a%2Fb&mode=live",
        "https://api.example:443/hook?mode=live&token=a%2Fb",
    )
    assert not callback_urls_match("https://api.example/hook?token=old", "https://api.example/hook?token=new")
    assert not callback_urls_match("not-a-url", "also-not-a-url")


def test_callback_endpoints_match_ignores_rotated_token() -> None:
    desired = "https://api.example/hook?token=current"
    assert callback_endpoints_match("https://api.example:443/hook?token=old", desired)
    assert not callback_endpoints_match("https://api.example/old-path", desired)
    assert not callback_endpoints_match("https://foreign.example/hook", desired)


def test_callback_redaction_removes_secret() -> None:
    redacted = redact_callback_url("https://api.example/hook?token=do-not-log")
    assert redacted == "https://api.example/hook?redacted"
    assert "do-not-log" not in redacted
