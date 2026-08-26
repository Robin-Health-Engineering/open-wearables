import pytest

from app.services.providers.base_strategy import ProviderCapabilities


def test_inbound_secret_requires_registration_api() -> None:
    """The secret is returned by the registration call, so it presupposes one."""
    with pytest.raises(ValueError, match="webhook_inbound_secret requires webhook_registration_api"):
        ProviderCapabilities(rest_pull=True, webhook_ping=True, webhook_inbound_secret=True)


def test_per_user_subscriptions_require_registration_api() -> None:
    """Per-user subscriptions are a property of how registration fans out, not a substitute for it."""
    with pytest.raises(ValueError, match="webhook_subscription_per_user requires webhook_registration_api"):
        ProviderCapabilities(rest_pull=True, webhook_ping=True, webhook_subscription_per_user=True)


def test_inbound_secret_allows_per_user_subscriptions() -> None:
    """A per-user registration can return a signing secret just as an application-level one can."""
    caps = ProviderCapabilities(
        rest_pull=True,
        webhook_ping=True,
        webhook_registration_api=True,
        webhook_subscription_per_user=True,
        webhook_inbound_secret=True,
    )

    assert caps.webhook_inbound_secret is True


def test_per_user_subscriptions_do_not_require_webhook_ping() -> None:
    """How subscriptions are owned is independent of whether the payload arrives whole:
    a provider can own subscriptions per user and still stream full records."""
    caps = ProviderCapabilities(
        webhook_stream=True,
        webhook_registration_api=True,
        webhook_subscription_per_user=True,
    )

    assert caps.webhook_subscription_per_user is True


def test_subscriptions_are_unmanaged_by_default() -> None:
    caps = ProviderCapabilities(rest_pull=True)

    assert caps.webhook_registration_api is False
    assert caps.webhook_subscription_per_user is False
