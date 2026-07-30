import pytest

from app.services.providers.base_strategy import ProviderCapabilities, WebhookSubscriptionOwner


def test_inbound_secret_requires_managed_subscriptions() -> None:
    """The secret is returned by the registration call, so it presupposes a registration
    mechanism — not a particular owner of it."""
    with pytest.raises(ValueError, match="webhook_inbound_secret requires webhook_subscription_owner"):
        ProviderCapabilities(rest_pull=True, webhook_ping=True, webhook_inbound_secret=True)


def test_inbound_secret_allows_user_owned_subscriptions() -> None:
    """A per-user registration can return a signing secret just as an application-level one can."""
    caps = ProviderCapabilities(
        rest_pull=True,
        webhook_ping=True,
        webhook_subscription_owner=WebhookSubscriptionOwner.USER,
        webhook_inbound_secret=True,
    )

    assert caps.webhook_inbound_secret is True


def test_user_owned_subscriptions_do_not_require_webhook_ping() -> None:
    """Who owns a subscription is independent of whether the payload arrives whole:
    a provider can own subscriptions per user and still stream full records."""
    caps = ProviderCapabilities(
        webhook_stream=True,
        webhook_subscription_owner=WebhookSubscriptionOwner.USER,
    )

    assert caps.webhook_subscription_owner is WebhookSubscriptionOwner.USER


def test_ownership_is_a_single_axis() -> None:
    caps = ProviderCapabilities(
        rest_pull=True,
        webhook_ping=True,
        webhook_subscription_owner=WebhookSubscriptionOwner.USER,
    )

    assert caps.webhook_subscription_owner is WebhookSubscriptionOwner.USER
    assert caps.webhook_subscription_owner != WebhookSubscriptionOwner.APPLICATION


def test_subscriptions_are_unmanaged_by_default() -> None:
    assert ProviderCapabilities(rest_pull=True).webhook_subscription_owner is None
