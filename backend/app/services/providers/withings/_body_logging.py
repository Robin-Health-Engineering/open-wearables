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


# Withings' envelope on a non-zero status is `{"status": N, "error": "...", "body": {...}}`, and the
# `error` string is the ONLY thing that says which parameter they objected to. Measured, not
# assumed: a real 503 from the token endpoint carried
# `{"body":{},"error":"Invalid Params: invalid refresh_token","status":503}` — a parameter NAME, no
# member data.
#
# Withholding it made a diagnosable failure undiagnosable. A cellular order came back 503 on
# staging and it took a Sentry frame-locals dump and an hour of reading Withings' OpenAPI document
# to get no further than "some parameter is wrong", because the one sentence naming it had been
# dropped one line from where it was received.
#
# Bounded and stripped of newlines all the same: it is an upstream string, and the guarantee that
# it only ever names parameters is Withings' to keep, not ours. If they ever echo a submitted value
# the cap limits what lands in a log line, and `describe_body` above remains the rule for the BODY.
_MAX_UPSTREAM_REASON = 200


def upstream_reason(envelope: object) -> str | None:
    """Withings' own `error` string from a response envelope, bounded — or None if absent."""
    if not isinstance(envelope, dict):
        return None
    reason = envelope.get("error")
    if not isinstance(reason, str) or not reason.strip():
        return None
    return " ".join(reason.split())[:_MAX_UPSTREAM_REASON]
