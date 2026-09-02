"""Withings HMAC request signing.

The digest is computed over an agreed SUBSET of parameters, so the risk these tests guard
is silent: signing too many or too few fields still produces a valid-looking hex string,
and the only symptom is an opaque non-zero status from the API.
"""

import hashlib
import hmac

import pytest

from app.services.providers.withings.signature import (
    NONCE_SIGNED_KEYS,
    REQUEST_SIGNED_KEYS,
    sign,
)

SECRET = "test-client-secret"


def _expected(*values: str) -> str:
    return hmac.new(SECRET.encode(), ",".join(values).encode(), hashlib.sha256).hexdigest()


class TestSign:
    def test_signs_action_client_id_nonce_in_key_order(self) -> None:
        params = {"action": "requesttoken", "client_id": "cid", "nonce": "abc123"}
        # sorted by KEY name: action, client_id, nonce
        assert sign(params, SECRET) == _expected("requesttoken", "cid", "abc123")

    def test_ignores_parameters_outside_the_signed_set(self) -> None:
        """requesttoken also sends grant_type/code/redirect_uri; none may enter the digest."""
        base = {"action": "requesttoken", "client_id": "cid", "nonce": "abc123"}
        noisy = {
            **base,
            "grant_type": "authorization_code",
            "code": "some-code",
            "redirect_uri": "https://example.test/callback",
        }
        assert sign(noisy, SECRET) == sign(base, SECRET)

    def test_getnonce_signs_timestamp_not_nonce(self) -> None:
        params = {"action": "getnonce", "client_id": "cid", "timestamp": "1750000000"}
        assert sign(params, SECRET, NONCE_SIGNED_KEYS) == _expected("getnonce", "cid", "1750000000")

    def test_key_order_is_by_name_not_insertion(self) -> None:
        a = {"nonce": "n", "client_id": "c", "action": "requesttoken"}
        b = {"action": "requesttoken", "client_id": "c", "nonce": "n"}
        assert sign(a, SECRET) == sign(b, SECRET)

    def test_secret_changes_the_digest(self) -> None:
        params = {"action": "requesttoken", "client_id": "cid", "nonce": "abc123"}
        assert sign(params, SECRET) != sign(params, "different-secret")

    def test_missing_signed_parameter_raises(self) -> None:
        """Signing a short message would be accepted locally and rejected opaquely upstream."""
        with pytest.raises(KeyError, match="nonce"):
            sign({"action": "requesttoken", "client_id": "cid"}, SECRET)

    def test_nonce_is_signed_only_for_ordinary_requests(self) -> None:
        assert "nonce" in REQUEST_SIGNED_KEYS
        assert "nonce" not in NONCE_SIGNED_KEYS

    def test_timestamp_is_signed_only_for_getnonce(self) -> None:
        assert "timestamp" in NONCE_SIGNED_KEYS
        assert "timestamp" not in REQUEST_SIGNED_KEYS
