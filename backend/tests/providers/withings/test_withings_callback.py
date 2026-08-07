import pytest

from app.services.providers.withings import callback
from app.services.providers.withings.callback import WithingsCallbackUrlInvalidError


def test_validate_callback_url_accepts_a_compliant_https_url() -> None:
    callback._validate_callback_url("https://api.example.com/hook")


def test_validate_callback_url_rejects_http_scheme() -> None:
    with pytest.raises(WithingsCallbackUrlInvalidError, match="HTTPS"):
        callback._validate_callback_url("http://api.example.com/hook")


def test_validate_callback_url_rejects_ip_literal_host() -> None:
    with pytest.raises(WithingsCallbackUrlInvalidError, match="public hostname"):
        callback._validate_callback_url("https://127.0.0.1/hook")


def test_validate_callback_url_rejects_localhost() -> None:
    with pytest.raises(WithingsCallbackUrlInvalidError, match="public hostname"):
        callback._validate_callback_url("https://localhost/hook")


def test_validate_callback_url_rejects_non_standard_port() -> None:
    with pytest.raises(WithingsCallbackUrlInvalidError, match="port 80 or 443"):
        callback._validate_callback_url("https://api.example.com:8080/hook")


@pytest.mark.parametrize(("length", "valid"), [(255, True), (256, False)])
def test_validate_callback_url_enforces_255_character_limit(length: int, valid: bool) -> None:
    prefix = "https://api.example.com/"
    url = prefix + "a" * (length - len(prefix))
    assert len(url) == length

    if valid:
        callback._validate_callback_url(url)
    else:
        with pytest.raises(WithingsCallbackUrlInvalidError, match="255"):
            callback._validate_callback_url(url)


@pytest.mark.parametrize("url", ["https://api.example.com./hook", "https://localhost./hook"])
def test_validate_callback_url_rejects_a_trailing_dot_host(url: str) -> None:
    with pytest.raises(WithingsCallbackUrlInvalidError, match="trailing dot"):
        callback._validate_callback_url(url)


def test_validate_callback_url_rejects_a_url_urlsplit_cannot_place_a_host_on() -> None:
    with pytest.raises(WithingsCallbackUrlInvalidError, match="invalid"):
        callback._validate_callback_url("https://user:pass@api.example.com/hook")


def test_url_parts_rejects_userinfo() -> None:
    assert callback._url_parts("https://user:pass@api.example.com/hook") is None


def test_url_parts_rejects_fragment() -> None:
    assert callback._url_parts("https://api.example.com/hook#section") is None
