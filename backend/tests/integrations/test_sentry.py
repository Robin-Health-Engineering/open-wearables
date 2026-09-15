"""Local variables must never be shipped to Sentry.

OW-BACKEND-B (2026-09-15): sentry-python defaults ``include_local_variables`` to True, and on the
Withings partner surface a frame's locals ARE the signed payload — a captured event carried the
member's name, street address, email, phone, birth date and weight in plain text.

The control is one kwarg, and its failure is silent in both directions: nothing crashes, no suite
goes red, and the only symptom is a member's address sitting in a third-party event nobody is
looking at. Removing it is also the *likely* edit, because the comment beside it makes the honest
case that locals are useful in triage. So it is asserted rather than trusted to survive. (Lucas,
#14.)
"""

from unittest.mock import patch

import pytest

from app.config import settings
from app.integrations.sentry import init_sentry


def test_local_variables_are_never_shipped_to_sentry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SENTRY_ENABLED", True)

    with patch("app.integrations.sentry.sentry_sdk.init") as init:
        init_sentry()

    assert init.call_args.kwargs["include_local_variables"] is False
