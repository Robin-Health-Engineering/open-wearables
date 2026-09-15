"""Withings' `error` string is the only thing that says WHICH parameter they rejected.

A cellular order came back `status=503` on staging and the failure was undiagnosable: the log had
the number and nothing else, because the whole envelope was withheld under the PII rule. The rule
is right about the BODY — it echoes a signed payload containing the member's address — but the
envelope's own `error` field is Withings describing their own validation, and a real 503 from the
token endpoint proved the shape:

    {"body": {}, "error": "Invalid Params: invalid refresh_token", "status": 503}

A parameter name. So it is surfaced, bounded, with the bound justified by the fact that the
"names parameters only" guarantee is Withings' to keep rather than ours.
"""

from __future__ import annotations

from app.services.providers.withings._body_logging import upstream_reason


class TestUpstreamReason:
    def test_returns_withings_own_words(self) -> None:
        envelope = {"body": {}, "error": "Invalid Params: invalid refresh_token", "status": 503}

        assert upstream_reason(envelope) == "Invalid Params: invalid refresh_token"

    def test_is_bounded(self) -> None:
        # The guarantee that this only ever names parameters belongs to Withings. If they ever echo
        # a submitted value, the cap decides how much of it reaches a log line.
        assert len(upstream_reason({"error": "x" * 5000}) or "") == 200

    def test_collapses_newlines(self) -> None:
        # A multi-line upstream string would otherwise break one structured log record into
        # several, which is how a log line stops being greppable.
        assert upstream_reason({"error": "Invalid\nParams:\r\n  bad ean"}) == "Invalid Params: bad ean"

    def test_absent_or_unusable_gives_none(self) -> None:
        # None rather than a placeholder: the log field is omitted entirely when Withings said
        # nothing, so an empty value in a log means "they were silent" and not "we lost it".
        for envelope in ({}, {"error": ""}, {"error": "   "}, {"error": 42}, {"status": 503}, None, "nope", []):
            assert upstream_reason(envelope) is None, envelope
