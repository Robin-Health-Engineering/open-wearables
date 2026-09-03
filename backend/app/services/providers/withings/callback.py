"""Withings callback identity, redaction, and ownership policy."""

from ipaddress import ip_address
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from app.config import settings

MANAGED_COMMENT = "open-wearables"


class WithingsCallbackUnavailableError(ValueError):
    """Indicate that this configuration cannot register a Withings callback."""


class WithingsWebhookTokenUnconfiguredError(WithingsCallbackUnavailableError):
    """Notifications are not configured at all: the shared-secret token is unset."""


class WithingsCallbackUrlInvalidError(WithingsCallbackUnavailableError):
    """API_BASE_URL produces a URL Withings refuses (scheme, host, port, length)."""


def withings_callback_url() -> str:
    """Return the shared-secret callback URL registered with Withings."""
    token = settings.withings_webhook_token
    if token is None or not token.get_secret_value():
        raise WithingsWebhookTokenUnconfiguredError(
            "WITHINGS_WEBHOOK_TOKEN must be configured for Withings notifications"
        )
    query = urlencode({"token": token.get_secret_value()})
    callback_url = f"{settings.api_base_url}{settings.api_v1}/providers/withings/webhooks?{query}"
    _validate_callback_url(callback_url)
    return callback_url


def callback_urls_match(left: str, right: str) -> bool:
    """Compare exact callback identity while ignoring query ordering."""
    left_components = _callback_components(left)
    right_components = _callback_components(right)
    return left_components is not None and right_components is not None and left_components == right_components


def callback_endpoints_match(left: str, right: str) -> bool:
    """Compare origin and path while ignoring a rotated query-string token."""
    left_parts = _url_parts(left)
    right_parts = _url_parts(right)
    return left_parts is not None and left_parts == right_parts


def redact_callback_url(url: str) -> str:
    """Remove query values before a callback URL reaches logs or task results."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "<invalid-callback-url>"
    query = "redacted" if parsed.query else ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def _validate_callback_url(url: str) -> None:
    parts = _url_parts(url)
    if parts is None:
        raise WithingsCallbackUrlInvalidError("Withings callback URL is invalid")
    scheme, hostname, port, _path = parts
    try:
        ip_address(hostname)
        is_ip_address = True
    except ValueError:
        is_ip_address = False
    if scheme != "https":
        raise WithingsCallbackUrlInvalidError("Withings callback URL must use HTTPS")
    if hostname.endswith("."):
        # Preserve one callback identity for hosts that DNS otherwise treats as equivalent.
        raise WithingsCallbackUrlInvalidError("Withings callback URL host must not end in a trailing dot")
    if hostname == "localhost" or hostname.endswith(".localhost") or is_ip_address:
        raise WithingsCallbackUrlInvalidError("Withings callback URL must use a public hostname")
    if port not in {80, 443}:
        raise WithingsCallbackUrlInvalidError("Withings callback URL must use port 80 or 443")
    if len(url) > 255:
        raise WithingsCallbackUrlInvalidError("Withings callback URL must not exceed 255 characters")


def _callback_components(url: str) -> tuple[str, str, int, str, dict[str, list[str]]] | None:
    parts = _url_parts(url)
    if parts is None:
        return None
    try:
        query = parse_qs(urlsplit(url).query, keep_blank_values=True)
    except ValueError:
        return None
    return (*parts, query)


def _url_parts(url: str) -> tuple[str, str, int, str] | None:
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        if not scheme or hostname is None or parsed.username or parsed.password or parsed.fragment:
            return None
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None
    return scheme, hostname.lower(), port, parsed.path.rstrip("/") or "/"
