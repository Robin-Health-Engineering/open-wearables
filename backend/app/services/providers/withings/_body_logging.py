"""How an upstream body may be logged, per Withings surface. Fork-only, deliberately.

``redact_body`` lives in ``oauth.py``, which is one of the few Withings files upstream also has —
and this fork has to keep rebasing onto upstream cleanly, which is the argument
``withings_sdk_account.py`` makes about not widening shared surfaces. Every caller of the helper
below is fork-only (``dropshipment``, ``end_program``, ``order_detail``, ``sdk_users``), so it
belongs here rather than growing ``oauth.py`` (Lucas, #13).
"""

from __future__ import annotations


# `redact_body` is for the TOKEN endpoint and nowhere else. It masks credential-shaped KEYS —
# client_secret, refresh_token, code — which is the whole risk on that surface and none of the
# risk on the partner surface, where the body Withings echo is our own signed payload: a member's
# email, birth date, weight and, on an order, their home address. None of those match a
# credential-shaped key, so redaction passes them through in the clear.
#
# That was not a hypothesis. `createuserorder`'s error path carried a comment claiming redaction
# covered "a signature AND a postal address"; driving the real function with a 400 whose body was
# `{"address": "Via Roma 1, Milano"}` printed the address into the structured log verbatim. The
# comment had been true of the intent and never of the code.
#
# There is no redaction that fixes it, because an echoed order has no fixed shape — so the
# partner surface logs the SHAPE and not the content. The HTTP status is the diagnostic there
# anyway; the body is a copy of what we just sent.
def describe_body(text: str | None) -> str:
    """Describe an upstream body without reproducing any of it.

    For the signed partner surface (`/v2/sdk`, `/v2/dropshipment`, `/v2/order`, `devicev2-*`),
    whose responses echo the member's own data. Use `redact_body` only on the token endpoint.
    """
    return f"{len(text or '')} bytes, not logged (may echo member data)"
