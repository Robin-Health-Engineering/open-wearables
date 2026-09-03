"""Allowlist for post-OAuth redirect targets.

``GET /oauth/{provider}/authorize`` accepts a ``redirect_uri`` query parameter, stores it
verbatim in Redis, and the callback later hands it to ``RedirectResponse``. Nothing between
those two points inspected it, and the authorize endpoint carries no API-key dependency — so
any third party able to reach the host could mint a state whose callback bounces the browser
to a destination of their choosing. That is an open redirect on the API's own origin, which is
worth more to an attacker than a redirect elsewhere: it inherits the host's reputation and,
for anyone matching on origin, its trust.

Validation happens on the way IN (so a bad value never reaches Redis) and again before the
redirect fires (so a state minted before this shipped, or by a future caller that bypasses the
route, still cannot be used).

Configured by ``OAUTH_ALLOWED_REDIRECT_PREFIXES`` (comma-separated). ``frontend_url`` is always
allowed, so an existing dashboard deployment keeps working without new configuration; native
apps add their custom schemes (e.g. ``robin://``) explicitly.
"""

# Control characters and whitespace: no place in a URI, and the usual vehicle for header or
# log injection once the value is echoed into a Location header. Note this must NOT reject
# "-": ordinary routes contain hyphens.
_FORBIDDEN = frozenset(chr(c) for c in range(0x21)) | {chr(0x7F)}


def is_allowed_redirect_uri(uri: str | None, allowed_prefixes: list[str]) -> bool:
    """Whether ``uri`` may be used as a post-authorization redirect target.

    An absent ``redirect_uri`` is allowed: it means "use the internal success page", which is
    the pre-existing default and not a redirect off-origin at all.
    """
    if uri is None:
        return True
    if not isinstance(uri, str) or uri == "":
        return False
    if any(ch in _FORBIDDEN for ch in uri):
        return False
    return any(prefix and _matches_prefix(uri, prefix) for prefix in allowed_prefixes)


def _matches_prefix(uri: str, prefix: str) -> bool:
    """Prefix match with a delimiter boundary.

    A bare ``startswith`` is not enough, and the default configuration is precisely the
    vulnerable shape: ``frontend_url`` is written the way URLs are written, without a trailing
    slash, so ``https://dashboard.example`` would also admit
    ``https://dashboard.example.attacker.test/steal`` — a redirect to a third-party host, which
    is the exact harm this module exists to prevent.

    Custom schemes were already safe (``robin://`` ends in ``/``, so ``robinevil://`` cannot
    match), which is why prefix matching rather than URL parsing is still the right approach —
    a native target has no host for urlparse to check. What was missing is the boundary.
    """
    if uri == prefix:
        return True
    if prefix.endswith("/"):
        return uri.startswith(prefix)
    # A bare origin may legitimately be followed by a path, a query or a fragment; anything
    # else after it is a different host.
    return any(uri.startswith(prefix + sep) for sep in ("/", "?", "#"))
